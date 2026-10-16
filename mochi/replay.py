"""Replay / simulation harness (TST-2).

Drives the real Engine with a scripted platform and a fake clock, faster than real time.
The same input always produces the same alerts, so baselines and detection can be tested
in seconds instead of days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .engine import Engine, EngineEvent, SampleView
from .platform.fake import DEFAULT_START, FakeClock, FakePlatform, FakeProc, local_ts
from .paths import Paths


@dataclass
class Simulation:
    root: Path
    cfg: Config = field(default_factory=Config)
    start: float = DEFAULT_START
    ncpu: int = 8
    ram_gb: float = 16.0
    interval: float = 5.0

    def __post_init__(self) -> None:
        self.clock = FakeClock(self.start)
        self.platform = FakePlatform(self.clock, ncpu=self.ncpu, ram_gb=self.ram_gb,
                                     boot_time=self.start - 6 * 3600)
        self.paths = Paths(Path(self.root) / "profile").ensure()
        self.events: list[EngineEvent] = []
        self.engine = self._make_engine()

    def _make_engine(self) -> Engine:
        eng = Engine(self.platform, self.paths, self.cfg, threaded=False, clock=self.clock, start_threads=False,
                     startup_watcher=False, bench_scale=0.05, bench_disk_mb=16)
        eng.subscribe(self.events.append)
        return eng

    def restart(self) -> Engine:
        """Simulate quitting and starting Mochi again on the same database."""
        self.engine.stop()
        self.events.clear()
        self.engine = self._make_engine()
        return self.engine

    # ---------------------------------------------------------------- driving
    def step(self, seconds: float | None = None) -> SampleView:
        self.clock.advance(seconds or self.interval)
        return self.engine.tick()

    def run(self, seconds: float, each: Callable[[float, SampleView], None] | None = None) -> list[SampleView]:
        views = []
        steps = int(round(seconds / self.interval))
        for i in range(steps):
            v = self.step()
            views.append(v)
            if each:
                each((i + 1) * self.interval, v)
        return views

    def jump_to(self, ts: float) -> None:
        """Move the clock forward without sampling (like the PC being off or asleep)."""
        gap = ts - self.clock.ts
        if gap < 0:
            raise ValueError("can't go back in time")
        self.clock.advance(gap)

    def warm_up(self, days: list[int], hours: tuple[int, int] = (9, 12), base_month: int = 10, base_year: int = 2026,
                setup: Callable[["Simulation"], None] | None = None) -> None:
        """Simulate normal use between ``hours`` on each listed day of the month."""
        for day in days:
            self.jump_to(local_ts(base_year, base_month, day, hours[0], 0, 0))
            if setup:
                setup(self)
            self.run((hours[1] - hours[0]) * 3600)

    # ---------------------------------------------------------------- queries
    def alert_events(self, kinds=("opened", "escalated")) -> list:
        return [e.data for e in self.events if e.kind == "alert" and e.data.kind in kinds]

    def opened(self) -> list:
        return [e.episode for e in self.alert_events(("opened",))]

    def popups(self) -> list:
        return [e for e in self.alert_events(("opened", "escalated")) if e.notify == "popup"]

    def clear_events(self) -> None:
        self.events.clear()


def add_browser(sim: Simulation, base_pid: int = 2000, tabs: int = 6, mb_each: float = 250.0,
                cpu_each: float = 2.0) -> list[FakeProc]:
    """A multi-process browser: one root and several helpers with the same exe name."""
    root = sim.platform.add(FakeProc("chrome.exe", base_pid, 1000, cpu_core=cpu_each, private_mb=mb_each,
                                     exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe"))
    out = [root]
    for i in range(1, tabs):
        out.append(sim.platform.add(FakeProc("chrome.exe", base_pid + i, base_pid, cpu_core=cpu_each,
                                             private_mb=mb_each, exe=root.exe)))
    return out


def normal_use(sim: Simulation) -> None:
    """A typical light session: a browser in front and a couple of background apps."""
    p = sim.platform
    if 2000 not in p.procs:
        add_browser(sim)
    if 3000 not in p.procs:
        p.add(FakeProc("Code.exe", 3000, 1000, cpu_core=4.0, private_mb=600))
    if 3100 not in p.procs:
        p.add(FakeProc("OneDrive.exe", 3100, 1000, cpu_core=0.5, private_mb=120))
    p.set(foreground_pid=2000, idle_seconds=3.0, cpu=None, ram_used_gb=None, paging_rate=5.0, disk_busy_pct=3.0)
