"""CPU/GPU temperature and fan sensors: feed file, vendor readers, the elevated helper (SEN-1..6)."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from mochi import sensor_helper
from mochi.model import Fan, SystemRaw
from mochi.sensors import (ACPI, LENOVO, LHM, LenovoGameZone, SensorError, SensorFeed, SensorHelper, SensorReading,
                           helper_command, merge, plausible_temp, read_lhm)

NOW = 1_800_000_000.0


def reading(**kw):
    d = dict(ts=NOW, source=LENOVO, cpu_temp=61.0, gpu_temp=52.0, fans=[Fan("Fan 1", 2350, 5400), Fan("Fan 2", 0, 5400)])
    d.update(kw)
    return SensorReading(**d)


# ------------------------------------------------------------------ fake WMI
class Prop:
    def __init__(self, name, value):
        self.Name, self.Value = name, value


class Out:
    def __init__(self, **props):
        self.Properties_ = [Prop(k, v) for k, v in props.items()]


class FakeGameZone:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def ExecMethod_(self, method):
        self.calls.append(method)
        v = self.values.get(method)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise RuntimeError("method not supported")
        return Out(ReturnValue=0, Data=v)


class FakeWmi:
    def __init__(self, objs=(), fail=None):
        self.objs, self.fail = list(objs), fail

    def ExecQuery(self, _q):
        if self.fail:
            raise self.fail
        return self.objs


class Row:
    def __init__(self, kind, name, value, ident):
        self.SensorType, self.Name, self.Value, self.Identifier = kind, name, value, ident


GZ_VALUES = {"GetCPUTemp": 61, "GetGPUTemp": 52, "GetFan1Speed": 2350, "GetFan2Speed": 2600, "GetFanMaxSpeed": 5400}


# ----------------------------------------------------------- reading / JSON
class TestReading:
    def test_json_roundtrip(self):
        r = reading()
        back = SensorReading.from_json(r.to_json())
        assert back == r and back.fans[0].pct == pytest.approx(2350 / 5400 * 100)

    @pytest.mark.parametrize("v, ok", [(45, 45.0), (45.5, 45.5), (0, None), (255, None), (-5, None), (True, None),
                                       ("60", None), (float("nan"), None), (None, None)])
    def test_plausible_temperature(self, v, ok):
        assert plausible_temp(v) == ok

    @pytest.mark.parametrize("payload", [
        "[]", "not json", json.dumps({"source": LENOVO}),                       # no timestamp
        json.dumps({"ts": NOW, "source": ""}), json.dumps({"ts": NOW, "source": "x" * 41}),
        json.dumps({"ts": NOW, "source": LENOVO, "fans": "fast"}),
        json.dumps({"ts": NOW, "source": LENOVO, "fans": [{"label": "F", "rpm": 1}] * 9}),
        json.dumps({"ts": NOW, "source": LENOVO, "fans": [{"label": "", "rpm": 1}]}),
        json.dumps({"ts": NOW, "source": LENOVO, "fans": [{"label": "F", "rpm": -1}]}),
        json.dumps({"ts": NOW, "source": LENOVO, "fans": [{"label": "F", "rpm": 99999}]}),
        json.dumps({"ts": True, "source": LENOVO}),
    ])
    def test_malformed_feed_is_rejected(self, payload):
        with pytest.raises(ValueError):
            SensorReading.from_json(payload)

    def test_implausible_temperatures_become_missing(self):
        r = SensorReading.from_json(json.dumps({"ts": NOW, "source": LENOVO, "cpu_temp": 0, "gpu_temp": 300}))
        assert r.cpu_temp is None and r.gpu_temp is None and r.empty


class TestMerge:
    def test_fills_missing_readings_and_clears_reasons(self):
        raw = SystemRaw(unavailable={"temp": "needs admin", "fan": "needs admin", "gpu_temp": "x"})
        merge(raw, reading())
        assert raw.temp_c == 61.0 and raw.gpu_temp_c == 52.0 and [f.rpm for f in raw.fans] == [2350, 0]
        assert raw.sources == {"temp": LENOVO, "gpu_temp": LENOVO, "fan": LENOVO}
        assert not {"temp", "fan", "gpu_temp"} & set(raw.unavailable)

    def test_platform_readings_win_except_acpi(self):
        raw = SystemRaw(temp_c=70.0, gpu_temp_c=48.0, fans=[Fan("CPU fan", 1800)],
                        sources={"temp": "x", "gpu_temp": "graphics driver", "fan": LHM})
        merge(raw, reading())
        assert (raw.temp_c, raw.gpu_temp_c, raw.fans[0].label) == (70.0, 48.0, "CPU fan")
        raw = SystemRaw(temp_c=27.9, sources={"temp": ACPI})
        merge(raw, reading())
        assert raw.temp_c == 61.0 and raw.sources["temp"] == LENOVO

    def test_driver_gpu_fan_and_helper_fans_together(self):
        raw = SystemRaw(fans=[Fan("GPU fan", 1450)], sources={"fan": "graphics driver"})
        merge(raw, reading())
        assert [f.label for f in raw.fans] == ["GPU fan", "Fan 1", "Fan 2"]
        assert raw.sources["fan"] == f"graphics driver + {LENOVO}"
        merge(raw, reading())                                       # never listed twice
        assert len(raw.fans) == 3
        raw = SystemRaw(fans=[Fan("cpu_fan", 900)])                 # a platform that names no source
        merge(raw, reading())
        assert len(raw.fans) == 3 and raw.sources["fan"] == f"this PC + {LENOVO}"
        raw = SystemRaw(fans=[Fan("Fan #1", 900)], sources={"fan": f"graphics driver + {LHM}"})
        merge(raw, reading())
        assert len(raw.fans) == 1                                   # LibreHardwareMonitor already lists them all

    def test_nothing_to_merge(self):
        raw = SystemRaw(unavailable={"temp": "needs admin"})
        merge(raw, None)
        merge(raw, reading(cpu_temp=None, gpu_temp=None, fans=[]))
        assert raw.temp_c is None and raw.unavailable["temp"] == "needs admin"


class TestFeed:
    def test_missing_fresh_stale_and_broken(self, tmp_path):
        feed = SensorFeed(tmp_path / "sensors.json", max_age_s=20)
        assert feed.read(NOW) is None
        (tmp_path / "sensors.json").write_text(reading().to_json(), encoding="utf-8")
        assert feed.read(NOW + 5).cpu_temp == 61.0
        assert feed.read(NOW + 60) is None                     # the helper stopped writing
        (tmp_path / "sensors.json").write_text("{broken", encoding="utf-8")
        assert feed.read(NOW) is None and feed.errors == 1

    def test_sees_a_rewrite_even_with_the_same_timestamp(self, tmp_path):
        path = tmp_path / "sensors.json"
        path.write_text(reading().to_json(), encoding="utf-8")
        st = path.stat()
        feed = SensorFeed(path)
        assert feed.read(NOW).cpu_temp == 61.0
        path.write_text(reading(cpu_temp=75.0).to_json(), encoding="utf-8")       # same length
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))                       # and same mtime
        assert feed.read(NOW).cpu_temp == 75.0


# ------------------------------------------------------------------ readers
class TestLenovo:
    def test_reads_temperatures_and_fans(self):
        gz = FakeGameZone(dict(GZ_VALUES))
        r = LenovoGameZone(FakeWmi([gz])).read()
        assert r.source == LENOVO and r.cpu_temp == 61 and r.gpu_temp == 52
        assert [(f.label, f.rpm, f.max_rpm) for f in r.fans] == [("Fan 1", 2350, 5400), ("Fan 2", 2600, 5400)]

    def test_unsupported_methods_and_placeholders(self):
        vals = dict(GZ_VALUES, GetFan2Speed=RuntimeError("no"), GetCPUTemp=0, GetFanMaxSpeed=100)
        r = LenovoGameZone(FakeWmi([FakeGameZone(vals)])).read()
        assert r.cpu_temp is None and len(r.fans) == 1 and r.fans[0].max_rpm is None

    def test_value_falls_back_to_first_number(self):
        class Odd(FakeGameZone):
            def ExecMethod_(self, method):
                return Out(ReturnValue=0, Temp=58)
        assert LenovoGameZone(FakeWmi([Odd({})])).read().cpu_temp == 58

    @pytest.mark.parametrize("wmi", [FakeWmi([]), FakeWmi(fail=PermissionError("Access denied"))])
    def test_not_available(self, wmi):
        with pytest.raises(SensorError):
            LenovoGameZone(wmi)


class TestLibreHardwareMonitor:
    def test_picks_package_gpu_core_and_fans(self):
        rows = [Row("Temperature", "Core #1", 70, "/intelcpu/0/temperature/1"),
                Row("Temperature", "CPU Package", 74, "/intelcpu/0/temperature/0"),
                Row("Temperature", "GPU Hot Spot", 66, "/gpu-nvidia/0/temperature/2"),
                Row("Temperature", "GPU Core", 58, "/gpu-nvidia/0/temperature/0"),
                Row("Temperature", "Drive", 40, "/nvme/0/temperature/0"),
                Row("Fan", "Fan #1", 2100, "/lpc/it8689e/fan/0"),
                Row("Load", "CPU Total", 12, "/intelcpu/0/load/0")]
        r = read_lhm(FakeWmi(rows))
        assert (r.source, r.cpu_temp, r.gpu_temp) == (LHM, 74, 58) and [f.rpm for f in r.fans] == [2100]

    def test_not_running_or_no_sensors(self):
        assert read_lhm(FakeWmi(fail=OSError("Invalid namespace"))) is None
        assert read_lhm(FakeWmi([Row("Load", "CPU Total", 12, "/intelcpu/0/load/0")])) is None


# ------------------------------------------------------------------- helper
class Runas:
    def __init__(self, rc=42, exc=None):
        self.rc, self.exc, self.calls = rc, exc, []

    def __call__(self, exe, args, cwd):
        self.calls.append((exe, args, cwd))
        if self.exc:
            raise self.exc
        return self.rc


class TestHelperLauncher:
    def test_command_line(self, tmp_path):
        exe, args = helper_command(tmp_path / "sensors.json", tmp_path / "sensors.stop", 1234, 3.0)
        assert args[:2] == ["-m", "mochi.sensor_helper"]
        assert args[args.index("--out") + 1] == str(tmp_path / "sensors.json")
        assert args[args.index("--parent") + 1] == "1234" and args[args.index("--interval") + 1] == "3"
        if Path(sys.executable).with_name("pythonw.exe").exists():
            assert exe.lower().endswith("pythonw.exe")      # no console window

    def test_asks_windows_without_admin(self, tmp_path):
        runas = Runas(42)
        (tmp_path / "sensors.stop").write_text("stop")       # left over from last time
        h = SensorHelper(tmp_path, lambda: False, runas=runas)
        h.start(777, wait=True)
        assert h.state == "running" and len(runas.calls) == 1 and h.age() >= 0
        assert not (tmp_path / "sensors.stop").exists()
        h.start(777, wait=True)
        assert len(runas.calls) == 1                         # already running: no second prompt

    # ShellExecuteW: > 32 is success; 5 means the person said no to the consent prompt.
    @pytest.mark.parametrize("rc, state", [(5, "declined"), (2, "failed"), (31, "failed"), (0, "failed")])
    def test_refused_or_broken(self, tmp_path, rc, state):
        h = SensorHelper(tmp_path, lambda: False, runas=Runas(rc))
        h.start(777, wait=True)
        assert h.state == state and h.message and h.age() == 0.0

    def test_launch_error(self, tmp_path):
        h = SensorHelper(tmp_path, lambda: False, runas=Runas(exc=OSError("boom")))
        h.start(777, wait=True)
        assert h.state == "failed" and "OSError" in h.message

    def test_admin_starts_directly(self, tmp_path):
        spawned, runas = [], Runas()
        h = SensorHelper(tmp_path, lambda: True, runas=runas, spawn=lambda cmd, **kw: spawned.append((cmd, kw)))
        h.start(777, wait=True)
        assert h.state == "running" and not runas.calls and spawned[0][0][1:3] == ["-m", "mochi.sensor_helper"]

    def test_stop_writes_the_stop_file(self, tmp_path):
        h = SensorHelper(tmp_path, lambda: False, runas=Runas(42))
        h.stop()
        assert not (tmp_path / "sensors.stop").exists() and h.state == "off"   # nothing was running
        h.start(1, wait=True)
        h.stop()
        assert (tmp_path / "sensors.stop").exists() and h.state == "off" and h.message == ""


class TestHelperProcess:
    def test_writes_readings_until_mochi_exits(self, tmp_path):
        out, stop = tmp_path / "sensors.json", tmp_path / "sensors.stop"
        alive = iter([True, True, False])
        why = sensor_helper.run(out, stop, 1, 1.0, lambda: reading(ts=time.time()), alive=lambda pid: next(alive),
                                sleep=lambda s: None)
        assert why == "Mochi exited" and SensorReading.from_json(out.read_text()).cpu_temp == 61.0

    def test_stop_file_ends_it(self, tmp_path):
        stop = tmp_path / "sensors.stop"
        stop.write_text("stop")
        assert sensor_helper.run(tmp_path / "sensors.json", stop, 1, 1.0, reading, alive=lambda p: True) == "stop file"

    def test_gives_up_when_sensors_never_answer(self, tmp_path):
        def boom():
            raise RuntimeError("wmi died")
        why = sensor_helper.run(tmp_path / "sensors.json", tmp_path / "s.stop", 1, 1.0, boom, alive=lambda p: True,
                                sleep=lambda s: None, max_failures=3)
        assert why == "sensors stopped answering" and not (tmp_path / "sensors.json").exists()
        empty = lambda: reading(cpu_temp=None, gpu_temp=None, fans=[])  # noqa: E731
        assert sensor_helper.run(tmp_path / "sensors.json", tmp_path / "s.stop", 1, 1.0, empty, alive=lambda p: True,
                                 sleep=lambda s: None, max_failures=2) == "sensors stopped answering"

    def test_main_only_writes_its_own_feed_file(self, tmp_path):
        assert sensor_helper.main(["--out", str(tmp_path / "evil.txt"), "--stop", str(tmp_path / "s"), "--parent", "1"]) == 2
        assert not (tmp_path / "evil.txt").exists()

    def test_main_cleans_up_its_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sensor_helper, "default_reader", lambda: lambda: reading(ts=time.time()))
        monkeypatch.setattr(sensor_helper, "parent_alive", lambda pid: False)
        out = tmp_path / "sensors.json"
        out.write_text("old")
        assert sensor_helper.main(["--out", str(out), "--stop", str(tmp_path / "s"), "--parent", "1"]) == 0
        assert not out.exists()

    def test_atomic_write_leaves_no_temp_files(self, tmp_path):
        out = tmp_path / "sensors.json"
        out.write_text("old")
        sensor_helper.write_atomic(out, reading().to_json())
        sensor_helper.write_atomic(out, reading(cpu_temp=70.0).to_json())
        assert SensorReading.from_json(out.read_text()).cpu_temp == 70.0
        assert [p.name for p in tmp_path.iterdir()] == ["sensors.json"]

    @pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
    def test_refuses_folders_redirected_by_a_junction(self, tmp_path):
        import _winapi

        target = tmp_path / "elsewhere"
        target.mkdir()
        jn = tmp_path / "profile"
        _winapi.CreateJunction(str(target), str(jn))      # no admin needed: a classic redirect trick
        assert sensor_helper.is_link(jn) and not sensor_helper.is_link(target)
        assert not sensor_helper.safe_folder(jn)
        (target / "inner").mkdir()
        assert not sensor_helper.safe_folder(jn / "inner")   # the parent is redirected
        assert sensor_helper.safe_folder(target) and not sensor_helper.safe_folder(tmp_path / "missing")
        rc = sensor_helper.main(["--out", str(jn / "sensors.json"), "--stop", str(tmp_path / "s"), "--parent", "1"])
        assert rc == 2 and [p.name for p in target.iterdir()] == ["inner"]   # nothing written through it

    def test_refuses_relative_paths(self):
        assert sensor_helper.main(["--out", "sensors.json", "--stop", "s", "--parent", "1"]) == 2

    def test_default_reader_falls_back_to_lhm(self, monkeypatch):
        def no_lenovo():
            raise SensorError("Access denied")
        monkeypatch.setattr(sensor_helper, "LenovoGameZone", no_lenovo)
        assert sensor_helper.default_reader() is read_lhm
