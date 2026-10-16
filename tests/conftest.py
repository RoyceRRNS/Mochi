"""Shared fixtures and builders for Mochi's tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mochi.config import Config  # noqa: E402
from mochi.model import GB, MB, App, Battery, Proc, Sample, SystemRaw, Volume, derive_metrics  # noqa: E402
from mochi.paths import Paths  # noqa: E402
from mochi.platform.fake import DEFAULT_START, FakeClock, FakePlatform  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never touch the real %LOCALAPPDATA%\\Mochi during tests."""
    monkeypatch.setenv("MOCHI_HOME", str(tmp_path / "mochi_home"))
    yield


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path / "profile").ensure()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(DEFAULT_START)


@pytest.fixture
def fake(clock) -> FakePlatform:
    return FakePlatform(clock)


def make_sys(**kw) -> SystemRaw:
    defaults = dict(cpu_total=5.0, cpu_per_core=[5.0] * 8, ram_total=16 * GB, ram_available=10 * GB, commit_pct=40.0,
                    paging_rate=5.0, disk_busy_pct=3.0, disk_latency_ms=1.0, cpu_perf_pct=110.0, perf_limit_pct=100.0,
                    idle_seconds=3.0, volumes=[Volume("C:\\", 512 * GB, 300 * GB, True)])
    defaults.update(kw)
    return SystemRaw(**defaults)


def make_app(key: str, private_mb: float = 100.0, cpu_core: float = 1.0, *, display: str | None = None,
             pid: int = 1000, ct: float = 1000.0, procs: int = 1, io_bps: float = 0.0, system: bool = False,
             ws_mb: float | None = None, me: bool = False) -> App:
    return App(key=key, display=display or key, exe_name=key.split(":")[0], pids=[pid], root_pid=pid,
               root_create_time=ct, cpu_core=cpu_core, cpu_total=cpu_core / 8, private_bytes=int(private_mb * MB),
               ws_private=int((ws_mb if ws_mb is not None else private_mb * 0.8) * MB), io_bps=io_bps,
               proc_count=procs, is_system=system, is_self=me)


def make_proc(pid: int, name: str, cpu_core: float = 0.0, private_mb: float = 50.0, app: str | None = None,
              ct: float = 1000.0, ppid: int = 4, io_bps: float | None = 0.0) -> Proc:
    return Proc(pid, ppid, ct, name, cpu_core, cpu_core / 8, int(private_mb * MB), int(private_mb * 0.8 * MB), io_bps, 0.0,
                1, app or name.lower())


def make_sample(ts: float = DEFAULT_START, mono: float = 10_000.0, sys: SystemRaw | None = None,
                apps: dict | None = None, procs: list | None = None, activity: str = "browsing", slot: int = 10) -> Sample:
    sys = sys or make_sys()
    s = Sample(ts=ts, mono=mono, sys=sys, procs=procs or [], apps=apps or {}, activity=activity, slot=slot)
    s.metrics = derive_metrics(sys)
    return s


@pytest.fixture
def builders():
    class B:
        sys = staticmethod(make_sys)
        app = staticmethod(make_app)
        proc = staticmethod(make_proc)
        sample = staticmethod(make_sample)
    return B
