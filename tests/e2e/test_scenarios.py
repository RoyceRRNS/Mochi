"""End-to-end use cases on the full pipeline (collector -> baselines -> detection -> diagnosis
-> alerts -> moods -> storage), driven by the scripted platform faster than real time.

Covers the fault-injection table (TST-4) with pass criteria, plus feedback, quiet modes,
learning mode, persistence and the quirks.
"""

from __future__ import annotations

import socket

import pytest

from mochi.config import Config
from mochi.model import GB, WATCH_METRICS, Battery, Fan, Level, Volume
from mochi.moods import Mood
from mochi.platform.base import FileInfo
from mochi.platform.fake import FakeProc, local_ts
from mochi.replay import Simulation, normal_use
from mochi.sensors import FEED_NAME, LENOVO, SensorReading

from .conftest import TEST_START, gaming, idle_time


def run_until(sim, predicate, max_seconds, step_cb=None):
    """Step until ``predicate()`` is true; return the seconds it took (or None)."""
    t = 0.0
    while t < max_seconds:
        if step_cb:
            step_cb(t)
        sim.step()
        t += sim.interval
        if predicate():
            return t
    return None


def opened_rules(sim, min_level=Level.PSST):
    return [e.episode.rule for e in sim.alert_events(("opened", "escalated", "reopened")) if e.episode.level >= min_level]


def first_episode(sim, rule):
    for e in sim.alert_events(("opened", "escalated", "reopened")):
        if e.episode.rule == rule:
            return e.episode
    return None


# ------------------------------------------------------------- normal use
def test_normal_day_raises_no_alerts(warm):
    warm.run(2 * 3600)
    assert warm.alert_events() == []
    assert warm.engine.latest.mood == Mood.HAPPY


def test_normal_heavy_use_in_a_known_busy_slot_is_quiet(warm):
    """Gaming at 21:00 is normal for this PC, so ~60% CPU raises nothing (TST-4)."""
    warm.jump_to(local_ts(2026, 10, 16, 20, 30, 0))
    normal_use(warm)
    gaming(warm)
    warm.run(45 * 60)
    assert warm.engine.latest.activity == "gaming" and warm.engine.latest.cpu > 45
    assert [e for e in warm.alert_events() if e.episode.level >= Level.HEADS_UP] == []


def test_same_load_at_an_unusual_time_is_flagged(warm):
    """70% CPU while browsing at 10 am is unusual for this PC: it's flagged."""
    warm.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=560.0, private_mb=300,
                               create_time=warm.clock.ts))
    took = run_until(warm, lambda: any(e.episode.level >= Level.HEADS_UP for e in warm.alert_events()), 8 * 60)
    assert took is not None and took <= 6 * 60


# ------------------------------------------------------------ memory hog
def hog_step(sim, mb_per_step=150.0, cap_mb=13_500.0):
    def cb(_t):
        p = sim.platform
        if 7000 not in p.procs:
            p.add(FakeProc("python.exe", 7000, 1000, cpu_core=25.0, private_mb=300, create_time=sim.clock.ts,
                           cmdline=["python", "tools\\memory_hog.py", "--gb", "13", "--token=SECRET123"]))
        p.update(7000, private_mb=min(cap_mb, p.procs[7000].private_mb + mb_per_step))
        used = 3 + sum(x.private_mb for x in p.procs.values()) / 1024
        avail = 100 * (16 - used) / 16
        p.set(paging_rate=3000.0 if avail < 10 else 6.0, commit_pct=95.0 if avail < 10 else 50.0)
    return cb


