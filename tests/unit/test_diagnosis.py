"""Diagnosis rules (DIA-1..14): each rule positive and negative, order, wording, fallback."""

import pytest

from mochi.config import Config
from mochi.detector import Signal
from mochi.diagnosis import RULE_ORDER, DiagContext, DiagnosisEngine, vm_note
from mochi.model import GB, MB, Battery, Level, Volume
from mochi.platform.base import FileInfo
from mochi.platform.fake import FakeClock, FakePlatform

from ..conftest import make_app, make_sample, make_sys

ENGINE = DiagnosisEngine()


def ctx_for(sys_kw=None, apps=None, history=None, bands=None, activity="browsing", user_idle=False, runaway=None,
            leaks=None, platform=None, cfg=None, n_history=30):
    s = make_sample(sys=make_sys(**(sys_kw or {})), apps=apps or {}, mono=10_000.0)
    hist = history
    if hist is None:
        hist = []
        for i in range(n_history):
            h = make_sample(sys=make_sys(**(sys_kw or {})), apps=apps or {}, mono=10_000.0 - (n_history - i) * 5)
            hist.append(h)
        hist.append(s)
    return DiagContext(s, hist, bands or {}, cfg or Config(), platform or FakePlatform(FakeClock()), activity, user_idle,
                       runaway or [], leaks or {})


def sig(metric, kind="z", level=Level.HEADS_UP, value=None, **detail):
    return Signal(metric, level, kind, value, z=4.0 if kind == "z" else None, detail=detail)


HOG = {"python.exe:memory_hog.py": make_app("python.exe:memory_hog.py", 9000, 20, display="python.exe (memory_hog.py)", pid=50),
       "chrome.exe": make_app("chrome.exe", 1500, 5, pid=60, procs=8)}


