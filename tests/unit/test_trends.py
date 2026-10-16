"""Leak, disk-fill and runaway trend detection (DET-10, DIA-5, DIA-7, DIA-9)."""

import pytest

from mochi.config import Config
from mochi.model import GB, MB, Level, Volume
from mochi.trends import DiskTrend, LeakTracker, RunawayTracker, linear_fit, max_drop_fraction

from ..conftest import make_app, make_proc


def test_linear_fit_exact():
    slope, icpt, r2 = linear_fit([0, 1, 2, 3], [1, 3, 5, 7])
    assert slope == pytest.approx(2) and icpt == pytest.approx(1) and r2 == pytest.approx(1)


def test_linear_fit_degenerate():
    assert linear_fit([1], [5]) == (0.0, 5, 0.0)
    assert linear_fit([1, 1, 1], [1, 2, 3])[0] == 0.0
    assert linear_fit([0, 1, 2], [4, 4, 4])[2] == 1.0


def test_max_drop_fraction():
    assert max_drop_fraction([1, 2, 3, 4]) == 0.0
    assert max_drop_fraction([10, 5, 10]) == pytest.approx(0.5)
    assert max_drop_fraction([]) == 0.0


def feed_leak(tracker, minutes, start_mb=200.0, mb_per_min=50.0, key="python.exe:leaky.py", ts0=0.0, every=60):
    for i in range(int(minutes * 60 / every) + 1):
        t = ts0 + i * every
        app = make_app(key, start_mb + mb_per_min * t / 60, display="python.exe (leaky.py)", pid=77, ct=1.0)
        tracker.observe(t, {key: app})


class TestLeak:
    def test_fast_leak_found_within_20_minutes_with_accurate_rate(self):
        tr = LeakTracker(Config())
        feed_leak(tr, 14)
        assert not tr.findings()  # too short
        feed_leak(tr, 20)
        f = tr.findings()
        assert len(f) == 1
        assert f[0].rate_mb_h == pytest.approx(3000, rel=0.2)   # 50 MB/min = 3000 MB/h, within +-20%
        assert f[0].display == "python.exe (leaky.py)"
        sig = tr.signals()[0]
        assert sig.metric == "ram" and sig.kind == "trend" and sig.level == Level.HEADS_UP

    def test_slow_leak_needs_an_hour(self):
        tr = LeakTracker(Config())
        feed_leak(tr, 50, mb_per_min=150 / 60)   # 150 MB/h
        assert not tr.findings()
        feed_leak(tr, 70, mb_per_min=150 / 60)
        assert tr.findings()

    def test_sawtooth_is_not_a_leak(self):
        tr = LeakTracker(Config())
        for i in range(60):
            mb = 500 + (i % 10) * 80  # grows then drops back (garbage collection)
            tr.observe(i * 60.0, {"java.exe": make_app("java.exe", mb, pid=5, ct=1.0)})
        assert not tr.findings()

    def test_ignore_list_and_grow_by_design(self):
        tr = LeakTracker(Config())
        feed_leak(tr, 30, key="msmpeng.exe")
        assert not tr.findings()
        tr = LeakTracker(Config())
        feed_leak(tr, 30, key="chrome.exe", mb_per_min=4)   # 240 MB/h: below 3x threshold for browsers
        assert not tr.findings()

    def test_restart_resets_history(self):
        tr = LeakTracker(Config())
        feed_leak(tr, 30)
        tr.observe(30 * 60 + 60, {"python.exe:leaky.py": make_app("python.exe:leaky.py", 100, pid=78, ct=999.0)})
        assert not tr.findings()

    def test_self_and_system_apps_skipped_and_stale_pruned(self):
        tr = LeakTracker(Config())
        tr.observe(0, {"mochi": make_app("mochi", 100, me=True), "svchost.exe": make_app("svchost.exe", 100, system=True),
                       "a.exe": make_app("a.exe", 100)})
        assert set(tr.series) == {"a.exe"}
        tr.observe(700, {})
        assert not tr.series

    def test_growing_for(self):
        tr = LeakTracker(Config())
        feed_leak(tr, 30)
        assert tr.growing_for("python.exe:leaky.py") == pytest.approx(30 * 60)
        tr2 = LeakTracker(Config())
        for i in range(30):
            tr2.observe(i * 60.0, {"a.exe": make_app("a.exe", 500, pid=1, ct=1.0)})
        assert tr2.growing_for("a.exe") == 0.0
        assert tr2.growing_for("missing") == 0.0


