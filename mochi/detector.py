"""Sustained z-score detection plus absolute limits (DET-2..12, LIF-5).

Fixes from the critique:
- Problem 2: absolute limits are checked on every sample, not only during cold start.
- Problem 4: sustain is time-based with tolerance (>= 80% of samples above the line
  over the window), each level has its own condition, and gaps (sleep) reset streaks.
- Problem 15: z alone isn't enough; the reading must also rise by a minimum absolute
  amount (e.g. +15 CPU points), and sigma has a per-metric floor.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .baseline import BaselineStore
from .config import Config
from .model import BASELINE_METRICS, METRICS, WATCH_METRICS, Level, Sample


def level_floor_ok(metric: str, level: Level, x: float, sample: Sample, tjmax: float = 100.0) -> bool:
    """A reading must also be *practically* high to reach Heads up or Uh-oh.

    z-scores on very steady metrics (memory, idle CPU) get huge for modest changes, so
    "z > 4 for 5 minutes" alone would make opening one big app an Uh-oh. These floors
    keep Uh-oh for situations that really hurt (documented deviation from the concept).
    """
    if level <= Level.PSST:
        return True
    heads = level == Level.HEADS_UP
    if metric == "cpu":
        return x >= (30.0 if heads else 85.0)
    if metric == "ram":
        used_pct = 100.0 - sample.sys.ram_available_pct
        return used_pct >= (60.0 if heads else 85.0)
    if metric == "paging":
        return x >= (100.0 if heads else 500.0)
    if metric == "disk":
        return x >= (50.0 if heads else 90.0)
    if metric == "temp":
        return x >= (tjmax - 25.0 if heads else tjmax - 10.0)
    if metric == "discharge":
        return x >= (10.0 if heads else 25.0)
    return True


def min_std_for(metric: str, sample: Sample) -> float:
    base = METRICS[metric].min_std
    if metric == "ram" and sample.sys.ram_total:
        return max(base, 0.03 * sample.sys.ram_total / (1024 ** 3))  # 3% of RAM
    return base


@dataclass
class Signal:
    metric: str
    level: Level
    kind: str                       # "z" | "abs" | "trend" | "proc"
    value: float | None = None
    z: float | None = None
    mu: float | None = None
    sigma: float | None = None
    bucket_level: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        return (self.kind, self.metric, self.detail.get("rule"), self.detail.get("target"))


@dataclass
class DetectorResult:
    signals: list[Signal]
    z: dict[str, float | None]
    band: dict[str, tuple[float, float] | None]
    bucket_level: dict[str, str | None]
    gap: bool = False
    grace: bool = False


class SustainTracker:
    """Is a condition held for long enough? Tolerates the occasional dip (DET-6)."""

    def __init__(self, durations: dict[Level, float], frac: float, interval: float) -> None:
        self.durations = durations
        self.frac = frac
        self.interval = interval
        self.hist: deque[tuple[float, int]] = deque()
        self.start: float | None = None
        self.longest = max(durations.values()) if durations else 0.0

    def reset(self) -> None:
        self.hist.clear()
        self.start = None

    def push(self, mono: float, met: Level) -> Level:
        if self.start is None:
            self.start = mono
        self.hist.append((mono, int(met)))
        while self.hist and self.hist[0][0] < mono - self.longest - self.interval:
            self.hist.popleft()
        for level in sorted(self.durations, reverse=True):
            dur = self.durations[level]
            if mono - self.start < dur - self.interval / 2:
                continue
            cutoff = mono - dur if dur > 0 else mono - 1e-9  # exactly the last `dur` seconds of samples
            window = [lvl for m, lvl in self.hist if m > cutoff]
            if not window:
                continue
            above = sum(1 for lvl in window if lvl >= level)
            if above / len(window) >= self.frac:
                return level
        return Level.NONE


@dataclass
class AbsRule:
    name: str
    metric: str
    level: Level
    duration: float
    check: Callable[[Sample], bool | None]
    learning_only: bool = False


class Detector:
    def __init__(self, cfg: Config, baselines: BaselineStore) -> None:
        self.baselines = baselines
        self.prev_mono: float | None = None
        self.prev_ts: float | None = None
        self.grace_until: float = 0.0
        self.configure(cfg)

    def configure(self, cfg: Config) -> None:
        self.cfg = cfg
        self.zthr = cfg.z_thresholds()
        self.trackers = {m: SustainTracker(cfg.sustain_seconds(), cfg.sustain_frac, cfg.sample_interval_s)
                         for m in BASELINE_METRICS}
        self.abs_rules = self._abs_rules(cfg)
        self.abs_trackers: dict[tuple[str, str | None], SustainTracker] = {}

    def _abs_rules(self, cfg: Config) -> list[AbsRule]:
        def cpu(s: Sample):
            return s.sys.cpu_total

        return [
            AbsRule("ram_critical", "ram", Level.UH_OH, 60.0,
                    lambda s: s.sys.ram_available_pct < cfg.ram_critical_avail_pct if s.sys.ram_total else None),
            AbsRule("temp_high", "temp", Level.UH_OH, 30.0,
                    lambda s: None if s.sys.temp_c is None else s.sys.temp_c >= cfg.tjmax_c - cfg.temp_margin_c),
            AbsRule("throttle", "cpu", Level.HEADS_UP, 120.0,
                    lambda s: None if s.sys.perf_limit_pct is None or cpu(s) is None
                    else (cpu(s) >= 50.0 and s.sys.perf_limit_pct < cfg.throttle_limit_pct)),
            AbsRule("cpu_pegged", "cpu", Level.HEADS_UP, 600.0,
                    lambda s: None if cpu(s) is None else cpu(s) >= 95.0, learning_only=True),
            AbsRule("paging_storm", "paging", Level.HEADS_UP, 120.0,
                    lambda s: None if s.sys.paging_rate is None
                    else (s.sys.paging_rate >= 2000.0 and s.sys.ram_available_pct < 10.0), learning_only=True),
            AbsRule("disk_busy_abs", "disk", Level.HEADS_UP, 300.0,
                    lambda s: None if s.sys.disk_busy_pct is None else s.sys.disk_busy_pct >= 98.0, learning_only=True),
        ]

    def reset(self) -> None:
        for t in self.trackers.values():
            t.reset()
        for t in self.abs_trackers.values():
            t.reset()

    def _abs_tracker(self, name: str, target: str | None, level: Level, duration: float) -> SustainTracker:
        key = (name, target)
        t = self.abs_trackers.get(key)
        if t is None:
            t = self.abs_trackers[key] = SustainTracker({level: duration}, self.cfg.sustain_frac, self.cfg.sample_interval_s)
        return t

    def process(self, sample: Sample, slot: int, activity: str, day: int, *, learning: bool = False,
                excluded: bool = False, login_grace: bool = False) -> DetectorResult:
        cfg = self.cfg
        gap = False
        if self.prev_mono is not None:
            if (sample.mono - self.prev_mono > cfg.gap_s or sample.ts - self.prev_ts > cfg.gap_s
                    or sample.ts < self.prev_ts - 1.0):
                gap = True
                self.reset()
                self.grace_until = sample.mono + cfg.resume_grace_s
        self.prev_mono, self.prev_ts = sample.mono, sample.ts
        grace = login_grace or sample.mono < self.grace_until
        quiet = excluded or grace

        signals: list[Signal] = []
        zs: dict[str, float | None] = {}
        bands: dict[str, tuple[float, float] | None] = {}
        levels: dict[str, str | None] = {}

        for m in BASELINE_METRICS:
            info = METRICS[m]
            watch_only = m in WATCH_METRICS   # learns a band for the timeline, never alerts
            x = sample.metrics.get(m)
            tracker = self.trackers[m]
            if x is None:
                tracker.reset()
                zs[m] = bands[m] = levels[m] = None
                continue
            stat, level_name, bucket = self.baselines.lookup(slot, activity, m)
            floor_sd = min_std_for(m, sample)
            z = None
            if stat is not None:
                sd = max(stat.std, floor_sd)
                z = (x - stat.mean) / sd           # pre-update mean and sigma (DET-2)
                bands[m] = (stat.mean, sd)
            else:
                bands[m] = None
            zs[m] = z
            levels[m] = level_name

            met = Level.NONE
            if (stat is not None and not learning and not watch_only and z is not None
                    and (x - stat.mean) >= info.min_delta):
                for lv in (Level.UH_OH, Level.HEADS_UP, Level.PSST):
                    if z > self.zthr[lv] and level_floor_ok(m, lv, x, sample, cfg.tjmax_c):
                        met = lv
                        break
            if quiet:
                tracker.reset()
                active = Level.NONE
            else:
                active = tracker.push(sample.mono, met)
            if active:
                signals.append(Signal(m, active, "z", x, z, stat.mean if stat else None,
                                      bands[m][1] if bands[m] else None, level_name, {"bucket": bucket}))

            # --- contamination guard: anomalous readings never enter the baseline (DET-3)
            if quiet:
                continue
            guard_stat, guard_bucket = (stat, bucket) if stat is not None else self.baselines.provisional(slot, activity, m)
            guarded = bool(active)
            if guard_stat is not None and guard_stat.n >= cfg.seed_guard_samples:
                gz = (x - guard_stat.mean) / max(guard_stat.std, floor_sd)
                if gz > cfg.guard_z and (x - guard_stat.mean) >= 0.5 * info.min_delta:
                    guarded = True
            if guarded:
                if guard_bucket is not None:
                    self.baselines.note_guarded(guard_bucket, m, day)
            else:
                self.baselines.queue(sample.ts, slot, activity, m, x, day)

        # --- absolute limits, every sample (DET-8)
        for rule in self.abs_rules:
            if rule.learning_only and not learning and self.baselines.lookup(slot, activity, rule.metric)[0] is not None:
                self._abs_tracker(rule.name, None, rule.level, rule.duration).reset()
                continue
            tracker = self._abs_tracker(rule.name, None, rule.level, rule.duration)
            ok = rule.check(sample)
            if ok is None or quiet and rule.level < Level.UH_OH:
                tracker.reset()
                continue
            active = tracker.push(sample.mono, rule.level if ok else Level.NONE)
            if active:
                signals.append(Signal(rule.metric, active, "abs", sample.metrics.get(rule.metric),
                                      detail={"rule": rule.name}))

        for vol in sample.sys.volumes:
            if not vol.fixed or not vol.total:
                continue
            full = vol.free_pct < cfg.disk_full_pct or vol.free_gb < cfg.disk_full_gb
            low = vol.free_pct < cfg.disk_low_pct and vol.free_gb < cfg.disk_low_gb
            for name, level, cond in (("disk_full", Level.UH_OH, full), ("disk_low", Level.HEADS_UP, low and not full)):
                tracker = self._abs_tracker(name, vol.mount, level, 0.0)
                if tracker.push(sample.mono, level if cond else Level.NONE):
                    signals.append(Signal("disk_free", level, "abs", vol.free_gb,
                                          detail={"rule": name, "target": vol.mount, "free_pct": vol.free_pct,
                                                  "free_gb": vol.free_gb}))

        return DetectorResult(signals, zs, bands, levels, gap=gap, grace=grace)
