"""Real-machine end-to-end tests (light and capped, TST-5).

These use the real Windows adapter, real processes and the real fault-injection scripts:
about 25 s of single-core CPU load, a 2 GB allocation, a 1 GB temp-file loop and a short
leak. Every script stops itself and cleans up.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from mochi.actions import ActionRunner
from mochi.config import Config
from mochi.engine import Engine
from mochi.paths import Paths

pytestmark = [pytest.mark.realmachine, pytest.mark.skipif(sys.platform != "win32", reason="Windows adapter")]

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
PY = sys.executable


@pytest.fixture(scope="module")
def win():
    from mochi.platform.windows import WindowsPlatform

    return WindowsPlatform()


def env_with_home(home: Path) -> dict:
    env = dict(os.environ)
    env["MOCHI_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    return env


def spawn_tool(name: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([PY, str(TOOLS / name), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            creationflags=0x08000000)


def real_engine(tmp_path, **cfg_kw) -> Engine:
    """The engine runs inside pytest, which also starts the fault scripts. In real use you start
    those scripts yourself, so the engine must not treat them as Mochi's own children."""
    from mochi.platform.windows import WindowsPlatform

    plat = WindowsPlatform()
    plat._self_pid = -1
    cfg = Config(sample_interval_s=1.0, cold_start_days=0.0, **cfg_kw)
    return Engine(plat, Paths(tmp_path / "real").ensure(), cfg, threaded=False, start_threads=False,
                  startup_watcher=False)


def tick_for(engine: Engine, seconds: float, until=None):
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        v = engine.tick()
        if until is not None and until(v):
            return v
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0) % 1.0))
    return None


# ------------------------------------------------------------- readings
class TestWindowsAdapter:
    def test_system_readings_are_sane(self, win):
        win.read_system()
        time.sleep(1.1)
        s = win.read_system()
        assert 0 <= s.cpu_total <= 100 and len(s.cpu_per_core) == psutil.cpu_count()
        assert s.ram_total > 0 and 0 < s.ram_available <= s.ram_total
        assert s.commit_pct is not None and 0 < s.commit_pct <= 100
        assert s.paging_rate is not None and s.paging_rate >= 0
        assert s.disk_busy_pct is not None and 0 <= s.disk_busy_pct <= 100
        assert s.perf_limit_pct is not None and s.cpu_perf_pct is not None
        assert s.ctx_switches > 0 and s.interrupts > 0
        assert s.idle_seconds >= 0
        assert s.volumes and all(v.fixed and v.total > 0 for v in s.volumes)
        if s.temp_c is None:
            assert "temp" in s.unavailable and s.unavailable["temp"]  # unavailable with a reason, never 0
        if s.battery is not None:
            assert 0 <= s.battery.percent <= 100 and s.battery.discharge_w is not None

    def test_process_list_matches_psutil_and_is_fast(self, win):
        t0 = time.process_time()
        procs = win.read_processes()
        cost = time.process_time() - t0
        ours = {p.pid for p in procs}
        theirs = set(psutil.pids())
        assert len(ours & theirs) >= 0.95 * len(theirs)
        me = next(p for p in procs if p.pid == os.getpid())
        assert me.private_bytes > 1_000_000 and 0 < me.ws_private <= me.private_bytes * 2
        assert abs(me.create_time - psutil.Process().create_time()) < 1.0
        assert cost < 0.1  # one syscall; psutil per-process scans cost ~0.4 s here

    def test_file_info_signature_and_publisher(self, win):
        info = win.file_info(sys.executable)
        assert info.company and not info.unfamiliar
        sysfile = win.file_info(os.path.join(os.environ["SystemRoot"], "System32", "notepad.exe"))
        assert sysfile.in_system_dir and not sysfile.unfamiliar
        assert win.file_info(None).path is None

    def test_environment_queries(self, win):
        assert isinstance(win.reduce_motion(), bool) and isinstance(win.high_contrast(), bool)
        pm = win.power_mode()
        assert pm is None or isinstance(pm, str)
        items = win.startup_items()
        assert all(i.source for i in items)
        disks, reason = win.disk_health()
        assert disks or reason
        bh = win.battery_health()
        assert bh is None or bh.design_mwh or bh.full_mwh


# ---------------------------------------------------------------- actions
def sleeper(seconds=60):
    return subprocess.Popen([PY, "-c", f"import time; time.sleep({seconds})"], creationflags=0x08000000)