def test_memory_hog_demo_flow(warm):
    """Puffy, then Dizzy; the alert names the script with the right cause; closing it feeds Mochi."""
    cb = hog_step(warm)
    puffy_at = run_until(warm, lambda: warm.engine.latest.mood == Mood.PUFFY, 10 * 60, cb)
    assert puffy_at is not None
    heads = run_until(warm, lambda: any(e.notify == "popup" for e in warm.alert_events()), 7 * 60, cb)
    assert heads is not None, "no popup within 7 minutes"
    ep = next(e.episode for e in warm.alert_events() if e.notify == "popup")
    assert ep.diagnosis.culprit.app == "python.exe:memory_hog.py"
    assert "memory_hog.py" in ep.diagnosis.cause + ep.diagnosis.tip
    run_until(warm, lambda: warm.engine.latest.mood == Mood.DIZZY, 6 * 60, cb)
    assert warm.engine.latest.mood == Mood.DIZZY
    assert first_episode(warm, "memory_pressure") is not None
    # The user closes the hog after the tip: Mochi gets fed and turns proud.
    warm.platform.remove(7000)
    warm.platform.set(paging_rate=6.0, commit_pct=50.0)
    warm.run(60)
    feeds = [e.data for e in warm.events if e.kind == "feed"]
    assert feeds and feeds[0].snack == "a whole dango"
    run_until(warm, lambda: warm.engine.latest.mood == Mood.PROUD, 120)
    assert warm.engine.latest.mood == Mood.PROUD
    # The command line (with a fake token) was never stored (SEC-3).
    dump = str(warm.engine.storage.query("SELECT * FROM alerts")) + str(warm.engine.storage.query("SELECT * FROM proc_samples"))
    assert "SECRET123" not in dump and "memory_hog.py" in dump


def test_memory_pressure_reaches_uh_oh_and_dismiss_does_not_nag(warm):
    cb = hog_step(warm, mb_per_step=400)
    run_until(warm, lambda: any(e.episode.level == Level.UH_OH and e.notify == "popup" for e in warm.alert_events()),
              12 * 60, cb)
    uh = [e for e in warm.alert_events() if e.episode.level == Level.UH_OH and e.notify == "popup"]
    assert uh
    ep = uh[0].episode
    warm.engine.respond(ep.id, "dismiss")
    warm.clear_events()
    warm.run(10 * 60, lambda t, v: cb(t))
    assert not [e for e in warm.alert_events() if e.notify == "popup" and e.episode.id == ep.id]


# ---------------------------------------------------------------- runaways
def test_single_thread_runaway(warm):
    """One stuck core is only 12.5% of this 8-thread PC; it's still caught (critique problem 6)."""
    warm.platform.add(FakeProc("python.exe", 7100, 1000, cpu_core=99.0, private_mb=20, create_time=warm.clock.ts,
                               cmdline=["python", "cpu_burner.py", "--threads", "1"]))
    took = run_until(warm, lambda: first_episode(warm, "runaway") is not None, 8 * 60)
    assert took is not None and took <= 6 * 60
    ep = first_episode(warm, "runaway")
    assert ep.diagnosis.culprit.app == "python.exe:cpu_burner.py" and "Is it stuck?" in ep.diagnosis.tip


def test_all_core_runaway_is_one_episode(warm):
    for i in range(8):
        warm.platform.add(FakeProc("python.exe", 7200 + i, 7200 if i else 1000, cpu_core=100.0, private_mb=15,
                                   create_time=warm.clock.ts + i * 0.01, cmdline=["python", "cpu_burner.py", "--threads", "all"]))
    took = run_until(warm, lambda: first_episode(warm, "runaway") is not None, 8 * 60)
    assert took is not None and took <= 6 * 60
    warm.run(5 * 60)
    runaway_eps = {e.episode.id for e in warm.alert_events(("opened",)) if e.episode.rule == "runaway"}
    assert len(runaway_eps) == 1


# ---------------------------------------------------------------- leak
def test_leak_detected_within_20_minutes_with_rate(warm):
    warm.platform.add(FakeProc("python.exe", 7300, 1000, cpu_core=2.0, private_mb=100, create_time=warm.clock.ts,
                               cmdline=["python", "leaky.py", "--mb-per-min", "50"]))

    def grow(_t):
        warm.platform.update(7300, private_mb=warm.platform.procs[7300].private_mb + 50 / 12)

    took = run_until(warm, lambda: first_episode(warm, "leak") is not None, 25 * 60, grow)
    assert took is not None and took <= 20 * 60
    ep = first_episode(warm, "leak")
    assert ep.diagnosis.culprit.app == "python.exe:leaky.py"
    assert ep.diagnosis.evidence["rate_mb_h"] == pytest.approx(3000, rel=0.2)


