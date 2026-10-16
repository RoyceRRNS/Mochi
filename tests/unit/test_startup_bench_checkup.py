"""Startup timing (STR-1..5), benchmarks and Health Score (BEN-1..8), the PC check (CHK-1..6)."""

import json
import os
import subprocess
import sys
import threading

import pytest

from mochi import bench_worker
from mochi.benchmarks import (WEIGHTS, BenchmarkRunner, BenchmarkScheduler, BenchResult, HealthScore,
                              cleanup_leftovers)
from mochi.checkup import (CheckItem, CheckupRunner, check_battery, check_disk_health, check_disk_space,
                           check_idle_background, check_leaks, check_startup, judge_load, top_suggestions)
from mochi.config import Config
from mochi.model import GB, Volume
from mochi.platform.base import BatteryHealth, DiskHealth, StartupItem
from mochi.platform.fake import FakeClock, FakePlatform
from mochi.startup_tracker import Launch, LaunchAnalyzer, several_apps_slow, slow_apps
from mochi.trends import LeakFinding


# --------------------------------------------------------------- startup
class TestLaunchAnalyzer:
    def test_settle_time_excludes_the_quiet_window(self):
        la = LaunchAnalyzer(settle_pct=5, settle_s=2)
        la.on_new((1, 100.0), "code.exe", "Code.exe", 100.0, cold=True, existing_instance=False)
        out = None
        t = 100.0
        for cpu in [80, 70, 60, 40, 20, 3, 2, 1, 1, 1, 1, 1, 1, 1]:
            t += 0.25
            out = la.on_cpu((1, 100.0), cpu, t) or out
        assert out is not None and out.settled and out.cold
        assert out.duration_s == pytest.approx(1.5)  # quiet from 101.5, not 103.5

    def test_busy_again_restarts_quiet_window(self):
        la = LaunchAnalyzer(settle_s=1)
        la.on_new((1, 0.0), "a", "a", 0.0, False, False)
        assert la.on_cpu((1, 0.0), 1, 1.0) is None
        assert la.on_cpu((1, 0.0), 50, 1.5) is None
        assert la.on_cpu((1, 0.0), 1, 2.0) is None
        assert la.on_cpu((1, 0.0), 1, 3.0).duration_s == pytest.approx(2.0)

    def test_never_settles_is_discarded(self):
        la = LaunchAnalyzer(timeout_s=60)
        la.on_new((1, 0.0), "game", "game", 0.0, False, False)
        out = la.on_cpu((1, 0.0), 90, 61.0)
        assert out is not None and not out.settled and "60 s" in out.reason

    def test_relaunch_handoff_not_counted(self):
        la = LaunchAnalyzer()
        la.on_new((1, 0.0), "code.exe", "Code.exe", 0.0, False, existing_instance=True)
        out = la.on_exit((1, 0.0), 1.0)
        assert not out.settled and "handed over" in out.reason
        la.on_new((2, 0.0), "x", "x", 0.0, False, existing_instance=False)
        assert "exited before" in la.on_exit((2, 0.0), 10.0).reason
        assert la.on_exit((3, 0.0), 1.0) is None and la.on_cpu((3, 0.0), 0, 1.0) is None


def launches(app, durations, cold=False, t0=1_000_000.0):
    return [Launch(app, app, t0 + i * 3600, d, cold, True) for i, d in enumerate(durations)]


class TestSlowApps:
    def test_trend_detected(self):
        ls = launches("code.exe", [2.0] * 12 + [7.0] * 5)
        slow = slow_apps(ls, 1_000_000 + 20 * 3600)
        assert slow and slow[0]["app"] == "code.exe" and slow[0]["ratio"] > 3

    def test_needs_ten_prior_launches(self):
        assert not slow_apps(launches("code.exe", [2.0] * 8 + [7.0] * 5), 1_000_000 + 20 * 3600)

    def test_single_slow_launch_ignored_and_kinds_separate(self):
        ls = launches("a", [2.0] * 14 + [9.0]) + launches("a", [9.0] * 20, cold=True)
        assert not slow_apps(ls, 1_000_000 + 40 * 3600)

    def test_several_means_three(self):
        assert not several_apps_slow([{"app": "a"}, {"app": "b"}])
        assert several_apps_slow([{"app": "a"}, {"app": "b"}, {"app": "c"}])


