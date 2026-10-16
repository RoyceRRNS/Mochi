"""Trend detectors that run on their own schedule (critique problem 3, DET-10).

A slow leak or a filling disk rarely trips a z-score, so these look at slopes:

- Leak (DIA-7): an app's private memory rising steadily with no real drops.
  Fast leaks: >= 15 min and >= 500 MB of growth. Slow leaks: >= 60 min at >= 100 MB/h.
- Disk filling (DIA-9): free space falling steadily for >= 6 h, projected full in < 7 days.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .config import Config
from .detector import Signal
from .model import MB, App, Level, Volume


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares. Returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sxy / sxx
    intercept = my - slope * mx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 1.0
    return slope, intercept, r2


def max_drop_fraction(ys: list[float]) -> float:
    """Largest fall from a running peak, as a fraction of that peak."""
    peak = 0.0
    worst = 0.0
    for y in ys:
        peak = max(peak, y)
        if peak > 0:
            worst = max(worst, (peak - y) / peak)
    return worst


@dataclass
class LeakFinding:
    app: str
    display: str
    rate_mb_h: float
    growth_mb: float
    span_min: float
    r2: float
    current_mb: float


class LeakTracker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.series: dict[str, deque[tuple[float, float]]] = {}
        self.display: dict[str, str] = {}
        self.roots: dict[str, tuple[int, float]] = {}
        self.last_seen: dict[str, float] = {}

    def observe(self, ts: float, apps: dict[str, App]) -> None:
        for key, app in apps.items():
            if app.is_self or app.is_system:
                continue
            root = (app.root_pid, round(app.root_create_time, 1))
            if self.roots.get(key) != root:
                # The app restarted: its memory history starts again.
                self.series[key] = deque(maxlen=240)
                self.roots[key] = root
            self.series[key].append((ts, app.private_bytes / MB))
            self.display[key] = app.display
            self.last_seen[key] = ts
        for key in [k for k, t in self.last_seen.items() if ts - t > 600]:
            for d in (self.series, self.display, self.roots, self.last_seen):
                d.pop(key, None)

    def growing_for(self, key: str) -> float:
        """Seconds since this app's memory last dropped noticeably (for Detective)."""
        s = self.series.get(key)
        if not s or len(s) < 3:
            return 0.0
        vals = list(s)
        start_idx = 0
        peak = 0.0
        for i, (_t, v) in enumerate(vals):
            if peak and v < peak * (1 - self.cfg.leak_max_drop):
                start_idx = i
                peak = v
            peak = max(peak, v)
        tail = vals[start_idx:]
        if len(tail) < 3:
            return 0.0
        span = tail[-1][0] - tail[0][0]
        growth = tail[-1][1] - tail[0][1]
        # Only call it "growing" when memory really rose (>= 50 MB at >= 10 MB/h).
        if span <= 0 or growth < 50.0 or growth / (span / 3600.0) < 10.0:
            return 0.0
        return span

    def findings(self) -> list[LeakFinding]:
        cfg = self.cfg
        out: list[LeakFinding] = []
        ignore = set(cfg.leak_ignore)
        by_design = set(cfg.grow_by_design)
        for key, s in self.series.items():
            if len(s) < 4 or key.split(":")[0] in ignore:
                continue
            factor = cfg.grow_by_design_factor if key.split(":")[0] in by_design else 1.0
            ts = [t for t, _ in s]
            mb = [v for _, v in s]
            # Look at the tail since the last real drop.
            start_idx = 0
            peak = 0.0
            for i, v in enumerate(mb):
                if peak and v < peak * (1 - cfg.leak_max_drop):
                    start_idx = i
                    peak = v
                peak = max(peak, v)
            ts, mb = ts[start_idx:], mb[start_idx:]
            if len(ts) < 4:
                continue
            span_min = (ts[-1] - ts[0]) / 60.0
            slope, _, r2 = linear_fit([t - ts[0] for t in ts], mb)
            rate = slope * 3600.0
            growth = mb[-1] - mb[0]
            if r2 < cfg.leak_r2 or max_drop_fraction(mb) > cfg.leak_max_drop or rate <= 0:
                continue
            fast = span_min >= cfg.leak_fast_window_min and growth >= cfg.leak_fast_growth_mb * factor \
                and rate >= cfg.leak_rate_mb_h * factor
            slow = span_min >= cfg.leak_slow_window_min and rate >= cfg.leak_rate_mb_h * factor
            if fast or slow:
                out.append(LeakFinding(key, self.display.get(key, key), rate, growth, span_min, r2, mb[-1]))
        return out

    def signals(self) -> list[Signal]:
        return [
            Signal("ram", Level.HEADS_UP, "trend", f.current_mb,
                   detail={"rule": "leak", "target": f.app, "rate_mb_h": f.rate_mb_h, "growth_mb": f.growth_mb,
                           "span_min": f.span_min, "r2": f.r2, "display": f.display})
            for f in self.findings()
        ]