# ------------------------------------------------------------- disk
def test_disk_bottleneck(warm):
    warm.platform.add(FakeProc("python.exe", 7400, 1000, cpu_core=8.0, private_mb=40, io_bps=250 * 1024 ** 2,
                               create_time=warm.clock.ts, cmdline=["python", "disk_thrash.py"]))
    warm.platform.set(disk_busy_pct=99.0, disk_latency_ms=60.0, disk_queue=8.0)
    took = run_until(warm, lambda: first_episode(warm, "disk_io") is not None, 7 * 60)
    assert took is not None and took <= 5 * 60
    ep = first_episode(warm, "disk_io")
    assert ep.diagnosis.culprit.app == "python.exe:disk_thrash.py" and "probably" in ep.diagnosis.tip


def test_disk_filling_makes_mochi_grumpy_within_a_minute(warm):
    total = 256 * GB
    warm.platform.set(volumes=[Volume("C:\\", total, int(40 * GB))])
    warm.run(60)
    warm.platform.set(volumes=[Volume("C:\\", total, int(18 * GB))])  # < 10% and < 20 GB
    grumpy = run_until(warm, lambda: warm.engine.latest.mood == Mood.GRUMPY, 120)
    assert grumpy is not None and grumpy <= 60
    ep = first_episode(warm, "storage")
    assert ep is not None and "Only 18 GB left on C:\\" in ep.diagnosis.cause
    assert ep.diagnosis.actions[0].kind == "biggest_folders"


# --------------------------------------------------------- thermal / battery
def test_thermal_throttling(warm):
    warm.platform.add(FakeProc("render.exe", 7500, 1000, cpu_core=700.0, private_mb=900, create_time=warm.clock.ts))
    warm.platform.set(perf_limit_pct=55.0, temp_c=97.0)
    warm.platform.sys.unavailable.pop("temp", None)
    took = run_until(warm, lambda: warm.engine.latest.mood == Mood.SWEATY, 3 * 60)
    assert took is not None
    took = run_until(warm, lambda: first_episode(warm, "throttling") is not None, 4 * 60)
    assert took is not None and took <= 3 * 60
    d = first_episode(warm, "throttling").diagnosis
    assert "overheating" in d.cause and "55%" in d.cause and "97" in d.cause and "vents" in d.tip


# ------------------------------------------- CPU temperature, GPU and fans
def helper_feed(sim, **kw):
    """What the elevated sensor helper writes every few seconds (before each sample)."""
    def write(_t):
        d = dict(ts=sim.clock.ts + sim.interval, source=LENOVO, cpu_temp=62.0, gpu_temp=None,
                 fans=[Fan("Fan 1", 2400, 5400), Fan("Fan 2", 2500, 5400)])
        d.update(kw)
        (sim.paths.root / FEED_NAME).write_text(SensorReading(**d).to_json(), encoding="utf-8")
    return write


def test_temperature_timeline_from_the_helper(warm):
    """The CPU temperature timeline fills in once the helper reports, and stops when it does."""
    assert warm.engine.latest.cpu_temp is None and "temp" in warm.engine.latest.unavailable
    run_until(warm, lambda: False, 10 * 60, step_cb=helper_feed(warm))
    v = warm.engine.latest
    assert v.cpu_temp == 62.0 and v.sources["temp"] == LENOVO and [f.rpm for f in v.fans] == [2400, 2500]
    rows = warm.engine.timeline("temp", 3600)["rows"]
    assert sum(1 for r in rows if r[1] == 62.0) >= 115
    fans = warm.engine.timeline("fan", 3600)["rows"]
    assert {r[1] for r in fans if r[1] is not None} == {2500}
    (warm.paths.root / FEED_NAME).unlink()                 # the helper quit
    warm.run(30)
    assert warm.engine.latest.cpu_temp is None and warm.engine.latest.unavailable["temp"]
    assert warm.alert_events() == []


