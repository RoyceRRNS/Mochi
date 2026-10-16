"""Collector rate derivation (COL-1..11) and the scripted platform."""

import pytest

from mochi.collector import Collector
from mochi.config import Config
from mochi.model import GB, MB
from mochi.platform.base import AccessDenied, FileInfo, PlatformError, ProcessGone, classify_path
from mochi.platform.fake import FakeClock, FakePlatform, FakeProc


@pytest.fixture
def setup():
    clock = FakeClock()
    fake = FakePlatform(clock, ncpu=8)
    col = Collector(fake, Config())
    return clock, fake, col


def collect(clock, col, dt=5.0):
    clock.advance(dt)
    return col.collect(clock.time(), clock.monotonic())


class TestCollector:
    def test_cpu_rates_from_cumulative_counters(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("busy.exe", 500, cpu_core=100.0, private_mb=200, create_time=clock.time() - 100))
        collect(clock, col)                       # first reading: no rate yet
        s = collect(clock, col)
        p = next(p for p in s.procs if p.pid == 500)
        assert p.cpu_core == pytest.approx(100.0, abs=0.01)
        assert p.cpu_total == pytest.approx(12.5, abs=0.01)  # % of the whole machine
        assert p.private_bytes == 200 * MB

    def test_io_and_hard_fault_rates(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("disk.exe", 501, io_bps=50 * MB, hard_faults_ps=200, create_time=clock.time() - 100))
        collect(clock, col)
        p = next(p for p in collect(clock, col).procs if p.pid == 501)
        assert p.io_bps == pytest.approx(50 * MB, rel=0.01)
        assert p.hard_faults_ps == pytest.approx(200, rel=0.01)

    def test_new_process_gets_average_over_its_life(self, setup):
        clock, fake, col = setup
        collect(clock, col)
        fake.add(FakeProc("new.exe", 502, cpu_core=50.0, create_time=clock.time() + 2.5))
        s = collect(clock, col)
        p = next(p for p in s.procs if p.pid == 502)
        assert p.cpu_core > 0

    def test_vanished_process_drops_out(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("gone.exe", 503, cpu_core=10, create_time=clock.time() - 50))
        collect(clock, col)
        fake.remove(503)
        s = collect(clock, col)
        assert all(p.pid != 503 for p in s.procs)

    def test_pid_reuse_is_a_new_process(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("a.exe", 504, cpu_core=100, create_time=clock.time() - 50))
        collect(clock, col)
        collect(clock, col)
        fake.remove(504)
        fake.add(FakeProc("b.exe", 504, cpu_core=0.0, create_time=clock.time() + 1))
        s = collect(clock, col)
        p = next(p for p in s.procs if p.pid == 504)
        assert p.name == "b.exe" and p.cpu_core < 1.0  # not a bogus delta against a.exe's counters

    def test_cpu_capped_at_machine_size(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("x.exe", 505, cpu_core=5000.0, create_time=clock.time() - 50))
        collect(clock, col)
        p = next(p for p in collect(clock, col).procs if p.pid == 505)
        assert p.cpu_core == 800.0

    def test_metrics_apps_and_foreground(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("chrome.exe", 600, cpu_core=10, private_mb=500, create_time=clock.time() - 50))
        fake.set(foreground_pid=600)
        s = collect(clock, col, 5)
        assert s.sys.foreground_exe == "chrome.exe" and s.sys.foreground_path
        assert "chrome.exe" in s.apps and "mochi" in s.apps  # Mochi itself is tagged
        assert s.metrics["ram"] is not None and s.metrics["temp"] is None
        assert s.sys.unavailable["temp"].startswith("needs admin")

    def test_system_idle_process_never_listed(self, setup):
        clock, fake, col = setup
        s = collect(clock, col)
        assert all(p.pid != 0 for p in s.procs)

    def test_late_ms_recorded(self, setup):
        clock, fake, col = setup
        clock.advance(5)
        s = col.collect(clock.time(), clock.monotonic(), late_ms=1234.0)
        assert s.late_ms == 1234.0

    def test_reset_forgets_previous_counters(self, setup):
        clock, fake, col = setup
        fake.add(FakeProc("busy.exe", 507, cpu_core=100.0, create_time=clock.time() - 100))
        collect(clock, col)
        col.reset()
        p = next(p for p in collect(clock, col).procs if p.pid == 507)
        assert p.cpu_core == 0.0

    def test_read_failure_propagates_for_supervisor(self, setup):
        clock, fake, col = setup
        fake.fail_reads = 1
        with pytest.raises(PlatformError):
            collect(clock, col)
        collect(clock, col)  # next read works again


class TestFakePlatform:
    def test_ram_and_cpu_derivation(self):
        clock = FakeClock()
        f = FakePlatform(clock, ncpu=4, ram_gb=8)
        f.add(FakeProc("a.exe", 10, cpu_core=200, private_mb=1024))
        s = f.read_system()
        assert s.cpu_total == pytest.approx(2 + 200 / 4)
        assert s.ram_total == 8 * GB
        f.set(cpu=55.0, ram_used_gb=7.5)
        s = f.read_system()
        assert s.cpu_total == 55.0 and s.ram_used == pytest.approx(7.5 * GB, rel=1e-6)

    def test_noise_is_deterministic(self):
        def run():
            clock = FakeClock()
            f = FakePlatform(clock)
            f.noise = 0.1
            return [f.read_system().cpu_total for _ in range(5)]
        assert run() == run()

    def test_set_unknown_field_rejected(self):
        with pytest.raises(AttributeError):
            FakePlatform(FakeClock()).set(nonsense=1)

    def test_actions_and_errors(self):
        f = FakePlatform(FakeClock())
        f.add(FakeProc("a.exe", 10))
        f.close_exits.add(10)
        assert f.post_close(10) == 1 and 10 not in f.procs
        f.add(FakeProc("b.exe", 11))
        f.access_denied.add(11)
        with pytest.raises(AccessDenied):
            f.terminate(11)
        with pytest.raises(ProcessGone):
            f.terminate(12345)
        with pytest.raises(ProcessGone):
            f.set_low_priority(12345)
        with pytest.raises(PlatformError):
            f.open_location("")


def test_classify_path(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("TEMP", r"C:\Users\me\AppData\Local\Temp")
    assert classify_path(r"C:\Windows\System32\svchost.exe") == (True, False)
    assert classify_path(r"C:\Users\me\Downloads\setup.exe") == (False, True)
    assert classify_path(r"C:\Users\me\AppData\Local\Temp\x\y.exe") == (False, True)
    assert classify_path(r"C:\Program Files\App\app.exe") == (False, False)
    assert classify_path(None) == (False, False)


def test_fileinfo_unfamiliar_rules():
    assert not FileInfo(None).unfamiliar
    assert FileInfo("x", signed=False, company=None).unfamiliar
    assert not FileInfo("x", signed=False, company="Contoso").unfamiliar
    assert not FileInfo("x", signed=False, company=None, in_system_dir=True).unfamiliar
    assert FileInfo("x", signed=True, company="Contoso", in_temp_or_downloads=True).unfamiliar
    assert FileInfo("x").publisher == "unknown publisher"
