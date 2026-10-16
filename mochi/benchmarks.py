"""Benchmarks, Health Score and scheduling (BEN-1..8) [stretch].

Health Score formula (BEN-7), documented here and in the README:

    reference_t = median of the first 10 valid runs with the same power source and
                  power mode (needs >= 3 runs, otherwise the score is "still learning")
    ratio_t     = min(1.2, reference_t / current_t)    # faster than usual counts, a bit
    score       = min(100, 100 * sum(w_t * ratio_t) / sum(w_t))
    weights     = cpu 0.35, memory 0.25, disk write 0.20, disk read 0.20

Because the score is relative to this PC's own early runs, a PC that was already slow
when Mochi was installed still scores 100. "Re-baseline" starts a new reference after a
hardware change.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)

WEIGHTS = {"cpu_s": 0.35, "mem_s": 0.25, "disk_write_s": 0.20, "disk_read_s": 0.20}
TEST_LABELS = {"cpu_s": "CPU", "mem_s": "Memory", "disk_write_s": "Disk write", "disk_read_s": "Disk read"}


@dataclass
class BenchResult:
    ts: float
    cpu_s: float | None = None
    mem_s: float | None = None
    disk_write_s: float | None = None
    disk_read_s: float | None = None
    disk_mode: str | None = None
    on_ac: bool = True
    power_mode: str | None = None
    background_cpu: float | None = None
    temp: float | None = None
    score: float | None = None
    valid: bool = True
    scheduled: bool = False
    note: str = ""
    spread: dict = field(default_factory=dict)

    def condition(self) -> tuple[bool, str | None]:
        return (self.on_ac, self.power_mode)

    def to_row(self) -> tuple:
        return (self.ts, self.cpu_s, self.mem_s, self.disk_write_s, self.disk_read_s, self.disk_mode, int(self.on_ac),
                self.power_mode, self.background_cpu, self.temp, self.score, int(self.valid), int(self.scheduled),
                self.note)

    @classmethod
    def from_row(cls, row: tuple) -> "BenchResult":
        (_id, ts, cpu, mem, dw, dr, mode, on_ac, pm, bg, temp, score, valid, sched, note) = row
        return cls(ts, cpu, mem, dw, dr, mode, bool(on_ac), pm, bg, temp, score, bool(valid), bool(sched), note or "")


class HealthScore:
    def __init__(self, ref_runs: int = 10, min_runs: int = 3, cap_ratio: float = 1.2) -> None:
        self.ref_runs = ref_runs
        self.min_runs = min_runs
        self.cap_ratio = cap_ratio

    def reference(self, history: list[BenchResult], cond: tuple[bool, str | None],
                  since: float = 0.0) -> dict[str, float] | None:
        """Median of the first ``ref_runs`` valid runs taken under the same conditions."""
        runs = sorted((r for r in history if r.valid and r.condition() == cond and r.ts >= since), key=lambda r: r.ts)
        runs = runs[: self.ref_runs]
        if len(runs) < self.min_runs:
            return None
        ref: dict[str, float] = {}
        for t in WEIGHTS:
            vals = [getattr(r, t) for r in runs if getattr(r, t)]
            if len(vals) >= self.min_runs:
                ref[t] = statistics.median(vals)
        return ref or None

    def score(self, result: BenchResult, ref: dict[str, float] | None) -> float | None:
        if ref is None or not result.valid:
            return None
        num = den = 0.0
        for t, w in WEIGHTS.items():
            cur = getattr(result, t)
            if cur and t in ref:
                num += w * min(self.cap_ratio, ref[t] / cur)
                den += w
        if den == 0:
            return None
        return round(min(100.0, 100.0 * num / den), 1)

    @staticmethod
    def should_suggest(scores: list[float | None], threshold: float, declined_ts: float | None, now: float,
                       decline_days: float) -> bool:
        """Suggest a check-up only after 2 low scores in a row, and not within 7 days of a "no" (BEN-8)."""
        valid = [s for s in scores if s is not None]
        if len(valid) < 2 or not (valid[-1] < threshold and valid[-2] < threshold):
            return False
        return declined_ts is None or now - declined_ts >= decline_days * 86400


class BenchmarkScheduler:
    """Daily for the first 14 days (so >= 10 reference runs exist early), then weekly (BEN-6)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def interval(self, now: float, first_ts: float | None) -> float:
        if first_ts is None or now - first_ts < self.cfg.bench_daily_days * 86400:
            return 20 * 3600.0
        return 7 * 86400.0

    def due(self, now: float, first_ts: float | None, last_ts: float | None) -> bool:
        if last_ts is None:
            return True
        return now - last_ts >= self.interval(now, first_ts)

    @staticmethod
    def can_run_now(user_idle: bool, on_ac: bool | None, fullscreen: bool) -> bool:
        # A desktop has no battery: on_ac None counts as plugged in.
        return user_idle and (on_ac is None or on_ac) and not fullscreen

    def overdue(self, now: float, first_ts: float | None, last_ts: float | None) -> bool:
        if last_ts is None:
            return first_ts is not None and now - first_ts > 3 * 86400
        return now - last_ts > 3 * self.interval(now, first_ts)