def test_overheating_reported_through_the_helper(warm):
    warm.platform.add(FakeProc("render.exe", 7500, 1000, cpu_core=700.0, private_mb=900, create_time=warm.clock.ts))
    warm.platform.set(perf_limit_pct=55.0)
    feed = helper_feed(warm, cpu_temp=97.0, fans=[Fan("Fan 1", 5300, 5400), Fan("Fan 2", 5350, 5400)])
    took = run_until(warm, lambda: first_episode(warm, "throttling") is not None, 4 * 60, step_cb=feed)
    assert took is not None and took <= 3 * 60
    d = first_episode(warm, "throttling").diagnosis
    assert "overheating" in d.cause and "97" in d.cause
    v = warm.engine.latest
    assert v.mood == Mood.SWEATY and max(f.rpm for f in v.fans) == 5350


def test_gpu_heavy_gaming_is_recorded_but_never_alerts(warm):
    warm.jump_to(local_ts(2026, 10, 16, 20, 0, 0))
    gaming(warm)
    warm.platform.set(gpu_busy_pct=99.0, gpu_temp_c=86.0, gpu_power_w=110.0, gpu_mem_used_gb=7.5, gpu_mem_total_gb=8.0,
                      gpu_name="NVIDIA GeForce RTX 4070 Laptop GPU")
    run_until(warm, lambda: False, 20 * 60, step_cb=helper_feed(warm, fans=[Fan("Fan 1", 5200, 5400)]))
    assert not [e for e in warm.alert_events() if set(e.episode.metrics) & set(WATCH_METRICS)]
    v = warm.engine.latest
    assert (v.gpu_busy, v.gpu_temp, v.gpu_power_w) == (99.0, 86.0, 110.0) and v.gpu_mem_used_gb == 7.5
    for metric, value in (("gpu", 99.0), ("gpu_temp", 86.0), ("fan", 5200)):
        rows = warm.engine.timeline(metric, 3600)["rows"]
        assert max(r[1] for r in rows if r[1] is not None) == value
    assert warm.engine.storage.scalar("SELECT MAX(gpu_power) FROM samples") == 110.0


def test_battery_draining_fast(warm):
    warm.jump_to(local_ts(2026, 10, 16, 13, 5, 0))
    normal_use(warm)
    warm.platform.set(battery=Battery(60.0, False, 10.0))
    warm.run(300)
    warm.clear_events()
    warm.platform.add(FakeProc("spotify.exe", 7600, 1000, cpu_core=120.0, private_mb=400, create_time=warm.clock.ts))
    warm.platform.set(battery=Battery(55.0, False, 32.0))
    took = run_until(warm, lambda: first_episode(warm, "battery") is not None, 8 * 60)
    assert took is not None
    d = first_episode(warm, "battery").diagnosis
    assert "faster than usual" in d.cause and "top CPU user on battery" in d.tip


# ----------------------------------------------------------- idle CPU
def go_idle(warm):
    warm.jump_to(local_ts(2026, 10, 16, 14, 5, 0))
    normal_use(warm)
    idle_time(warm)
    warm.run(300)
    warm.clear_events()


def test_windows_maintenance_while_idle_is_reassuring(warm):
    go_idle(warm)
    warm.platform.add(FakeProc("MsMpEng.exe", 7700, 700, cpu_core=320.0, private_mb=300, create_time=warm.clock.ts,
                               exe=r"C:\ProgramData\Microsoft\Windows Defender\Platform\MsMpEng.exe"))
    run_until(warm, lambda: any(e.episode.level >= Level.HEADS_UP for e in warm.alert_events()), 8 * 60)
    eps = [e.episode for e in warm.alert_events()]
    assert eps
    d = eps[0].diagnosis
    assert d.rule in ("idle_cpu", "runaway")
    text = (d.cause + d.tip).lower()
    assert "malicious" not in text and "unwanted" not in text and "stuck" not in text
    assert "defender" in text or "windows" in text