class DiskTrend:
    def __init__(self, cfg: Config, min_span_h: float = 6.0, horizon_days: float = 7.0, max_free_pct: float = 30.0) -> None:
        self.cfg = cfg
        self.min_span_h = min_span_h
        self.horizon_days = horizon_days
        self.max_free_pct = max_free_pct
        self.series: dict[str, deque[tuple[float, float]]] = {}
        self.totals: dict[str, int] = {}

    def observe(self, ts: float, volumes: list[Volume]) -> None:
        for v in volumes:
            if not v.fixed:
                continue
            self.series.setdefault(v.mount, deque(maxlen=1440)).append((ts, float(v.free)))
            self.totals[v.mount] = v.total

    def signals(self) -> list[Signal]:
        out: list[Signal] = []
        for mount, s in self.series.items():
            if len(s) < 10:
                continue
            span_h = (s[-1][0] - s[0][0]) / 3600.0
            if span_h < self.min_span_h:
                continue
            ts = [t - s[0][0] for t, _ in s]
            free = [v for _, v in s]
            slope, _, r2 = linear_fit(ts, free)
            total = self.totals.get(mount) or 1
            free_pct = 100.0 * free[-1] / total
            if slope >= 0 or r2 < 0.6 or free_pct >= self.max_free_pct:
                continue
            days_left = free[-1] / (-slope) / 86400.0
            if days_left < self.horizon_days:
                out.append(Signal("disk_free", Level.HEADS_UP, "trend", free[-1] / (1024 ** 3),
                                  detail={"rule": "disk_projection", "target": mount, "days_left": days_left,
                                          "free_pct": free_pct, "free_gb": free[-1] / (1024 ** 3),
                                          "rate_gb_day": -slope * 86400 / (1024 ** 3)}))
        return out


class RunawayTracker:
    """Per-process CPU history for the runaway rule (DIA-5, critique problem 6).

    A stuck single-threaded script is only ~3% of a 32-thread CPU, so it never trips the
    system CPU z-score. This tracker flags any background process holding >= 90% of one
    core (or >= 50% of the machine) for the runaway window.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.hist: dict[tuple[int, float], deque[tuple[float, float, float]]] = {}
        self.meta: dict[tuple[int, float], tuple[str, str, int]] = {}
        self.foreground_app: str | None = None

    def observe(self, mono: float, procs, foreground_app: str | None) -> None:
        keep = self.cfg.runaway_s + 2 * self.cfg.sample_interval_s
        seen = set()
        for p in procs:
            if p.cpu_core < 20.0 and p.ident not in self.hist:
                continue
            key = p.ident
            seen.add(key)
            h = self.hist.setdefault(key, deque())
            h.append((mono, p.cpu_core, p.cpu_total))
            while h and h[0][0] < mono - keep:
                h.popleft()
            self.meta[key] = (p.app, p.name, p.pid)
        for key in [k for k in self.hist if k not in seen]:
            self.hist.pop(key, None)
            self.meta.pop(key, None)
        self.foreground_app = foreground_app

    def history(self, key: tuple[int, float]) -> list[tuple[float, float, float]]:
        return list(self.hist.get(key, ()))

    def findings(self, mono: float, apps) -> list[dict]:
        cfg = self.cfg
        out = []
        for key, h in self.hist.items():
            if not h or mono - h[0][0] < cfg.runaway_s - cfg.sample_interval_s / 2:
                continue
            window = [(c, t) for m, c, t in h if m > mono - cfg.runaway_s - cfg.sample_interval_s / 2]
            if not window:
                continue
            hot = sum(1 for c, t in window if c >= cfg.runaway_core_pct or t >= cfg.runaway_total_pct)
            if hot / len(window) < cfg.sustain_frac:
                continue
            app_key, name, pid = self.meta[key]
            app = apps.get(app_key)
            if app is None or app.is_self or app_key == self.foreground_app:
                continue
            out.append({"key": key, "app": app_key, "name": name, "pid": pid,
                        "cpu_core": window[-1][0], "cpu_total": window[-1][1],
                        "minutes": (h[-1][0] - h[0][0]) / 60.0})
        return out

    def signals(self, mono: float, apps) -> list[Signal]:
        return [
            Signal("cpu", Level.HEADS_UP, "proc", f["cpu_core"],
                   detail={"rule": "runaway", "target": f["app"], "pid": f["pid"], "minutes": f["minutes"],
                           "cpu_core": f["cpu_core"], "cpu_total": f["cpu_total"]})
            for f in self.findings(mono, apps)
        ]