def cleanup_leftovers(folder: Path) -> int:
    """Remove temp files left by a benchmark that was killed mid-run (BEN-5)."""
    n = 0
    if folder.exists():
        for p in folder.glob("mochi_bench*.tmp"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


class BenchmarkRunner:
    def __init__(self, cfg: Config, tmp_dir: Path, python: str | None = None, scale: float = 1.0,
                 disk_mb: int | None = None) -> None:
        self.cfg = cfg
        self.tmp_dir = Path(tmp_dir)
        self.python = python or sys.executable
        self.scale = scale
        self.disk_mb = disk_mb or cfg.bench_disk_mb
        self.health = HealthScore(cfg.bench_ref_runs)
        self.proc: subprocess.Popen | None = None

    def _cmd(self, tests: list[str]) -> list[str]:
        return [self.python, "-m", "mochi.bench_worker", "--tests", ",".join(tests), "--reps", str(self.cfg.bench_reps),
                "--dir", str(self.tmp_dir), "--disk-mb", str(self.disk_mb), "--scale", str(self.scale)]

    def run(self, *, scheduled: bool, free_gb: float | None, on_ac: bool | None, power_mode: str | None,
            background_cpu: float | None, temp: float | None,
            abort_check: Callable[[], str | None] | None = None, timeout_s: float = 90.0) -> BenchResult:
        res = BenchResult(time.time(), on_ac=on_ac if on_ac is not None else True, power_mode=power_mode,
                          background_cpu=background_cpu, temp=temp, scheduled=scheduled)
        if scheduled and background_cpu is not None and background_cpu > 10.0:
            res.valid, res.note = False, f"skipped: background CPU was {background_cpu:.0f}%"
            return res
        tests = ["cpu", "mem"]
        if free_gb is None or free_gb >= self.cfg.bench_min_free_gb:
            tests.append("disk")
        else:
            res.note = f"disk test skipped: only {free_gb:.1f} GB free"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(self._cmd(tests), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=flags, env=env, text=True)
        start = time.monotonic()
        try:
            while self.proc.poll() is None:
                if abort_check is not None and (reason := abort_check()):
                    self.proc.kill()
                    self.proc.wait(5)
                    res.valid, res.note = False, f"stopped: {reason}"
                    cleanup_leftovers(self.tmp_dir)
                    return res
                if time.monotonic() - start > timeout_s:
                    self.proc.kill()
                    self.proc.wait(5)
                    res.valid, res.note = False, "stopped: took too long"
                    cleanup_leftovers(self.tmp_dir)
                    return res
                time.sleep(0.25)
            out, err = self.proc.communicate(timeout=5)
        finally:
            self.proc = None
        if not out.strip():
            res.valid, res.note = False, f"benchmark failed: {err.strip()[-200:]}"
            return res
        data = json.loads(out.strip().splitlines()[-1])
        for key, attr in (("cpu", "cpu_s"), ("mem", "mem_s"), ("disk_write", "disk_write_s"), ("disk_read", "disk_read_s")):
            vals = data.get(key)
            if vals:
                med = statistics.median(vals)
                setattr(res, attr, med)
                res.spread[attr] = (max(vals) - min(vals)) / med if med else 0.0
        res.disk_mode = data.get("disk_mode")
        return res

    def load_test(self, seconds: float, procs: int | None = None) -> subprocess.Popen:
        cmd = [self.python, "-m", "mochi.bench_worker", "--load", str(seconds)]
        if procs:
            cmd += ["--procs", str(procs)]
        flags = 0x08000000 if sys.platform == "win32" else 0
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, env=env,
                                text=True)


def result_dict(r: BenchResult) -> dict:
    return asdict(r)
