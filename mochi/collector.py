"""Turn raw OS readings into a timestamped Sample with per-process rates."""

from __future__ import annotations

from typing import Callable

from .config import Config
from .grouping import Grouper
from .model import Proc, Sample, SystemRaw, derive_metrics
from .platform.base import Platform


class Collector:
    def __init__(self, platform: Platform, cfg: Config, augment: Callable[[SystemRaw], None] | None = None) -> None:
        self.platform = platform
        self.cfg = cfg
        self.augment = augment      # adds readings from outside the platform (the sensor helper)
        self.ncpu = max(1, platform.cpu_count())
        self.grouper = Grouper(platform.self_pid(), platform.cmdline)
        self._prev: dict[tuple[int, float], tuple[float, int | None, int | None]] = {}
        self._prev_mono: float | None = None
        self._prev_ts: float | None = None

    def reset(self) -> None:
        self._prev.clear()
        self._prev_mono = None
        self._prev_ts = None

    def collect(self, ts: float, mono: float, late_ms: float = 0.0) -> Sample:
        raws = self.platform.read_processes()
        sysraw = self.platform.read_system()
        self.platform.describe_foreground(sysraw, raws)
        if self.augment is not None:
            self.augment(sysraw)
        dt = (mono - self._prev_mono) if self._prev_mono is not None else None
        max_core = self.ncpu * 100.0
        procs: list[Proc] = []
        nxt: dict[tuple[int, float], tuple[float, int | None, int | None]] = {}
        for r in raws:
            if r.pid == 0:
                continue
            key = (r.pid, round(r.create_time, 3))
            prev = self._prev.get(key)
            cpu_core = 0.0
            io_bps = hf_ps = None
            if prev is not None and dt and dt > 0:
                cpu_core = max(0.0, (r.cpu_time - prev[0]) / dt * 100.0)
                if r.io_bytes is not None and prev[1] is not None:
                    io_bps = max(0.0, (r.io_bytes - prev[1]) / dt)
                if r.hard_faults is not None and prev[2] is not None:
                    hf_ps = max(0.0, (r.hard_faults - prev[2]) / dt)
            elif dt and self._prev_ts is not None and r.create_time >= self._prev_ts - 1.0:
                # Started since the last sample: average over its short life so far.
                life = max(0.5, ts - r.create_time)
                cpu_core = max(0.0, r.cpu_time / life * 100.0)
                if r.io_bytes is not None:
                    io_bps = r.io_bytes / life
                if r.hard_faults is not None:
                    hf_ps = r.hard_faults / life
            cpu_core = min(cpu_core, max_core)
            procs.append(
                Proc(r.pid, r.ppid, r.create_time, r.name, cpu_core, cpu_core / self.ncpu,
                     r.private_bytes, r.ws_private, io_bps, hf_ps, r.threads)
            )
            nxt[key] = (r.cpu_time, r.io_bytes, r.hard_faults)
        self._prev = nxt
        self._prev_mono = mono
        self._prev_ts = ts
        apps = self.grouper.group(procs)
        sample = Sample(ts=ts, mono=mono, sys=sysraw, procs=procs, apps=apps, late_ms=late_ms)
        sample.metrics = derive_metrics(sysraw)
        return sample