class TestDiskTrend:
    def series(self, hours, free_start_gb, gb_per_hour, total_gb=500):
        d = DiskTrend(Config())
        for i in range(int(hours * 60) + 1):
            free = (free_start_gb - gb_per_hour * i / 60) * GB
            d.observe(i * 60.0, [Volume("C:\\", int(total_gb * GB), int(free))])
        return d

    def test_projection_within_a_week(self):
        sig = self.series(8, 100, 1.0).signals()   # 100 GB free, -24 GB/day -> ~4 days
        assert len(sig) == 1 and sig[0].detail["rule"] == "disk_projection"
        assert sig[0].detail["days_left"] == pytest.approx(92 / 24, rel=0.05)

    def test_short_span_or_big_free_space_ignored(self):
        assert not self.series(3, 100, 1.0).signals()           # < 6 hours of data
        assert not self.series(8, 300, 1.0).signals()           # 60% free: not worth a warning
        assert not self.series(8, 100, -1.0).signals()          # growing free space

    def test_removable_volumes_not_observed(self):
        d = DiskTrend(Config())
        d.observe(0, [Volume("E:\\", 10 * GB, GB, fixed=False)])
        assert not d.series


class TestRunaway:
    def run(self, tracker, seconds, procs, apps, fg=None, t0=0.0):
        for i in range(int(seconds / 5) + 1):
            tracker.observe(t0 + i * 5.0, procs, fg)
        return tracker.findings(t0 + int(seconds / 5) * 5.0, apps)

    def test_single_thread_runaway_found(self):
        """Critique problem 6: one stuck core is only 3% of a 32-thread CPU."""
        tr = RunawayTracker(Config())
        p = make_proc(10, "python.exe", cpu_core=99, app="python.exe:burn.py")
        apps = {"python.exe:burn.py": make_app("python.exe:burn.py", 20, 99, pid=10)}
        assert not self.run(tr, 200, [p], apps)
        f = self.run(tr, 310, [p], apps)
        assert f and f[0]["app"] == "python.exe:burn.py" and f[0]["cpu_core"] == 99
        assert tr.signals(310.0, apps)[0].detail["rule"] == "runaway"

    def test_foreground_app_is_not_a_runaway(self):
        tr = RunawayTracker(Config())
        p = make_proc(11, "game.exe", cpu_core=150, app="game.exe")
        assert not self.run(tr, 310, [p], {"game.exe": make_app("game.exe", 1000, 150, pid=11)}, fg="game.exe")

    def test_dips_tolerated_but_mostly_idle_is_not(self):
        tr = RunawayTracker(Config())
        apps = {"a.exe": make_app("a.exe", 10, 50, pid=12)}
        for i in range(70):
            tr.observe(i * 5.0, [make_proc(12, "a.exe", cpu_core=95 if i % 2 else 5, app="a.exe")], None)
        assert not tr.findings(345.0, apps)

    def test_mochi_itself_never_runaway_and_exited_forgotten(self):
        tr = RunawayTracker(Config())
        p = make_proc(13, "python.exe", cpu_core=99, app="mochi")
        assert not self.run(tr, 310, [p], {"mochi": make_app("mochi", 10, 99, pid=13, me=True)})
        tr.observe(400, [], None)
        assert not tr.hist and tr.history((13, 1000.0)) == []