# -------------------------------------------------------------- benchmarks
def run(ts, cpu=1.0, mem=0.5, dw=0.2, dr=0.1, on_ac=True, pm="balanced", valid=True):
    return BenchResult(ts, cpu, mem, dw, dr, "unbuffered", on_ac, pm, 2.0, None, None, valid)


class TestHealthScore:
    def test_still_learning_below_three_runs(self):
        hs = HealthScore()
        assert hs.reference([run(1), run(2)], (True, "balanced")) is None
        assert hs.score(run(3), None) is None

    def test_same_speed_scores_100_and_slower_scores_lower(self):
        hs = HealthScore()
        hist = [run(i) for i in range(10)]
        ref = hs.reference(hist, (True, "balanced"))
        assert hs.score(run(11), ref) == 100.0
        half = hs.score(run(12, cpu=2.0, mem=1.0, dw=0.4, dr=0.2), ref)
        assert half == pytest.approx(50.0)
        assert sum(WEIGHTS.values()) == pytest.approx(1.0)

    def test_faster_is_capped_at_100(self):
        hs = HealthScore()
        ref = hs.reference([run(i) for i in range(5)], (True, "balanced"))
        assert hs.score(run(9, cpu=0.1, mem=0.1, dw=0.01, dr=0.01), ref) == 100.0

    def test_like_for_like_conditions(self):
        hs = HealthScore()
        hist = [run(i, on_ac=True) for i in range(5)] + [run(10 + i, cpu=3.0, on_ac=False) for i in range(5)]
        assert hs.reference(hist, (True, "balanced"))["cpu_s"] == 1.0
        assert hs.reference(hist, (False, "balanced"))["cpu_s"] == 3.0
        assert hs.reference(hist, (True, "best performance")) is None

    def test_reference_uses_first_runs_and_rebaseline(self):
        hs = HealthScore(ref_runs=3)
        hist = [run(i) for i in range(3)] + [run(10 + i, cpu=5.0) for i in range(5)]
        assert hs.reference(hist, (True, "balanced"))["cpu_s"] == 1.0
        assert hs.reference(hist, (True, "balanced"), since=10)["cpu_s"] == 5.0

    def test_skipped_disk_test_renormalises(self):
        hs = HealthScore()
        ref = hs.reference([run(i) for i in range(4)], (True, "balanced"))
        r = run(9, cpu=2.0, mem=1.0)
        r.disk_write_s = r.disk_read_s = None
        assert hs.score(r, ref) == pytest.approx(50.0)
        assert hs.score(run(9, valid=False), ref) is None

    def test_should_suggest(self):
        s = HealthScore.should_suggest
        assert not s([70.0], 80, None, 0, 7)
        assert not s([90.0, 70.0], 80, None, 0, 7)
        assert s([75.0, 70.0], 80, None, 0, 7)
        assert not s([75.0, 70.0], 80, 0.0, 6 * 86400, 7)  # declined 6 days ago
        assert s([75.0, None, 70.0], 80, 0.0, 8 * 86400, 7)

    def test_row_roundtrip(self):
        r = run(5)
        assert BenchResult.from_row((1,) + r.to_row()).cpu_s == r.cpu_s


class TestScheduler:
    def test_daily_then_weekly(self):
        sch = BenchmarkScheduler(Config())
        assert sch.due(0, None, None)
        assert not sch.due(19 * 3600, 0, 0) and sch.due(20 * 3600, 0, 0)
        late = 15 * 86400
        assert not sch.due(late + 3 * 86400, 0, late) and sch.due(late + 7 * 86400, 0, late)

    def test_can_run_now(self):
        assert BenchmarkScheduler.can_run_now(True, True, False)
        assert BenchmarkScheduler.can_run_now(True, None, False)  # desktop: no battery = plugged in
        assert not BenchmarkScheduler.can_run_now(True, False, False)
        assert not BenchmarkScheduler.can_run_now(False, True, False)
        assert not BenchmarkScheduler.can_run_now(True, True, True)

    def test_overdue(self):
        sch = BenchmarkScheduler(Config())
        assert not sch.overdue(86400, 0, None) and sch.overdue(4 * 86400, 0, None)
        assert sch.overdue(4 * 86400, 0, 0) and not sch.overdue(3600, 0, 0)