def test_unfamiliar_program_while_idle(warm):
    go_idle(warm)
    path = r"C:\Users\me\Downloads\miner.exe"
    warm.platform.files[path] = FileInfo(path, None, None, False, False, True)
    warm.platform.add(FakeProc("miner.exe", 7800, 1000, cpu_core=320.0, private_mb=100, create_time=warm.clock.ts,
                               exe=path))
    run_until(warm, lambda: first_episode(warm, "idle_cpu") is not None or first_episode(warm, "runaway") is not None,
              8 * 60)
    eps = [e.episode for e in warm.alert_events() if e.episode.rule in ("idle_cpu", "runaway")]
    assert eps
    if eps[0].rule == "idle_cpu":
        text = (eps[0].diagnosis.cause + eps[0].diagnosis.tip).lower()
        assert "unfamiliar" in text and "malicious" not in text


# ------------------------------------------------------------ feedback
def test_i_meant_to_do_that_full_flow(warm):
    st_before = warm.engine.baselines.lookup(9, "browsing", "cpu")[0]
    mean_before = st_before.mean if st_before else None

    def render_on():
        warm.platform.add(FakeProc("blender.exe", 7900, 1000, cpu_core=600.0, private_mb=2000, create_time=warm.clock.ts))

    render_on()
    run_until(warm, lambda: any(e.notify == "popup" for e in warm.alert_events()), 10 * 60)
    ep = next(e.episode for e in warm.alert_events() if e.notify == "popup")
    reply = warm.engine.respond(ep.id, "intentional")
    assert "blender.exe" in reply
    assert any(s.app == "blender.exe" for s in warm.engine.suppressions())
    # finish the render, wait out the cooldown, render again: no popup this time
    warm.platform.remove(7900)
    warm.run(40 * 60)
    warm.clear_events()
    render_on()
    warm.run(10 * 60)
    assert not [e for e in warm.alert_events() if e.notify == "popup" and e.episode.level < Level.UH_OH]
    st_after = warm.engine.baselines.lookup(9, "browsing", "cpu")[0]
    if mean_before is not None and st_after is not None:
        assert st_after.mean < mean_before + 5  # the render never became "normal"
    # undo in Settings: it can alert again later
    sid = next(s.id for s in warm.engine.suppressions() if s.app == "blender.exe")
    assert warm.engine.remove_suppression(sid)


def test_not_a_problem_is_recorded_for_the_false_alarm_count(warm):
    warm.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=560.0, private_mb=300, create_time=warm.clock.ts))
    run_until(warm, lambda: any(e.notify == "popup" for e in warm.alert_events()), 10 * 60)
    ep = next(e.episode for e in warm.alert_events() if e.notify == "popup")
    warm.engine.respond(ep.id, "not_a_problem")
    warm.engine.storage.flush()
    rows = warm.engine.storage.alerts_between(0, 1e12)
    assert any(r["response"] == "not_a_problem" for r in rows)


# ----------------------------------------------------------- quiet modes
def test_shh_mode_holds_and_summarises(warm):
    warm.platform.set(fullscreen=True)  # presentation
    warm.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=560.0, private_mb=300, create_time=warm.clock.ts))
    warm.run(8 * 60)
    assert not [e for e in warm.alert_events() if e.notify == "popup" and e.episode.level < Level.UH_OH]
    assert [e for e in warm.alert_events() if e.notify == "held"]
    warm.platform.set(fullscreen=False)
    took = run_until(warm, lambda: [e for e in warm.alert_events(("summary",))], 60)
    assert took is not None and took <= 60


def test_snooze_holds_heads_up(warm):
    warm.engine.snooze(3600)
    warm.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=560.0, private_mb=300, create_time=warm.clock.ts))
    warm.run(8 * 60)
    assert not [e for e in warm.alert_events() if e.notify == "popup" and e.episode.level < Level.UH_OH]
    warm.engine.snooze(0)
    took = run_until(warm, lambda: [e for e in warm.alert_events(("summary",))], 60)
    assert took is not None


