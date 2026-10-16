"""A realistic warmed-up PC, built once and copied for every end-to-end scenario.

Template: three weekdays (Tue-Thu, 12-15 Oct 2026) of normal use 09:00-12:00, gaming
20:00-22:00 at high CPU, and an idle stretch with Windows busy-but-normal, all with
jitter. Scenarios then start on Friday 16 Oct 09:50, after the 3-day learning mode.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mochi.config import Config
from mochi.model import Battery
from mochi.platform.fake import FakeProc, local_ts
from mochi.replay import Simulation, normal_use

WARM_DAYS = (13, 14, 15)
TEST_START = local_ts(2026, 10, 16, 9, 50, 0)


def gaming(sim: Simulation) -> None:
    p = sim.platform
    if 6000 not in p.procs:
        p.add(FakeProc("eldenring.exe", 6000, 1000, cpu_core=420.0, private_mb=6000,
                       exe=r"D:\SteamLibrary\steamapps\common\ELDEN RING\Game\eldenring.exe"))
    p.set(foreground_pid=6000, fullscreen=True, fullscreen_d3d=True, idle_seconds=2.0)


def stop_gaming(sim: Simulation) -> None:
    sim.platform.remove(6000)
    sim.platform.set(fullscreen=False, fullscreen_d3d=False)


def idle_time(sim: Simulation) -> None:
    sim.platform.set(idle_seconds=1800.0, foreground_pid=2000)


def battery_session(sim: Simulation, watts: float = 10.0) -> None:
    sim.platform.set(battery=Battery(70.0, False, watts))


def build_template(root: Path) -> tuple[Path, float]:
    sim = Simulation(root, cfg=Config())
    sim.platform.noise = 0.06
    for day in WARM_DAYS:
        sim.jump_to(local_ts(2026, 10, day, 9, 0, 0))
        normal_use(sim)
        sim.platform.set(battery=Battery(90.0, True, 0.0))
        sim.run(3 * 3600)
        sim.jump_to(local_ts(2026, 10, day, 13, 0, 0))
        battery_session(sim)
        sim.run(3600)
        sim.platform.set(battery=Battery(90.0, True, 0.0))
        idle_time(sim)
        sim.run(3600)                                     # 14:00-15:00 away from the PC
        sim.platform.set(idle_seconds=3.0)
        sim.jump_to(local_ts(2026, 10, day, 20, 0, 0))
        gaming(sim)
        sim.run(2 * 3600)
        stop_gaming(sim)
    end = sim.clock.ts
    sim.engine.stop()
    return sim.paths.root, end


@pytest.fixture(scope="session")
def warm_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("warm_template")
    return build_template(root)


@pytest.fixture
def warm(warm_template, tmp_path):
    """A fresh copy of the warmed-up PC, positioned at Friday 09:50."""
    src, _ = warm_template
    dest = tmp_path / "profile"
    shutil.copytree(src, dest)
    sim = Simulation(tmp_path, cfg=Config(), start=TEST_START)
    sim.platform.noise = 0.06
    normal_use(sim)
    sim.platform.set(battery=Battery(90.0, True, 0.0))
    sim.run(600)  # 10 minutes of ordinary use to settle
    sim.clear_events()
    yield sim
    sim.engine.stop()