class TestRules:
    def test_rule_order_is_documented(self):
        assert RULE_ORDER.index("memory_pressure") < RULE_ORDER.index("disk_io")
        assert RULE_ORDER.index("runaway") < RULE_ORDER.index("throttling")

    def test_memory_pressure_positive(self):
        c = ctx_for({"paging_rate": 2500.0, "ram_available": int(0.8 * GB), "commit_pct": 95}, HOG,
                    bands={"paging": (10.0, 5.0)})
        d = ENGINE.diagnose(sig("ram"), c)
        assert d.rule == "memory_pressure"
        assert d.culprit.app == "python.exe:memory_hog.py"
        assert "8.8 GB" in d.tip and "shuffling memory to disk" in d.cause and "usually about 10" in d.cause
        assert d.key == ("app", "python.exe:memory_hog.py")

    def test_memory_pressure_negative_when_memory_is_free(self):
        c = ctx_for({"paging_rate": 2500.0, "ram_available": 8 * GB, "commit_pct": 50}, HOG, bands={"paging": (10.0, 5.0)})
        assert ENGINE.diagnose(sig("paging"), c).rule != "memory_pressure"

    def test_memory_pressure_beats_disk_io(self):
        """Paging *is* disk I/O, so memory pressure must win (DIA-3)."""
        c = ctx_for({"paging_rate": 3000.0, "ram_available": int(0.5 * GB), "disk_busy_pct": 100.0}, HOG,
                    bands={"paging": (10.0, 5.0)})
        assert ENGINE.diagnose(sig("disk"), c).rule == "memory_pressure"

    def test_runaway_from_process_signal(self):
        apps = {"python.exe:burn.py": make_app("python.exe:burn.py", 20, 99, display="python.exe (burn.py)", pid=70)}
        c = ctx_for({}, apps)
        d = ENGINE.diagnose(sig("cpu", "proc", value=99, rule="runaway", target="python.exe:burn.py", minutes=6,
                                cpu_core=99, cpu_total=3), c)
        assert d.rule == "runaway" and "99% of a CPU core for 6 minutes" in d.cause and "Is it stuck?" in d.tip
        kinds = [a.kind for a in d.actions]
        assert "end_process" in kinds and "lower_priority" in kinds

    def test_runaway_whole_cpu_wording_and_beats_throttling(self):
        apps = {"burner.exe": make_app("burner.exe", 20, 790, pid=71)}
        apps["burner.exe"].cpu_total = 98.75
        runaway = [{"app": "burner.exe", "cpu_core": 790, "cpu_total": 98.75, "minutes": 5.5}]
        c = ctx_for({"cpu_total": 99.0, "perf_limit_pct": 50.0}, apps, runaway=runaway)
        d = ENGINE.diagnose(sig("cpu"), c)
        assert d.rule == "runaway" and "99% of your whole CPU" in d.cause

    def test_system_process_never_called_stuck(self):
        apps = {"msmpeng.exe": make_app("msmpeng.exe", 200, 99, display="MsMpEng.exe", pid=72, system=True)}
        c = ctx_for({}, apps, runaway=[{"app": "msmpeng.exe", "cpu_core": 99, "cpu_total": 12, "minutes": 6}])
        d = ENGINE.diagnose(sig("cpu"), c)
        assert d.rule == "runaway" and "stuck" not in d.tip.lower() and "Defender" in d.tip
        assert [a.kind for a in d.actions if a.kind == "end_process"] == []

    def test_disk_io_positive_and_negative(self):
        apps = {"onedrive.exe": make_app("onedrive.exe", 100, 5, io_bps=80 * MB, pid=73),
                "chrome.exe": make_app("chrome.exe", 100, 5, io_bps=1 * MB, pid=74)}
        c = ctx_for({"disk_busy_pct": 99.0, "disk_latency_ms": 80.0}, apps)
        d = ENGINE.diagnose(sig("disk"), c)
        assert d.rule == "disk_io" and d.culprit.app == "onedrive.exe" and "probably" in d.tip and "80 MB/s" in d.tip
        c = ctx_for({"disk_busy_pct": 99.0}, apps, n_history=3)   # only 20 s of history
        assert ENGINE.diagnose(sig("disk"), c).rule != "disk_io"

    def test_leak_from_trend(self):
        apps = {"discord.exe": make_app("discord.exe", 1400, 2, display="Discord.exe", pid=75)}
        d = ENGINE.diagnose(sig("ram", "trend", rule="leak", target="discord.exe", rate_mb_h=300, growth_mb=900,
                                span_min=180), ctx_for({}, apps))
        assert d.rule == "leak" and "grown by 900 MB in 3 h and never let go" in d.cause and "Restarting" in d.tip

    def test_memory_hog_names_biggest_app(self):
        c = ctx_for({"ram_available": 5 * GB}, HOG, bands={"ram": (5.5, 0.4)})
        d = ENGINE.diagnose(sig("ram"), c)
        assert d.rule == "memory_hog" and d.culprit.app == "python.exe:memory_hog.py"
        assert "2.0x your usual" in d.cause or "twice your usual" in d.cause

    def test_throttling_positive_and_negative(self):
        c = ctx_for({"cpu_total": 90.0, "perf_limit_pct": 55.0, "temp_c": 97.0}, {})
        d = ENGINE.diagnose(sig("cpu", "abs", rule="throttle"), c)
        assert d.rule == "throttling" and "55%" in d.cause and "97" in d.cause and "vents" in d.tip
        c = ctx_for({"cpu_total": 90.0, "perf_limit_pct": 100.0, "cpu_perf_pct": 140.0}, {})
        assert ENGINE.diagnose(sig("cpu", "abs", rule="throttle"), c).rule != "throttling"

    def test_hot_cpu_without_throttling(self):
        d = ENGINE.diagnose(sig("temp", "abs", rule="temp_high"), ctx_for({"cpu_total": 20.0, "temp_c": 98.0}, {}))
        assert d.rule == "throttling" and "98" in d.cause

    def test_storage_wording(self):
        d = ENGINE.diagnose(sig("disk_free", "abs", Level.UH_OH, rule="disk_full", target="C:\\", free_gb=8.0, free_pct=3.0),
                            ctx_for({}, {}))
        assert d.rule == "storage" and "Only 8 GB left on C:\\" in d.cause and d.actions[0].kind == "biggest_folders"
        d = ENGINE.diagnose(sig("disk_free", "trend", rule="disk_projection", target="D:\\", free_gb=50, free_pct=10,
                                days_left=4.2, rate_gb_day=12.0), ctx_for({}, {}))
        assert "12.0 GB a day" in d.cause and "4 days" in d.cause

    def test_idle_cpu_known_maintenance_is_reassuring(self):
        apps = {"msmpeng.exe": make_app("msmpeng.exe", 200, 60, display="MsMpEng.exe", pid=80, system=True)}
        apps["msmpeng.exe"].is_system = False  # even if it isn't flagged system, the name is recognised
        d = ENGINE.diagnose(sig("cpu"), ctx_for({"cpu_total": 40}, apps, activity="idle", user_idle=True))
        assert d.rule == "idle_cpu" and d.evidence["maintenance"] and "normal Windows housekeeping" in d.tip
        assert "end_process" not in [a.kind for a in d.actions]

    def test_idle_cpu_unfamiliar_never_says_malicious(self):
        clock = FakeClock()
        fake = FakePlatform(clock)
        path = r"C:\Users\me\Downloads\weird.exe"
        fake.files[path] = FileInfo(path, None, None, False, False, True)
        apps = {"weird.exe": make_app("weird.exe", 50, 70, pid=81)}
        from mochi.platform.fake import FakeProc

        fake.add(FakeProc("weird.exe", 81, exe=path))
        d = ENGINE.diagnose(sig("cpu"), ctx_for({"cpu_total": 40}, apps, activity="idle", user_idle=True, platform=fake))
        text = (d.cause + d.tip).lower()
        assert d.rule == "idle_cpu" and "unfamiliar" in text and "downloads" in text
        for word in ("malicious", "malware", "virus", "unwanted"):
            assert word not in text

    def test_idle_cpu_needs_user_away(self):
        apps = {"a.exe": make_app("a.exe", 50, 70, pid=82)}
        assert ENGINE.diagnose(sig("cpu"), ctx_for({"cpu_total": 40}, apps, activity="browsing")).rule != "idle_cpu"

    def test_battery_wording(self):
        apps = {"spotify.exe": make_app("spotify.exe", 300, 30, display="Spotify.exe", pid=83)}
        d = ENGINE.diagnose(sig("discharge", value=24.0), ctx_for({"battery": Battery(60, False, 24.0)}, apps,
                                                                  bands={"discharge": (12.0, 2.0)}))
        assert d.rule == "battery" and "2.0x faster than usual" in d.cause and "top CPU user on battery" in d.tip
        assert "power user" not in d.tip

    def test_vm_host_explained(self):
        apps = {"vmmemwsl": make_app("vmmemwsl", 6000, 5, display="VmmemWSL", pid=84)}
        c = ctx_for({"paging_rate": 2500.0, "ram_available": int(0.5 * GB)}, apps, bands={"paging": (10.0, 5.0)})
        d = ENGINE.diagnose(sig("ram"), c)
        assert "WSL" in d.tip and "can't see" in d.tip
        assert vm_note("chrome.exe") is None and vm_note("vmmem") is not None

    def test_cpu_hog_names_the_busy_app(self):
        apps = {"blender.exe": make_app("blender.exe", 2000, 600, pid=87)}
        apps["blender.exe"].cpu_total = 75.0
        d = ENGINE.diagnose(sig("cpu"), ctx_for({"cpu_total": 80}, apps, bands={"cpu": (8.0, 3.0)}))
        assert d.rule == "cpu_hog" and d.culprit.app == "blender.exe" and "75% of your CPU" in d.cause
        assert "usual 8%" in d.cause and d.key == ("app", "blender.exe")

    def test_cpu_hog_needs_one_app_to_explain_it(self):
        apps = {f"a{i}.exe": make_app(f"a{i}.exe", 10, 40, pid=90 + i) for i in range(8)}
        for a in apps.values():
            a.cpu_total = 5.0
        assert ENGINE.diagnose(sig("cpu"), ctx_for({"cpu_total": 45}, apps)).rule == "unknown"

    def test_fallback_lists_top_users(self):
        apps = {"a.exe": make_app("a.exe", 100, 40, pid=85), "b.exe": make_app("b.exe", 100, 20, pid=86)}
        for a in apps.values():
            a.cpu_total = 2.0
        s = Signal("cpu", Level.PSST, "z", 30.0, 3.0, mu=10.0)
        d = ENGINE.diagnose(s, ctx_for({"cpu_total": 30}, apps))
        assert d.rule == "unknown" and "can't pin down" in d.tip and "a.exe: 40%" in d.tip and "vs about 10.0" in d.cause

    def test_no_unmeasurable_claims_anywhere(self):
        """DIA-14: no 'syncing 3 GB', no tab counts, no 'power user'."""
        cases = [
            (sig("ram"), ctx_for({"paging_rate": 2500.0, "ram_available": int(0.5 * GB)}, HOG, bands={"paging": (10, 5)})),
            (sig("disk"), ctx_for({"disk_busy_pct": 99.0}, {"x.exe": make_app("x.exe", 1, 1, io_bps=MB, pid=9)})),
            (sig("discharge", value=20.0), ctx_for({"battery": Battery(50, False, 20)}, HOG, bands={"discharge": (5, 1)})),
        ]
        for s, c in cases:
            d = ENGINE.diagnose(s, c)
            text = (d.cause + " " + d.tip).lower()
            for banned in ("syncing", "tabs", "power user", "critical failure"):
                assert banned not in text

    def test_rules_only_explain_their_metrics(self):
        """A full disk never 'explains' a CPU spike."""
        c = ctx_for({"cpu_total": 50, "volumes": [Volume("C:\\", 100 * GB, GB)]}, {"a.exe": make_app("a.exe", 1, 50, pid=9)})
        assert ENGINE.diagnose(sig("cpu"), c).rule != "storage"