# ------------------------------------------------------------- learning
def test_first_days_use_only_absolute_limits(tmp_path):
    sim = Simulation(tmp_path)
    normal_use(sim)
    sim.run(30 * 60)
    sim.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=560.0, private_mb=300, create_time=sim.clock.ts))
    sim.run(10 * 60)
    assert [e for e in sim.alert_events() if e.episode.level >= Level.HEADS_UP] == []
    assert sim.engine.latest.learning and sim.engine.latest.learning_day == 1
    sim.platform.set(volumes=[Volume("C:\\", 256 * GB, int(3 * GB))])
    took = run_until(sim, lambda: any(e.episode.level == Level.UH_OH for e in sim.alert_events()), 30)
    assert took is not None  # a nearly full disk is flagged even while learning
    sim.engine.stop()


# ----------------------------------------------------------- lifecycle
def test_restart_keeps_baselines_alerts_and_progress(warm):
    stats_before = {k: (v.n, round(v.mean, 6)) for k, v in warm.engine.baselines.stats.items()}
    warm.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=560.0, private_mb=300, create_time=warm.clock.ts))
    warm.run(8 * 60)
    n_alerts = len(warm.engine.inbox())
    eng = warm.restart()
    assert eng.first_ts is not None
    assert len(eng.storage.alerts_between(0, 1e12)) >= n_alerts > 0  # ...but history is kept
    for k, (n, _mean) in stats_before.items():
        assert eng.baselines.stats[k].n >= n
    assert eng.alerts._ids.__next__() > max(r["id"] for r in eng.storage.alerts_between(0, 1e12))


def test_sleep_gap_does_not_create_alerts(warm):
    warm.run(60)
    warm.jump_to(warm.clock.ts + 2 * 3600)   # laptop lid closed for two hours
    warm.platform.add(FakeProc("updater.exe", 6200, 1000, cpu_core=500.0, private_mb=300, create_time=warm.clock.ts))
    warm.run(150)  # post-resume storm inside the 3-minute grace
    assert [e for e in warm.alert_events() if e.episode.level >= Level.PSST] == []
    assert warm.engine.detector.grace_until > 0


def test_clock_change_and_dst_do_not_crash(warm):
    warm.clock.set_wall(warm.clock.ts - 3600)   # manual clock change backwards
    warm.run(120)
    warm.clock.set_wall(warm.clock.ts + 7200)   # and forwards
    warm.run(120)
    assert warm.engine.latest is not None and warm.engine.errors == 0


# ----------------------------------------------------------- quirks
def test_quirks_have_content_after_a_few_days(warm):
    e = warm.engine
    assert e.weather() is not None and "chance of" in e.weather()
    assert "switched between tasks" in e.os_fact()["text"]
    assert e.calm_streak() >= 3
    assert e.diary() and e.records()
    assert {a.key for a in e.achievements()} >= {"tab_tamer", "calm_week"}


def test_new_normal_after_several_days(tmp_path):
    cfg = Config(cold_start_days=0.0, new_normal_days=3)
    sim = Simulation(tmp_path, cfg=cfg)
    for day in (12, 13):
        sim.jump_to(local_ts(2026, 10, day, 10, 30, 0)) if day != 12 else None
        normal_use(sim)
        sim.run(3600)
    for day in (14, 15, 16):
        sim.jump_to(local_ts(2026, 10, day, 10, 0, 0))
        normal_use(sim)
        if 6100 not in sim.platform.procs:
            sim.platform.add(FakeProc("newjob.exe", 6100, 1000, cpu_core=400.0, private_mb=300, create_time=sim.clock.ts))
        sim.run(1800)
    assert any(e.kind == "notice" and e.data.id.startswith("relearn") for e in sim.events)
    sim.engine.stop()


# ---------------------------------------------------- stretch features
def test_speed_tests_health_score_and_checkup_suggestion(warm):
    eng = warm.engine
    for _ in range(4):
        r = eng.run_benchmark()
        assert r.valid
    assert eng.health_summary()["score"] is not None
    eng.bench.scale *= 6   # the PC got much slower
    for _ in range(2):
        eng.run_benchmark()
    assert eng.health_summary()["score"] < 80
    assert any(n.id == "checkup_suggest" for n in eng.active_notices())
    eng.dismiss_notice("checkup_suggest", declined_checkup=True)
    eng.run_benchmark()
    assert not any(n.id == "checkup_suggest" for n in eng.active_notices())  # waits 7 days after a "no"