class TestBenchWorker:
    def test_in_process_tests_return_timings(self, tmp_path):
        assert bench_worker.cpu_test(0.01) > 0
        assert bench_worker.mem_test(0.05) > 0
        w, r, mode = bench_worker.disk_test(str(tmp_path), 8)
        assert w > 0 and r > 0 and mode in ("unbuffered", "buffered", "direct")
        assert not list(tmp_path.glob("mochi_bench*.tmp"))  # cleaned up

    def test_cli_json(self, tmp_path, capsys):
        assert bench_worker.main(["--tests", "cpu,mem", "--reps", "2", "--scale", "0.01"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out["cpu"]) == 2 and len(out["mem"]) == 2

    def test_cleanup_leftovers(self, tmp_path):
        (tmp_path / "mochi_bench_1.tmp").write_text("x")
        (tmp_path / "keep.txt").write_text("x")
        assert cleanup_leftovers(tmp_path) == 1 and (tmp_path / "keep.txt").exists()
        assert cleanup_leftovers(tmp_path / "missing") == 0


class TestBenchmarkRunner:
    def test_subprocess_run(self, tmp_path):
        r = BenchmarkRunner(Config(bench_reps=2), tmp_path, scale=0.02, disk_mb=8)
        res = r.run(scheduled=False, free_gb=100.0, on_ac=None, power_mode="balanced", background_cpu=3.0, temp=None)
        assert res.valid and res.cpu_s and res.mem_s and res.disk_write_s and res.disk_read_s
        assert res.on_ac is True  # desktop treated as plugged in
        assert set(res.spread) == set(WEIGHTS)

    def test_disk_skipped_when_space_is_low(self, tmp_path):
        r = BenchmarkRunner(Config(bench_reps=1), tmp_path, scale=0.02, disk_mb=8)
        res = r.run(scheduled=False, free_gb=1.0, on_ac=True, power_mode=None, background_cpu=1.0, temp=None)
        assert res.valid and res.disk_write_s is None and "disk test skipped" in res.note

    def test_scheduled_run_skipped_when_busy(self, tmp_path):
        r = BenchmarkRunner(Config(), tmp_path, scale=0.02)
        res = r.run(scheduled=True, free_gb=100, on_ac=True, power_mode=None, background_cpu=40.0, temp=None)
        assert not res.valid and "background CPU" in res.note

    def test_abort_when_user_returns(self, tmp_path):
        r = BenchmarkRunner(Config(bench_reps=3), tmp_path, scale=2.0, disk_mb=64)
        calls = {"n": 0}

        def abort():
            calls["n"] += 1
            return "you came back" if calls["n"] > 1 else None

        res = r.run(scheduled=True, free_gb=100, on_ac=True, power_mode=None, background_cpu=1.0, temp=None,
                    abort_check=abort)
        assert not res.valid and res.note == "stopped: you came back"
        assert not list(tmp_path.glob("mochi_bench*.tmp"))

    def test_load_test_process(self, tmp_path):
        p = BenchmarkRunner(Config(), tmp_path).load_test(0.5, procs=1)
        out, _ = p.communicate(timeout=60)
        assert json.loads(out)["procs"] == 1


# ------------------------------------------------------------------ check-up
class TestCheckItems:
    def test_disk_space_thresholds(self):
        assert check_disk_space([Volume("C:\\", 500 * GB, 300 * GB)]).status == "fine"
        assert check_disk_space([Volume("C:\\", 200 * GB, 30 * GB)]).status == "look"
        assert check_disk_space([Volume("C:\\", 200 * GB, 10 * GB)]).status == "action"
        assert check_disk_space([Volume("C:\\", 2000 * GB, 150 * GB)]).status == "fine"  # 7.5% but 150 GB
        assert check_disk_space([]).status == "unavailable"
        item = check_disk_space([Volume("C:\\", 500 * GB, 300 * GB)], [("C:\\Users\\me\\Videos", 40 * GB)])
        assert "Videos" in item.detail

    def test_disk_health(self):
        f = FakePlatform(FakeClock())
        assert check_disk_health(f).status == "unavailable"
        f.disks = ([DiskHealth("NVMe", "SSD", "healthy", "Windows")], None)
        item = check_disk_health(f, smartctl=None)
        assert item.status == "fine" and "SMART details need" in item.detail
        f.disks = ([DiskHealth("A", "SSD", "healthy", "W"), DiskHealth("B", "HDD", "unhealthy", "W")], None)
        assert check_disk_health(f).status == "action"

    def test_startup_items(self):
        f = FakePlatform(FakeClock())
        f.startup = [StartupItem(f"app{i}", f"app{i}.exe", "Run (you)", True) for i in range(5)]
        assert check_startup(f).status == "fine"
        f.startup += [StartupItem(f"x{i}", None, "Run (you)", True) for i in range(6)]
        f.startup.append(StartupItem("off", None, "Run (you)", False))
        item = check_startup(f)
        assert item.status == "look" and item.data["count"] == 11 and "isn't measured yet" in item.detail
        f.startup += [StartupItem(f"y{i}", None, "Run (you)", True) for i in range(6)]
        assert check_startup(f, {"app1": 2.5}).status == "action"

    def test_load_judgement(self):
        assert judge_load([100, 99], [70], 100)[0] == "fine"
        assert judge_load([90], [], 100)[0] == "look"
        assert judge_load([70], [], 100)[0] == "action"
        assert judge_load([100], [96], 100)[0] == "action"
        assert judge_load([], [], 100)[0] == "unavailable"
        assert "needs admin" in judge_load([100], [], 100)[1]

    def test_idle_background_and_leaks(self):
        assert check_idle_background([]).status == "unavailable"
        assert check_idle_background([("a", 2.0)]).status == "fine"
        assert check_idle_background([("updater.exe", 35.0)]).status == "look"
        assert check_leaks([]).status == "fine"
        f = LeakFinding("d", "Discord.exe", 300, 900, 180, 0.95, 1400)
        assert "Discord.exe (+300 MB/h)" in check_leaks([f]).detail

    def test_battery_hidden_on_desktops(self):
        f = FakePlatform(FakeClock())
        assert check_battery(f, has_battery=False) is None
        assert check_battery(f, True).status == "unavailable"
        f.battery_info = BatteryHealth(60000, 54000, 300)
        item = check_battery(f, True)
        assert item.status == "fine" and "90%" in item.detail and "300 charge cycles" in item.detail
        f.battery_info = BatteryHealth(60000, 30000, None)
        assert check_battery(f, True).status == "action"

    def test_top_three_ranked_by_severity(self):
        items = [CheckItem("a", "A", "look", "", "tip a"), CheckItem("b", "B", "action", "", "tip b"),
                 CheckItem("c", "C", "fine", "", "tip c"), CheckItem("d", "D", "look", "", "tip d"),
                 CheckItem("e", "E", "action", "", "tip e")]
        assert top_suggestions(items) == ["tip b", "tip e", "tip a"]


class TestCheckupRunner:
    def make(self, score=95.0, fail_step=False):
        f = FakePlatform(FakeClock())
        f.disks = ([DiskHealth("NVMe", "SSD", "healthy", "W")], None)

        def idle_rows():
            if fail_step:
                raise RuntimeError("boom")
            return [("a.exe", 1.0)]

        return CheckupRunner(Config(), f, lambda: [Volume("C:\\", 500 * GB, 300 * GB)], lambda: [], idle_rows,
                             lambda: [], lambda: False, lambda: ({"cpu_s": 1}, score),
                             lambda stop: ([100.0], []))

    def test_full_report(self):
        progress = []
        rep = self.make().run(lambda s, f: progress.append((s, f)))
        keys = [i.key for i in rep.items]
        assert keys == ["disk_space", "disk_health", "startup", "cpu_load", "idle_background", "leaks", "health"]
        assert progress[-1] == ("Done", 1.0) and rep.score == 95.0 and rep.seconds < 90
        assert json.dumps(rep.to_json())

    def test_cancel_and_failures(self):
        ev = threading.Event()
        ev.set()
        rep = self.make().run(cancel=ev)
        assert rep.items == [] and rep.score is None
        rep = self.make(fail_step=True).run()
        bad = [i for i in rep.items if i.status == "unavailable" and "went wrong" in i.detail]
        assert len(bad) == 1

    def test_health_item_states(self):
        assert self.make(None).run().items[-1].status == "unavailable"
        assert self.make(70.0).run().items[-1].status == "action"
        assert self.make(85.0).run().items[-1].status == "look"
