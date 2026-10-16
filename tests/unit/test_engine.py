"""Engine threads and API (LIF-3, LIF-7, COL-9, UI-6) on the scripted platform."""

import time

import pytest

from mochi.config import Config
from mochi.engine import Engine, SampleView
from mochi.model import Level
from mochi.platform.fake import FakeClock, FakePlatform, FakeProc
from mochi.replay import Simulation, normal_use


def fast_cfg(**kw):
    c = Config(sample_interval_s=0.1, **kw)
    return c


def wait_for(cond, timeout=10.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def threaded(paths):
    fake = FakePlatform(FakeClock())
    eng = Engine(fake, paths, fast_cfg(), startup_watcher=False)
    yield eng, fake
    eng.stop()


class TestThreads:
    def test_collects_on_its_own(self, threaded):
        eng, _ = threaded
        assert wait_for(lambda: eng.latest is not None and len(eng.views) >= 5)
        st = eng.status()
        assert st["threads"]["collector"] and st["threads"]["processing"] and st["threads"]["supervisor"]
        assert st["last_sample_age"] is not None and not st["stale"]

    def test_collector_survives_read_errors(self, threaded):
        eng, fake = threaded
        wait_for(lambda: eng.latest is not None)
        fake.fail_reads = 3
        n = len(eng.views)
        assert wait_for(lambda: len(eng.views) > n + 3)
        assert eng.errors >= 3

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_supervisor_restarts_a_dead_thread(self, threaded, monkeypatch):
        eng, _ = threaded
        wait_for(lambda: eng.latest is not None)
        original = eng.process_sample
        calls = {"n": 0}

        def dying(sample):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit("simulated thread death")  # BaseException kills the thread
            return original(sample)

        monkeypatch.setattr(eng, "process_sample", dying)
        assert wait_for(lambda: eng.restarts >= 1, timeout=10)
        n = len(eng.views)
        assert wait_for(lambda: len(eng.views) > n + 2)

    def test_bounded_queue_drops_oldest(self, paths, monkeypatch):
        fake = FakePlatform(FakeClock())
        eng = Engine(fake, paths, fast_cfg(queue_size=2), startup_watcher=False, start_threads=False, threaded=True)
        monkeypatch.setattr(eng, "process_sample", lambda s: time.sleep(0.5))
        eng.start()
        try:
            assert wait_for(lambda: eng.drops >= 3, timeout=10)
            assert eng.queue.qsize() <= 2
        finally:
            eng.stop()

    def test_stop_is_fast_and_saves(self, paths):
        fake = FakePlatform(FakeClock())
        eng = Engine(fake, paths, fast_cfg(), startup_watcher=False)
        wait_for(lambda: len(eng.views) >= 5)
        took = eng.stop(timeout=2.0)
        assert took < 2.5
        from mochi.storage import Storage

        s = Storage(paths.db, threaded=False)
        assert s.scalar("SELECT COUNT(*) FROM samples") >= 3
        s.close()

    def test_stale_status(self, threaded):
        eng, _ = threaded
        wait_for(lambda: eng.latest is not None)
        eng.last_sample_mono = time.monotonic() - 60
        assert eng.status()["stale"]


@pytest.fixture
def sim(tmp_path):
    s = Simulation(tmp_path, cfg=Config(cold_start_days=0.0, min_days=1, min_samples=20))
    normal_use(s)
    s.run(600)
    yield s
    s.engine.stop()


class TestApi:
    def test_view_fields(self, sim):
        v = sim.engine.latest
        assert isinstance(v, SampleView)
        assert v.ram_total_gb == pytest.approx(16.0) and v.primary_volume[0] == "C:\\"
        assert v.mood_label[0] and v.speech and v.activity == "browsing"
        assert v.self_mem_mb > 0  # Mochi's own memory is visible in Nerd stats (UI-6)
        assert v.unavailable["temp"]

    def test_snooze_and_resume(self, sim):
        until = sim.engine.snooze(3600)
        assert until == pytest.approx(sim.clock.ts + 3600)
        tomorrow = sim.engine.snooze(None)
        assert tomorrow > sim.clock.ts
        assert sim.engine.snooze(0) == 0.0
        assert sim.engine.storage.get_meta("snoozed_until") == "0.0"

    def test_update_config_validates_and_persists(self, sim):
        warnings = sim.engine.update_config(sensitivity=4, alpha="bad")
        assert sim.engine.cfg.sensitivity == 4 and warnings
        from mochi import config as config_mod

        assert config_mod.load(sim.paths.config).config.sensitivity == 4
        sim.engine.update_config(demo_mode=True)
        assert sim.engine.cfg.heads_up_s < 300 and sim.engine.detector.cfg.heads_up_s < 300

    def test_autostart_toggle_uses_registry_abstraction(self, sim):
        from mochi.autostart import Autostart, MemoryRegistry

        sim.engine.autostart = Autostart(MemoryRegistry())
        sim.engine.update_config(autostart=True)
        assert sim.engine.autostart.is_enabled()
        sim.engine.update_config(autostart=False)
        assert not sim.engine.autostart.is_enabled()

    def test_mapping_edits_apply_forward(self, sim):
        sim.platform.add(FakeProc("blender.exe", 4000, cpu_core=1))
        sim.platform.set(foreground_pid=4000)
        sim.run(90)
        assert sim.engine.latest.activity == "other"
        sim.engine.set_mapping("Blender.exe", "media")
        sim.run(90)
        assert sim.engine.latest.activity == "media"
        assert '"blender.exe": "media"' in sim.paths.activity_map.read_text()
        sim.engine.set_mapping("blender.exe", None)
        assert "blender.exe" not in sim.engine.mapper.mapping

    def test_respond_unknown_episode_and_bad_kind(self, sim):
        assert sim.engine.respond(9999, "intentional") == "That alert has already gone."

    def test_notices(self, sim):
        sim.engine._notice("x", 1.0, "T", "text", "run_checkup")
        sim.engine._notice("x", 2.0, "T2", "text")  # duplicate ignored while active
        assert [n.title for n in sim.engine.active_notices()] == ["T"]
        sim.engine.dismiss_notice("x", declined_checkup=True)
        assert sim.engine.active_notices() == [] and sim.engine.storage.get_meta("checkup_declined_ts")

    def test_act_marks_episode(self, sim):
        from mochi.diagnosis import Action

        sim.platform.add(FakeProc("hog.exe", 4100, cpu_core=1, create_time=sim.clock.ts - 10))
        sim.platform.close_exits.add(4100)
        res = sim.engine.act(Action("end_process", "End", {"pid": 4100, "create_time": sim.clock.ts - 10,
                                                           "app": "hog.exe", "display": "hog.exe"}))
        assert res.ok
        assert sim.engine.act(Action("show_detective", "Show")).ok
        assert sim.engine.act(Action("open_location", "Where", {"path": None})).ok is False

    def test_queries_do_not_crash_on_fresh_data(self, sim):
        e = sim.engine
        assert e.timeline("cpu", 3600)["rows"]
        assert e.timeline("ram", 7 * 86400)["resolution"] == "minute"
        assert e.detective()[0]["app"] == "chrome.exe"
        assert e.weather() is None  # not enough days yet
        assert e.os_fact()["ctx_switches"] > 0
        assert e.calm_streak() >= 0 and isinstance(e.records(), dict)
        assert [a.key for a in e.achievements()]
        assert e.health_summary()["score"] is None and e.checkup_history() == []
        assert e.inbox() == [] and e.suppressions() == []

    def test_delete_all_resets_learning(self, sim):
        sim.engine.delete_all_data()
        assert sim.engine.first_ts is None and not sim.engine.baselines.stats
        assert sim.engine.storage.scalar("SELECT COUNT(*) FROM samples") == 0
        sim.run(10)
        assert sim.engine.first_ts is not None

    def test_export(self, sim):
        out = sim.engine.export_data()
        assert (out / "samples.csv").exists()


def test_stop_twice_is_safe(paths):
    eng = Engine(FakePlatform(FakeClock()), paths, Config(), threaded=False, start_threads=False, startup_watcher=False)
    eng.tick()
    assert eng.stop() >= 0.0
    assert eng.stop() == 0.0