def test_benchmark_is_excluded_from_detection(warm):
    eng = warm.engine
    eng.benchmark_running = True
    warm.platform.add(FakeProc("python.exe", 6300, warm.platform.self_pid(), cpu_core=780.0, private_mb=300,
                               create_time=warm.clock.ts, cmdline=["python", "-m", "mochi.bench_worker"]))
    moods = [v.mood for v in warm.run(6 * 60)]
    eng.benchmark_running = False
    assert warm.alert_events() == []
    assert Mood.ZOOMIES in moods
    assert warm.engine.latest.apps[0].key != "mochi"  # Mochi's own work is never a suspect


def test_checkup_end_to_end(warm, monkeypatch, tmp_path):
    from mochi.platform.base import DiskHealth, StartupItem

    eng = warm.engine
    warm.platform.disks = ([DiskHealth("NVMe SSD", "SSD", "healthy", "Windows Storage")], None)
    warm.platform.startup = [StartupItem(f"app{i}", f"app{i}.exe", "Run (you)") for i in range(12)]
    monkeypatch.setattr(eng, "_load_test", lambda stop: ([97.0, 96.0], []))
    (tmp_path / "scanme" / "big").mkdir(parents=True)
    (tmp_path / "scanme" / "big" / "f.bin").write_bytes(b"x" * 100_000)
    steps = []
    rep = eng.run_checkup(progress=lambda s, f: steps.append(s), quick_scan_root=str(tmp_path / "scanme"))
    keys = [i.key for i in rep.items]
    assert keys[:3] == ["disk_space", "disk_health", "startup"] and "health" in keys
    assert next(i for i in rep.items if i.key == "startup").status == "look"
    assert rep.top3 and steps[-1] == "Done"
    assert eng.checkup_history() and eng.last_checkup is rep
    assert "battery" in keys  # this simulated laptop has a battery


def test_slow_app_launches_suggest_a_checkup(warm):
    from mochi.startup_tracker import Launch
    import time as _t

    now = _t.time()
    for app in ("code.exe", "chrome.exe", "slack.exe"):
        for i in range(12):
            warm.engine._on_launch(Launch(app, app, now - 20 * 86400 + i * 3600, 2.0, False, True))
        for i in range(5):
            warm.engine._on_launch(Launch(app, app, now - 3600 + i * 60, 7.0, False, True))
    assert any(n.id == "slow_launches" for n in warm.engine.active_notices())


# ------------------------------------------------------------- privacy
def test_no_network_activity_during_a_busy_run(warm, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("Mochi tried to open a network socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    cb = hog_step(warm)
    warm.run(10 * 60, lambda t, v: cb(t))
    warm.engine.export_data()
    assert warm.alert_events()


# ---------------------------------------------------- false-alarm rate
@pytest.mark.slow
def test_simulated_week_false_alarm_rate(warm):
    """TST-6 (simulated): five ordinary noisy weekdays raise < 1 Heads-up/Uh-oh per day."""
    import random

    rng = random.Random(11)
    heads = 0
    for day in (19, 20, 21, 22, 23):
        warm.jump_to(local_ts(2026, 10, day, 9, 0, 0))
        normal_use(warm)
        warm.clear_events()

        def jitter(t, v):
            # short bursts (compiles, page loads) that a normal day has
            if rng.random() < 0.01:
                warm.platform.update(3000, cpu_core=rng.choice([4.0, 150.0, 300.0]))
            elif rng.random() < 0.05:
                warm.platform.update(3000, cpu_core=4.0)

        warm.run(3 * 3600, jitter)
        heads += len([e for e in warm.alert_events(("opened", "escalated")) if e.episode.level >= Level.HEADS_UP])
    assert heads / 5 < 1.0
