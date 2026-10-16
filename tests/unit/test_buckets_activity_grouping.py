"""Buckets (CTX-1, CTX-6), activity classification (CTX-2..4) and app grouping (COL-5, COL-7)."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mochi.activity import DEFAULT_MAP, ActivityMapper, ActivitySmoother, flatten
from mochi.buckets import bucket_chain, day_ordinal, slot_label, time_slot
from mochi.grouping import SELF_KEY, Grouper, script_from_cmdline

from ..conftest import make_proc, make_sys

NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------------ buckets
class TestBuckets:
    def test_weekday_and_weekend_slots(self):
        mon_10 = datetime(2026, 10, 12, 10, 30, tzinfo=NY).timestamp()
        sat_10 = datetime(2026, 10, 17, 10, 30, tzinfo=NY).timestamp()
        assert time_slot(mon_10, NY) == 10
        assert time_slot(sat_10, NY) == 34

    def test_friday_to_saturday_midnight_boundary(self):
        fri = datetime(2026, 10, 16, 23, 59, 59, tzinfo=NY).timestamp()
        assert time_slot(fri, NY) == 23
        assert time_slot(fri + 1, NY) == 24  # Saturday 00:00
        assert day_ordinal(fri + 1, NY) == day_ordinal(fri, NY) + 1

    def test_dst_spring_forward_and_fall_back_never_crash(self):
        # 2026-03-08 02:00 doesn't exist in New York; 2026-11-01 01:00 happens twice.
        spring = datetime(2026, 3, 8, 6, 30, tzinfo=ZoneInfo("UTC")).timestamp()  # 01:30 EST
        slots = [time_slot(spring + i * 600, NY) for i in range(12)]
        assert 2 not in slots and all(0 <= s < 48 for s in slots)
        fall = datetime(2026, 11, 1, 5, 0, tzinfo=ZoneInfo("UTC")).timestamp()  # 01:00 EDT
        assert [time_slot(fall + i * 1800, NY) for i in range(5)].count(25) >= 3  # Sunday 01:xx repeats

    def test_all_48_slots_reachable(self):
        start = datetime(2026, 10, 12, 0, 0, tzinfo=NY).timestamp()
        assert {time_slot(start + h * 3600, NY) for h in range(24 * 7)} == set(range(48))

    def test_chain_order_and_labels(self):
        assert bucket_chain(10, "coding") == [("slot+activity", "s10|coding"), ("activity", "*|coding"), ("global", "*|*")]
        assert slot_label(34) == "weekend 10:00-11:00"
        assert slot_label(23) == "weekday 23:00-00:00"


# ----------------------------------------------------------------- activity
class TestActivity:
    def test_defaults_cover_at_least_30_apps(self):
        assert len(flatten(DEFAULT_MAP)) >= 30

    def test_mapping_is_case_insensitive_and_unknown_is_other(self):
        m = ActivityMapper()
        assert m.classify(make_sys(foreground_exe="CHROME.EXE")) == "browsing"
        assert m.classify(make_sys(foreground_exe="Code.exe")) == "coding"
        assert m.classify(make_sys(foreground_exe="mystery.exe")) == "other"
        assert m.classify(make_sys(foreground_exe=None)) == "other"

    def test_idle_needs_ten_minutes_without_input(self):
        m = ActivityMapper()
        assert m.classify(make_sys(foreground_exe="chrome.exe", idle_seconds=599)) == "browsing"
        assert m.classify(make_sys(foreground_exe="chrome.exe", idle_seconds=601)) == "idle"

    def test_watching_a_film_is_not_idle(self):
        m = ActivityMapper()
        assert m.classify(make_sys(foreground_exe="vlc.exe", idle_seconds=7200)) == "media"
        assert not m.is_user_idle(make_sys(foreground_exe="vlc.exe", idle_seconds=7200))

    def test_fullscreen_or_gamepad_game_is_not_idle(self):
        m = ActivityMapper()
        sys_ = make_sys(foreground_exe="eldenring.exe", idle_seconds=3600, fullscreen=True, fullscreen_d3d=True)
        assert m.classify(sys_) == "gaming"
        path_game = make_sys(foreground_exe="game.exe", foreground_path=r"D:\SteamLibrary\steamapps\common\Game\game.exe",
                             idle_seconds=3600)
        assert m.classify(path_game) == "gaming"

    def test_lock_screen_is_idle(self):
        assert ActivityMapper().classify(make_sys(foreground_exe="chrome.exe", locked=True, idle_seconds=1)) == "idle"

    def test_load_save_and_bad_entries(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text(json.dumps({"Blender.exe": "media", "x.exe": "idle", "y.exe": "dancing", "z.exe": "office"}))
        m = ActivityMapper.load(p)
        assert m.mapping == {"blender.exe": "media", "z.exe": "office"}
        assert len(m.warnings) == 2
        m.set("Foo.EXE", "coding")
        m.save(p)
        assert ActivityMapper.load(p).mapping["foo.exe"] == "coding"

    def test_corrupt_mapping_file_uses_defaults(self, tmp_path):
        p = tmp_path / "map.json"
        p.write_text("[]")
        m = ActivityMapper.load(p)
        assert m.mapping == flatten(DEFAULT_MAP) and m.warnings

    def test_set_rejects_unknown_class(self):
        m = ActivityMapper()
        with pytest.raises(ValueError):
            m.set("a.exe", "idle")
        with pytest.raises(ValueError):
            m.set("a.exe", "sleeping")
        m.remove("chrome.exe")
        assert "chrome.exe" not in m.mapping

    def test_smoother_uses_dominant_class_over_60s(self):
        sm = ActivitySmoother(60)
        t = 0.0
        for _ in range(10):
            t += 5
            out = sm.update(t, "coding")
        assert out == "coding"
        # alt-tab to the browser for 10 seconds doesn't flip the bucket
        assert sm.update(t + 5, "browsing") == "coding"
        assert sm.update(t + 10, "browsing") == "coding"
        # after a full minute of browsing it does
        for i in range(3, 13):
            out = sm.update(t + 5 * i, "browsing")
        assert out == "browsing"

    def test_smoother_tie_goes_to_most_recent(self):
        sm = ActivitySmoother(60)
        sm.update(1, "a")
        assert sm.update(2, "b") == "b"
        sm.reset()
        assert sm.update(3, "c") == "c"


# ----------------------------------------------------------------- grouping
def cmd_lookup(table):
    return lambda pid, ct: table.get(pid)


class TestGrouping:
    def test_script_detection(self):
        assert script_from_cmdline(["python", "C:\\tools\\memory_hog.py", "--gb", "2"]) == "memory_hog.py"
        assert script_from_cmdline(["python.exe", "-u", "leaky.py"]) == "leaky.py"
        assert script_from_cmdline(["python", "-m", "pytest", "-q"]) == "pytest"
        assert script_from_cmdline(["node", "server.js"]) == "server.js"
        assert script_from_cmdline(["python", "-c", "print(1)"]) is None
        assert script_from_cmdline(None) is None and script_from_cmdline([]) is None

    def test_multi_process_browser_is_one_app(self):
        procs = [make_proc(100, "chrome.exe", 5, 300, ppid=50)] + [make_proc(100 + i, "chrome.exe", 2, 200, ppid=100)
                                                                   for i in range(1, 6)]
        apps = Grouper(999, cmd_lookup({})).group(procs)
        assert list(apps) == ["chrome.exe"]
        a = apps["chrome.exe"]
        assert a.proc_count == 6 and a.root_pid == 100
        assert a.private_bytes == pytest.approx((300 + 5 * 200) * 1024 ** 2, rel=1e-6)
        assert a.cpu_core == pytest.approx(15)

    def test_interpreters_split_by_script(self):
        procs = [make_proc(10, "python.exe", 90, 2000), make_proc(11, "python.exe", 1, 30)]
        apps = Grouper(999, cmd_lookup({10: ["python", "memory_hog.py"], 11: ["python", "other.py"]})).group(procs)
        assert set(apps) == {"python.exe:memory_hog.py", "python.exe:other.py"}
        assert apps["python.exe:memory_hog.py"].display == "python.exe (memory_hog.py)"

    def test_mochi_and_its_children_are_self(self):
        procs = [make_proc(999, "python.exe", 1, 60, ct=900), make_proc(1001, "python.exe", 80, 30, ppid=999, ct=950)]
        apps = Grouper(999, cmd_lookup({999: ["python", "-m", "mochi"], 1001: ["python", "-m", "mochi.bench_worker"]})).group(procs)
        assert list(apps) == [SELF_KEY] and apps[SELF_KEY].is_self and apps[SELF_KEY].proc_count == 2

    def test_webview_helper_joins_its_parent_app(self):
        procs = [make_proc(200, "ms-teams.exe", 2, 300, ppid=4, ct=100), make_proc(201, "msedgewebview2.exe", 3, 400, ppid=200, ct=110),
                 make_proc(202, "msedgewebview2.exe", 1, 100, ppid=201, ct=120)]
        apps = Grouper(999, cmd_lookup({})).group(procs)
        assert list(apps) == ["ms-teams.exe"] and apps["ms-teams.exe"].proc_count == 3

    def test_reused_parent_pid_is_not_followed(self):
        # The "parent" started after the child: its PID was reused, so no ancestry.
        procs = [make_proc(300, "msedgewebview2.exe", 1, 100, ppid=301, ct=100), make_proc(301, "ms-teams.exe", 1, 100, ct=500)]
        apps = Grouper(999, cmd_lookup({})).group(procs)
        assert set(apps) == {"msedgewebview2.exe", "ms-teams.exe"}

    def test_pseudo_processes_excluded_and_system_flagged(self):
        procs = [make_proc(0, "System Idle Process", 700), make_proc(4, "System", 1, 1), make_proc(50, "svchost.exe", 1, 10)]
        apps = Grouper(999, cmd_lookup({})).group(procs)
        assert "system idle process" not in apps
        assert apps["system"].is_system and apps["svchost.exe"].is_system

    def test_ws_private_none_propagates(self):
        p = make_proc(10, "a.exe", 1, 10)
        p.ws_private = None
        apps = Grouper(999, cmd_lookup({})).group([p, make_proc(11, "a.exe", 1, 10, ppid=10)])
        assert apps["a.exe"].ws_private is None

    def test_script_cache_is_pruned(self):
        g = Grouper(999, cmd_lookup({}))
        for i in range(200):
            g.group([make_proc(10_000 + i, "python.exe", ct=float(i))])
        assert len(g._scripts) < 200
