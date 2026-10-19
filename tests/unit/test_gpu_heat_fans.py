"""GPU load/temperature and fan speed through the whole pipeline (GPU-1..6)."""

import sqlite3
import sys

import pytest

from mochi import config as config_mod
from mochi.baseline import BaselineStore
from mochi.config import Config
from mochi.detector import Detector
from mochi.engine import Engine
from mochi.model import BASELINE_METRICS, GB, METRICS, WATCH_METRICS, Z_METRICS, Fan, derive_metrics
from mochi.platform.fake import FakeClock, FakePlatform
from mochi.sensors import LENOVO, SensorHelper, SensorReading
from mochi.storage import V1_SAMPLE_COLS, V3_AGG_COLS, V3_SAMPLE_COLS, Storage, connect, migrate

from ..conftest import make_sample, make_sys
from .test_sensors import Runas

DAY0 = 739_900


# ------------------------------------------------------------------- model
class TestModel:
    def test_metric_lists(self):
        assert set(WATCH_METRICS) == {"gpu", "gpu_temp", "fan"} and not set(WATCH_METRICS) & set(Z_METRICS)
        assert all(m in METRICS for m in BASELINE_METRICS)

    def test_derived_metrics(self):
        m = derive_metrics(make_sys(gpu_busy_pct=12.0, gpu_temp_c=50.0, fans=[Fan("Fan 1", 1800), Fan("Fan 2", 2600)]))
        assert (m["gpu"], m["gpu_temp"], m["fan"]) == (12.0, 50.0, 2600)
        m = derive_metrics(make_sys())
        assert m["gpu"] is None and m["gpu_temp"] is None and m["fan"] is None

    def test_fan_percent(self):
        assert Fan("F", 2700, 5400).pct == 50.0 and Fan("F", 9000, 5400).pct == 100.0 and Fan("F", 100).pct is None