class TestRealActions:
    def test_end_process_and_pid_reuse_guard(self, win):
        p = sleeper()
        try:
            ct = psutil.Process(p.pid).create_time()
            runner = ActionRunner(win, grace_s=1.0)
            refused = runner.end_process(p.pid, ct - 100, "python.exe")
            assert not refused.ok and p.poll() is None   # wrong start time: never kill
            res = runner.end_process(p.pid, ct, "python.exe")
            assert res.ok
            p.wait(5)
        finally:
            if p.poll() is None:
                p.kill()

    def test_lower_priority(self, win):
        p = sleeper()
        try:
            ct = psutil.Process(p.pid).create_time()
            assert ActionRunner(win).lower_priority(p.pid, ct, "python.exe").ok
            assert psutil.Process(p.pid).nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
        finally:
            p.kill()
            p.wait(5)

    def test_protected_process_never_ended(self, win):
        csrss = next((p for p in psutil.process_iter(["name", "create_time"]) if p.info["name"] == "csrss.exe"), None)
        if csrss is None:
            pytest.skip("csrss.exe not visible")
        res = ActionRunner(win).end_process(csrss.pid, csrss.info["create_time"], "csrss.exe")
        assert not res.ok and "part of Windows" in res.message


# ------------------------------------------------------------------ CLI
class TestCli:
    def test_headless_run_writes_a_consistent_db(self, tmp_path):
        home = tmp_path / "home"
        r = subprocess.run([PY, "-m", "mochi", "--headless", "--seconds", "12", "--profile", "e2e"], env=env_with_home(home),
                           capture_output=True, text=True, timeout=90)
        assert r.returncode == 0, r.stderr
        db = home / "e2e" / "mochi.db"
        con = sqlite3.connect(db)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 2
        assert con.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] > 0
        con.close()
        assert (home / "e2e" / "logs" / "mochi.log").exists()

    def test_second_instance_is_refused(self, tmp_path):
        home = tmp_path / "home"
        env = env_with_home(home)
        first = subprocess.Popen([PY, "-m", "mochi", "--headless", "--seconds", "25", "--profile", "solo"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if (home / "solo" / "mochi.lock").exists():
                    break
                time.sleep(0.1)
            time.sleep(1.0)
            second = subprocess.run([PY, "-m", "mochi", "--headless", "--seconds", "5", "--profile", "solo"], env=env,
                                    capture_output=True, text=True, timeout=60)
            assert second.returncode == 3 and "already running" in second.stderr
        finally:
            first.kill()
            first.wait(10)

    def test_invalid_profile_name(self, tmp_path):
        r = subprocess.run([PY, "-m", "mochi", "--headless", "--profile", "../x"], env=env_with_home(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 2

    def test_hard_kill_keeps_data_consistent(self, tmp_path):
        home = tmp_path / "home"
        env = env_with_home(home)
        subprocess.run([PY, "-m", "mochi", "--headless", "--seconds", "8", "--profile", "kill"], env=env, timeout=90,
                       check=True, capture_output=True)
        con = sqlite3.connect(home / "kill" / "mochi.db")
        baselines_before = con.execute("SELECT COUNT(*) FROM baselines").fetchone()[0]
        con.close()
        p = subprocess.Popen([PY, "-m", "mochi", "--headless", "--seconds", "120", "--profile", "kill"], env=env)
        time.sleep(40)
        killed_at = time.time()
        p.kill()   # like Task Manager > End task (TerminateProcess)
        p.wait(10)
        con = sqlite3.connect(home / "kill" / "mochi.db")
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] >= baselines_before > 0
        last = con.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        assert killed_at - last <= 60.0   # at most one batch (30 s) plus a sample interval was lost
        con.close()


# --------------------------------------------------------------- benchmark
def test_real_benchmark_is_unbuffered_and_cleans_up(tmp_path):
    from mochi.benchmarks import BenchmarkRunner

    r = BenchmarkRunner(Config(bench_reps=2), tmp_path, scale=0.2, disk_mb=64)
    res = r.run(scheduled=False, free_gb=100.0, on_ac=True, power_mode="balanced", background_cpu=2.0, temp=None)
    assert res.valid and res.disk_mode == "unbuffered"
    assert all(getattr(res, t) > 0 for t in ("cpu_s", "mem_s", "disk_write_s", "disk_read_s"))
    assert not list(tmp_path.glob("mochi_bench*.tmp"))


# --------------------------------------------------------- fault injection
class TestFaultInjection:
    def test_single_core_runaway_is_caught_and_named(self, tmp_path):
        eng = real_engine(tmp_path, runaway_s=12.0)
        burner = spawn_tool("cpu_burner.py", "--threads", "1", "--seconds", "30")
        try:
            tick_for(eng, 3)
            seen = tick_for(eng, 26, until=lambda v: any(e.rule == "runaway" for e in eng.alerts.active()))
            assert seen is not None, "runaway not detected"
            ep = next(e for e in eng.alerts.active() if e.rule == "runaway")
            assert ep.diagnosis.culprit.app == "python.exe:cpu_burner.py"
            assert "cpu_burner.py" in ep.diagnosis.cause
        finally:
            burner.kill()
            burner.wait(10)
            eng.stop()

    def test_memory_hog_is_attributed_in_detective(self, tmp_path):
        eng = real_engine(tmp_path)
        hog = spawn_tool("memory_hog.py", "--gb", "2", "--ramp-s", "4", "--hold-s", "20")
        try:
            def found(_v):
                rows = eng.detective()
                r = next((x for x in rows if x["app"] == "python.exe:memory_hog.py"), None)
                return r is not None and r["private"] >= 1.8 * 1024 ** 3
            assert tick_for(eng, 25, until=found) is not None
            rows = eng.detective()
            total_in_ram = sum(r["in_ram"] for r in rows)
            assert total_in_ram <= psutil.virtual_memory().total  # QRK-1: never more than physical RAM
            assert any(r.get("prime") for r in rows)
        finally:
            hog.kill()
            hog.wait(10)
            eng.stop()

    def test_disk_thrash_is_attributed(self, tmp_path):
        eng = real_engine(tmp_path)
        thrash = spawn_tool("disk_thrash.py", "--mb", "1024", "--seconds", "20", "--dir", str(tmp_path))
        best = {"io": 0.0, "busy": 0.0}
        try:
            def watch(v):
                s = eng.history[-1]
                app = s.apps.get("python.exe:disk_thrash.py")
                if app:
                    best["io"] = max(best["io"], app.io_bps)
                best["busy"] = max(best["busy"], s.sys.disk_busy_pct or 0.0)
                return best["io"] > 50 * 1024 ** 2 and best["busy"] > 20
            assert tick_for(eng, 25, until=watch) is not None, best
        finally:
            thrash.kill()
            thrash.wait(10)
            eng.stop()
        for _ in range(50):
            if not list(tmp_path.glob("mochi_thrash_*")):
                break
            time.sleep(0.1)

    def test_fast_leak_is_found(self, tmp_path):
        eng = real_engine(tmp_path, app_snapshot_s=2.0, trend_eval_s=2.0, leak_fast_window_min=0.3,
                          leak_fast_growth_mb=150.0)
        leak = spawn_tool("leaky.py", "--mb-per-min", "900", "--minutes", "1")
        try:
            seen = tick_for(eng, 40, until=lambda v: "python.exe:leaky.py" in eng.leak_map)
            assert seen is not None, "leak not detected"
            assert eng.leak_map["python.exe:leaky.py"]["rate_mb_h"] > 10_000
        finally:
            leak.kill()
            leak.wait(10)
            eng.stop()


# --------------------------------------------------------------- overhead
@pytest.mark.slow
def test_headless_overhead_is_small(tmp_path):
    """PERF-1/PERF-2 quick check: 30 s of the real engine, measured from outside."""
    sys.path.insert(0, str(TOOLS))
    from measure_overhead import measure

    env = env_with_home(tmp_path / "home")
    old = dict(os.environ)
    os.environ.update(env)
    try:
        res = measure([PY, "-m", "mochi", "--headless", "--seconds", "60", "--profile", "ovh"], seconds=30, warmup=8)
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert res["samples"] >= 20
    assert res["cpu_avg_pct_of_total"] < 2.0, res
    assert res["private_mb_peak"] < 100.0, res


# ---------------------------------------------------------- startup timing
def test_startup_watcher_times_a_real_launch(tmp_path):
    from mochi.startup_tracker import StartupWatcher

    got = []
    # self_pid=-1: pytest starts the dummy app here, but in real use you launch apps yourself
    w = StartupWatcher(got.append, -1, psutil.boot_time(), lambda app, bt: False, settle_pct=5.0)
    w.start()
    import tempfile

    # cmd.exe mangles a command line with more than two quoted paths, so keep the script path space-free.
    script = Path(tempfile.mkdtemp(prefix="mochi_launch_")) / "launch_dummy.py"
    script.write_text("import time, hashlib\nt=time.time()\nwhile time.time()-t<1.2: hashlib.sha256(b'x'*4096).digest()\n"
                      "time.sleep(6)\n")
    # Launched through cmd.exe, like from a terminal (a python parent would look like a helper process).
    p = subprocess.Popen(["cmd", "/c", PY, str(script)], creationflags=0x08000000)
    try:
        for _ in range(100):
            if any(la.app == "python.exe:launch_dummy.py" for la in got):
                break
            time.sleep(0.1)
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        p.wait(5)
        w.stop()
    la = next(la for la in got if la.app == "python.exe:launch_dummy.py")
    assert la.settled and la.cold and 0.8 <= la.duration_s <= 4.0