# ---------------------------------------------------------------- platforms
class TestFakePlatform:
    def test_defaults_explain_missing_readings(self):
        raw = FakePlatform(FakeClock()).read_system()
        assert raw.gpu_busy_pct is None and raw.fans == []
        assert {"gpu", "gpu_temp", "fan"} <= set(raw.unavailable) and raw.unavailable["fan"].startswith("needs admin")

    def test_scripted_readings(self):
        p = FakePlatform(FakeClock())
        p.set(gpu_busy_pct=0.0, gpu_temp_c=55.0, gpu_mem_used_gb=1.5, gpu_mem_total_gb=8.0, gpu_power_w=20.0,
              gpu_name="Test GPU", fans=[Fan("Fan 1", 2000, 5000)])
        raw = p.read_system()
        assert raw.gpu_busy_pct == 0.0 and raw.gpu_mem_used == int(1.5 * GB) and raw.gpu_mem_total == 8 * GB
        assert "gpu" not in raw.unavailable and "fan" not in raw.unavailable
        assert raw.sources["gpu"] == "simulator" and raw.fans[0] is not p.sys.fans[0]   # a copy
        with pytest.raises(AttributeError):
            p.set(gpu_fan=1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows adapter")
class TestWindowsGpu:
    def test_busy_is_the_busiest_engine_summed_over_processes(self):
        from mochi.platform.windows import gpu_busy

        eng = {
            "pid_10_luid_0x0_0x1_phys_0_eng_0_engtype_3D": 30.0,
            "pid_11_luid_0x0_0x1_phys_0_eng_0_engtype_3D": 25.0,     # same engine, other app: adds up
            "pid_10_luid_0x0_0x1_phys_0_eng_3_engtype_VideoDecode": 40.0,
            "pid_12_luid_0x0_0x2_phys_0_eng_0_engtype_3D": 10.0,     # another GPU
            "pid_13_luid_0x0_0x1_phys_0_eng_5_engtype_Copy": -3.0,   # counter noise
        }
        assert gpu_busy(eng) == 55.0
        assert gpu_busy({}) == 0.0
        assert gpu_busy({"pid_1_luid_a_eng_0": 80.0, "pid_2_luid_a_eng_0": 70.0}) == 100.0

    RTX = ("PCI\\VEN_10DE&DEV_2860&SUBSYS_3E4317AA", "NVIDIA GeForce RTX 4070 Laptop GPU")
    IGPU = ("PCI\\VEN_8086&DEV_A788&SUBSYS_3E4317AA", "Intel(R) UHD Graphics")

    def _platform(self, monkeypatch, adapters, gpus):
        """``gpus``: what the driver would report per GPU when asked; records who was asked."""
        from mochi.platform import windows as w

        p = w.WindowsPlatform.__new__(w.WindowsPlatform)
        p._vram = {}
        p._pdh = type("P", (), {"failed": {}})()
        p.asked = []
        monkeypatch.setattr(w, "display_adapters", lambda: adapters)

        def fake_sensors(awake):
            out = []
            for g in gpus:
                g = w.GpuSensor(**g)
                if awake(g.ven_dev):
                    p.asked.append(g.ven_dev)
                else:
                    g.vram = g.temp_c = g.fan_rpm = None       # a sleeping GPU is only identified
                out.append(g)
            return out

        monkeypatch.setattr(w, "gpu_sensors", fake_sensors)
        return p

    RTX_GPU = dict(ven_dev="VEN_10DE&DEV_2860", vram=8 * GB, temp_c=47.0, temp_max_c=105.0)
    IGPU_GPU = dict(ven_dev="VEN_8086&DEV_A788", vram=128 * 2**20, temp_c=None)

    def test_awake_gpu_reports_temperature_and_memory(self, monkeypatch):
        p = self._platform(monkeypatch, [(*self.IGPU, 1), (*self.RTX, 1)], [self.IGPU_GPU, self.RTX_GPU])
        raw = make_sys()
        p._gpu(raw, {"gpu_engine": {"pid_1_luid_x_eng_0_engtype_3D": 3.0}, "gpu_mem": {"luid_x": 900 * 2**20}})
        assert (raw.gpu_busy_pct, raw.gpu_temp_c) == (3.0, 47.0)
        assert raw.gpu_mem_used == 900 * 2**20 and raw.gpu_mem_total == 8 * GB    # the discrete GPU is shown
        assert raw.gpu_name == "NVIDIA GeForce RTX 4070 Laptop GPU"
        assert raw.sources == {"gpu_temp": "graphics driver", "gpu": "GPU performance counters"}
        assert raw.fans == []                                                      # laptop: the firmware runs the fans

    def test_sleeping_gpu_is_never_asked(self, monkeypatch):
        p = self._platform(monkeypatch, [(*self.IGPU, 1), (*self.RTX, 1)], [self.IGPU_GPU, self.RTX_GPU])
        p._gpu(make_sys(), {"gpu_engine": {}})                                     # learn its memory size
        p.asked.clear()
        monkeypatch.setattr("mochi.platform.windows.display_adapters", lambda: [(*self.IGPU, 1), (*self.RTX, 4)])
        raw = make_sys()
        p._gpu(raw, {"gpu_engine": {}, "gpu_mem": {}})
        assert p.asked == ["VEN_8086&DEV_A788"]                                    # only the awake iGPU
        assert raw.gpu_temp_c is None and "asleep" in raw.unavailable["gpu_temp"]
        assert raw.gpu_name.startswith("NVIDIA") and raw.gpu_mem_total == 8 * GB and raw.gpu_busy_pct == 0.0

    def test_desktop_gpu_fan_is_listed(self, monkeypatch):
        gpu = dict(self.RTX_GPU, fan_rpm=1450.0, fan_max_rpm=3300.0)
        p = self._platform(monkeypatch, [(*self.RTX, 1)], [gpu])
        raw = make_sys()
        p._gpu(raw, {"gpu_engine": {}})
        assert [(f.label, f.rpm, f.max_rpm) for f in raw.fans] == [("GPU fan", 1450.0, 3300.0)]
        assert raw.sources["fan"] == "graphics driver"

    def test_no_counters_and_no_temperature(self, monkeypatch):
        p = self._platform(monkeypatch, [(*self.IGPU, 1)], [self.IGPU_GPU])
        raw = make_sys()
        p._gpu(raw, {})
        assert raw.gpu_busy_pct is None and raw.gpu_name == "Intel(R) UHD Graphics"
        assert raw.unavailable["gpu"] and "doesn't report its temperature" in raw.unavailable["gpu_temp"]

    def test_no_gpu_found(self, monkeypatch):
        p = self._platform(monkeypatch, [], [])
        raw = make_sys()
        p._gpu(raw, {"gpu_engine": {}})
        assert raw.gpu_name is None and raw.gpu_temp_c is None and raw.unavailable["gpu_temp"]


# ----------------------------------------------------------------- storage
class TestStorageV3:
    def test_upgrades_a_v2_database_in_place(self, tmp_path):
        db = tmp_path / "m.db"
        conn = connect(db)
        assert migrate(conn, upto=2) == 2
        conn.execute(f"INSERT INTO samples(ts, cpu, temp) VALUES (1.0, 12.0, 55.0)")
        conn.close()
        s = Storage(db, threaded=False)
        assert s.version == 3
        cols = {r[1] for r in s.query("PRAGMA table_info(samples)")}
        assert set(V1_SAMPLE_COLS) | set(V3_SAMPLE_COLS) == cols
        agg = {r[1] for r in s.query("PRAGMA table_info(agg_hour)")}
        assert set(V3_AGG_COLS) <= agg
        assert s.query("SELECT cpu, temp, gpu FROM samples") == [(12.0, 55.0, None)]   # old data kept
        s.close()

    def test_new_readings_are_saved_aggregated_and_charted(self, tmp_path):
        s = Storage(tmp_path / "m.db", threaded=False)
        t0 = 1_800_000_000.0 - 1_800_000_000.0 % 3600
        for i in range(24):
            smp = make_sample(ts=t0 + i * 5, sys=make_sys(gpu_busy_pct=10.0 + i, gpu_temp_c=50.0, gpu_power_w=12.0,
                                                          gpu_mem_used=2 * GB, fans=[Fan("Fan 1", 2000 + i)]))
            s.save_sample(smp, {"gpu": (12.0, 3.0), "fan": (2000.0, 150.0)}, None)
        rows = s.series("gpu", t0, t0 + 3600)
        assert len(rows) == 24 and rows[0][1:] == (10.0, 12.0, 3.0)
        assert s.series("gpu_temp", t0, t0 + 3600)[0][1:] == (50.0, None, None)
        assert s.query("SELECT gpu_mem, gpu_power FROM samples LIMIT 1") == [(2.0, 12.0)]
        s.maintain(t0 + 7200)
        minute = s.series("fan", t0, t0 + 3600, "minute")
        assert len(minute) == 2 and minute[0][1] == pytest.approx(2005.5) and minute[0][2] == 2000.0
        assert s.series("gpu", t0, t0 + 7200, "hour")[0][1] == pytest.approx(21.5)
        with pytest.raises(ValueError):
            s.series("gpu_fan", t0, t0 + 1)
        s.close()


# ---------------------------------------------------------------- detector
def watch_runner(metric_value_kw, cfg=None):
    cfg = cfg or Config(min_samples=20, min_days=2)
    store = BaselineStore(cfg)
    t = 1_000_000.0
    for day in (DAY0, DAY0 + 1):
        for _ in range(40):
            for m, v in (("gpu", 10.0), ("gpu_temp", 50.0), ("fan", 2000.0), ("cpu", 10.0)):
                store.queue(t, 10, "browsing", m, v + (t % 3), day)
            t += 5
    store.commit_all()
    return Detector(cfg, store), store


class TestWatchMetrics:
    def test_learn_a_band_but_never_alert(self):
        det, store = watch_runner({})
        t, mono, results = 2_000_000.0, 60_000.0, []
        for _ in range(130):    # 10+ minutes far above normal
            t += 5
            mono += 5
            s = make_sample(ts=t, mono=mono, sys=make_sys(gpu_busy_pct=99.0, gpu_temp_c=90.0,
                                                          fans=[Fan("Fan 1", 5400)]))
            results.append(det.process(s, 10, "browsing", DAY0 + 3))
        last = results[-1]
        assert last.band["gpu"] is not None and last.z["gpu"] > 10 and last.z["gpu_temp"] > 10 and last.z["fan"] > 10
        assert not [sig for r in results for sig in r.signals if sig.metric in WATCH_METRICS]
        store.commit_all()
        assert store.get("s10|browsing", "gpu").mean < 15.0       # the spike never became "normal"

    def test_normal_readings_are_learned(self):
        det, store = watch_runner({})
        n0 = store.get("s10|browsing", "fan").n
        s = make_sample(ts=2_000_000.0, mono=60_000.0, sys=make_sys(fans=[Fan("Fan 1", 2001)]))
        det.process(s, 10, "browsing", DAY0 + 3)
        store.commit_all()
        assert store.get("s10|browsing", "fan").n == n0 + 1

    def test_missing_reading_has_no_band(self):
        det, _ = watch_runner({})
        res = det.process(make_sample(ts=2_000_000.0, mono=60_000.0), 10, "browsing", DAY0 + 3)
        assert res.z["gpu"] is None and res.band["fan"] is None


# ------------------------------------------------------------------ engine
@pytest.fixture
def eng(paths):
    fake = FakePlatform(FakeClock())
    e = Engine(fake, paths, Config(), threaded=False, start_threads=False, startup_watcher=False,
               sensor_helper=SensorHelper(paths.root, lambda: False, runas=Runas(42)))
    yield e, fake, paths
    e.stop()


def write_feed(paths, ts, **kw):
    d = dict(ts=ts, source=LENOVO, cpu_temp=63.0, gpu_temp=None, fans=[Fan("Fan 1", 2400, 5400), Fan("Fan 2", 2500, 5400)])
    d.update(kw)
    (paths.root / "sensors.json").write_text(SensorReading(**d).to_json(), encoding="utf-8")


class TestEngine:
    def test_helper_feed_reaches_the_view_and_the_database(self, eng):
        e, fake, paths = eng
        fake.set(gpu_busy_pct=7.0, gpu_temp_c=45.0, gpu_name="Test GPU", gpu_mem_used_gb=1.0, gpu_mem_total_gb=8.0)
        write_feed(paths, e.clock.time())
        v = e.tick()
        assert (v.cpu_temp, v.gpu_temp, v.gpu_busy, [f.rpm for f in v.fans]) == (63.0, 45.0, 7.0, [2400, 2500])
        assert v.metrics["fan"] == 2500 and v.metrics["temp"] == 63.0 and v.gpu_mem_total_gb == 8.0
        assert v.sources["temp"] == LENOVO and v.sources["gpu_temp"] == "simulator"
        assert "temp" not in v.unavailable and "fan" not in v.unavailable
        e.storage.flush()
        assert e.storage.query("SELECT temp, gpu, gpu_temp, fan FROM samples") == [(63.0, 7.0, 45.0, 2500)]
        assert e.timeline("fan", 3600)["rows"][0][1] == 2500

    def test_stale_feed_is_ignored(self, eng):
        e, _, paths = eng
        write_feed(paths, e.clock.time() - 120)
        v = e.tick()
        assert v.cpu_temp is None and v.fans == [] and v.unavailable["temp"].startswith("needs admin")

    @pytest.mark.parametrize("state, age, text", [
        ("asking", 0, "waiting for Windows"), ("running", 1, "starting"), ("running", 99, "isn't reporting"),
        ("declined", 0, "didn't give permission"), ("off", 0, "needs admin")])
    def test_missing_reason_follows_the_helper(self, eng, monkeypatch, state, age, text):
        e, _, _ = eng
        h = e.sensor_helper
        h.state = state
        h.message = "Windows didn't give permission, so CPU temperature and fans stay off." if state == "declined" else ""
        monkeypatch.setattr(h, "age", lambda: age)
        v = e.tick()
        assert text in v.unavailable["temp"] and text in v.unavailable["fan"]

    def test_turning_the_helper_on_and_off(self, eng):
        e, _, paths = eng
        assert e.set_sensor_helper(True) == []
        e.sensor_helper._thread.join(5)
        assert e.sensor_helper.state == "running" and e.base_cfg.sensor_helper
        assert config_mod.load(paths.config).config.sensor_helper
        e.set_sensor_helper(False)
        assert e.sensor_helper.state == "off" and (paths.root / "sensors.stop").exists()
        assert not config_mod.load(paths.config).config.sensor_helper

    def test_helper_starts_with_mochi_when_turned_on(self, paths):
        runas = Runas(42)
        cfg = Config(sensor_helper=True, sample_interval_s=0.1)
        e = Engine(FakePlatform(FakeClock()), paths, cfg, startup_watcher=False,
                   sensor_helper=SensorHelper(paths.root, lambda: False, runas=runas))
        try:
            e.sensor_helper._thread.join(5)
            assert len(runas.calls) == 1 and e.sensor_helper.state == "running"
        finally:
            e.stop()
        assert (paths.root / "sensors.stop").exists()          # quitting Mochi stops the helper

    def test_not_started_unless_asked(self, paths):
        runas = Runas(42)
        e = Engine(FakePlatform(FakeClock()), paths, Config(sample_interval_s=0.1), startup_watcher=False,
                   sensor_helper=SensorHelper(paths.root, lambda: False, runas=runas))
        e.stop()
        assert not runas.calls

    def test_no_helper_off_windows(self, paths):
        e = Engine(FakePlatform(FakeClock()), paths, Config(), threaded=False, start_threads=False,
                   startup_watcher=False)
        assert e.sensor_helper is None
        assert e.set_sensor_helper(True) and e.tick().unavailable["fan"]
        e.stop()


class TestConfig:
    def test_sensor_helper_setting(self):
        assert Config().sensor_helper is False
        res = config_mod.from_dict({"sensor_helper": "yes"})
        assert res.config.sensor_helper is False and res.warnings
        assert config_mod.from_dict({"sensor_helper": True}).config.sensor_helper is True
