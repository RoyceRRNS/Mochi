"""GPU, temperature and fan readings on Home, Timeline and Settings (GPU-UI-1..6)."""

from __future__ import annotations

import pytest

from mochi.model import Fan
from mochi.sensors import SensorHelper
from mochi.ui.screens.home import short_gpu_name, temp_state

from .conftest import pump

pytestmark = pytest.mark.ui

RTX = "NVIDIA GeForce RTX 4070 Laptop GPU"


class Runas:
    def __init__(self):
        self.calls = []

    def __call__(self, exe, args, cwd):
        self.calls.append(args)
        return 42


@pytest.fixture
def with_helper(sim, tmp_path):
    """A Windows-like engine: the elevated helper exists (Windows' consent prompt is faked)."""
    runas = Runas()
    sim.engine.sensor_helper = SensorHelper(tmp_path, lambda: False, runas=runas)
    sim.platform.sys.unavailable["temp"] = "needs admin on this PC"
    return runas


def show(ctrl, sim, qtbot, **readings):
    if readings:
        sim.platform.set(**readings)
        if "temp_c" in readings:
            sim.platform.sys.unavailable.pop("temp", None)
    pump(qtbot, sim, 10)
    ctrl.bridge.sample.emit(sim.engine.latest)
    qtbot.wait(1)
    return ctrl.window.pages["home"]


FULL = dict(gpu_busy_pct=14.0, gpu_temp_c=52.0, gpu_mem_used_gb=1.1, gpu_mem_total_gb=8.0, gpu_power_w=18.0,
            gpu_name=RTX, temp_c=61.0, fans=[Fan("Fan 1", 2350, 5400), Fan("Fan 2", 2600, 5400)])


class TestHelpers:
    @pytest.mark.parametrize("name, short", [(RTX, "RTX 4070 Laptop GPU"), ("AMD Radeon(TM) 780M", "Radeon 780M"),
                                             ("Intel(R) UHD Graphics", "Intel UHD Graphics"), (None, "GPU"), ("", "GPU")])
    def test_short_gpu_name(self, name, short):
        assert short_gpu_name(name) == short

    @pytest.mark.parametrize("t, z, state", [(None, None, "off"), (50, None, "good"), (50, 3.5, "warning"),
                                             (82, None, "warning"), (88, None, "critical")])
    def test_temp_state(self, t, z, state):
        assert temp_state(t, 80.0, 87.0, z) == state


class TestHomeTiles:
    def test_all_readings(self, ctrl, sim, qtbot):
        home = show(ctrl, sim, qtbot, **FULL)
        assert home.t_cpu_temp.value_label.text() == "61 °C" and home.t_cpu_temp.state == "good"
        assert home.t_gpu_temp.value_label.text() == "52 °C"
        assert home.t_gpu.value_label.text() == "14% busy" and home.t_gpu.title_label.text() == "RTX 4070 Laptop GPU"
        assert "1.1 of 8 GB memory · 18 W" in home.t_gpu.sub_label.text() and home.t_gpu.bar.isVisibleTo(home)
        assert home.t_fans.value_label.text() == "2,600 RPM"
        assert "Fan 1 2,350 · Fan 2 2,600" in home.t_fans.sub_label.text() and home.t_fans.bar.isVisibleTo(home)
        assert not any(t.action.isVisibleTo(home) for t in (home.t_cpu_temp, home.t_fans))
        assert "GPU: 14% busy" in home.t_gpu.accessibleDescription()

    def test_hot_readings_turn_red(self, ctrl, sim, qtbot):
        home = show(ctrl, sim, qtbot, **dict(FULL, temp_c=95.0, gpu_temp_c=89.0))
        assert home.t_cpu_temp.state == "critical" and home.t_gpu_temp.state == "critical"
        assert home.t_cpu_temp.dot.token == "critical_strong"

    def test_stopped_fans_and_unknown_maximum(self, ctrl, sim, qtbot):
        home = show(ctrl, sim, qtbot, fans=[Fan("CPU fan", 0)])
        assert home.t_fans.value_label.text() == "Stopped (quiet)" and not home.t_fans.bar.isVisibleTo(home)

    def test_missing_readings_explain_why(self, ctrl, sim, qtbot):
        home = show(ctrl, sim, qtbot)
        for tile in (home.t_cpu_temp, home.t_gpu_temp, home.t_gpu, home.t_fans):
            assert tile.value_label.text() == "--" and tile.sub_label.text() and tile.state == "off"
            assert not tile.action.isVisibleTo(home)          # no helper off Windows: nothing to turn on

    def test_turn_on_from_home(self, with_helper, ctrl, sim, qtbot):
        home = show(ctrl, sim, qtbot)
        assert home.t_fans.action.isVisibleTo(home) and home.t_cpu_temp.action.text() == "Turn on"
        assert "Windows asks you first" in home.t_fans.sub_label.text()
        assert not home.t_gpu_temp.action.isVisibleTo(home)   # the helper can't fix a missing GPU sensor
        home.t_fans.action.click()
        sim.engine.sensor_helper._thread.join(5)
        assert with_helper.calls and sim.engine.base_cfg.sensor_helper
        assert "Windows will ask" in ctrl.window.toast_label.history[-1]
        home = show(ctrl, sim, qtbot)
        assert not home.t_fans.action.isVisibleTo(home) and "starting" in home.t_fans.sub_label.text()

    def test_poke_bubble_mentions_heat(self, ctrl, sim, qtbot):
        home = show(ctrl, sim, qtbot, **FULL)
        home._poke()
        assert "CPU 61 °C · GPU 52 °C · fans 2,600 RPM" in home.bubble.text()


class TestTimelineHeat:
    def test_new_metric_chips(self, ctrl):
        page = ctrl.window.pages["timeline"]
        keys = [b.property("metric") for b in page.metric_group.buttons()]
        assert {"gpu", "gpu_temp", "fan", "temp"} <= set(keys)
        assert {b.text() for b in page.metric_group.buttons()} >= {"GPU", "GPU temp", "CPU temp", "Fans"}

    @pytest.mark.parametrize("metric, unit", [("gpu", "%"), ("gpu_temp", "°C"), ("fan", "RPM"), ("temp", "°C")])
    def test_charts_each_new_metric(self, ctrl, sim, qtbot, metric, unit):
        show(ctrl, sim, qtbot, **FULL)
        pump(qtbot, sim, 60)
        page = ctrl.window.pages["timeline"]
        ctrl.window.navigate("timeline", animate=False)
        page.set_metric(metric)
        assert page.last_data["rows"] and unit in page.summary.text() and not page.unavailable.isVisibleTo(page)
        assert any(b.isChecked() and b.property("metric") == metric for b in page.metric_group.buttons())
        page._nerd(sim.engine.latest)
        assert page.nerd_label.text().endswith(unit)
        if metric == "fan":
            assert page.nerd_label.text() == "Now: 2,600 RPM"

    def test_no_readings_says_so(self, ctrl, sim, qtbot):
        page = ctrl.window.pages["timeline"]
        ctrl.window.navigate("timeline", animate=False)
        page.set_metric("cpu")
        page.set_metric("fan")
        assert page.summary.text() == "No fastest fan readings in this range yet."
        page.set_metric("temp")
        assert page.summary.text() == "No CPU temperature readings in this range yet."
        assert "CPU temperature isn't available" in page.unavailable.text()
        assert not page.fix_button.isVisibleTo(page)

    def test_turn_on_from_the_timeline(self, with_helper, ctrl, sim, qtbot):
        show(ctrl, sim, qtbot)
        page = ctrl.window.pages["timeline"]
        ctrl.window.navigate("timeline", animate=False)
        page.set_metric("gpu_temp")
        assert not page.fix_button.isVisibleTo(page)
        page.set_metric("fan")
        assert page.fix_button.isVisibleTo(page)
        page.fix_button.click()
        sim.engine.sensor_helper._thread.join(5)
        assert with_helper.calls and not page.fix_button.isVisibleTo(page)


class TestSettingsSensors:
    def test_disabled_without_the_helper(self, ctrl):
        page = ctrl.window.pages["settings"]
        ctrl.window.navigate("settings", animate=False)
        assert not page.sensor_box.isEnabled()
        assert "only available on Windows" in page.sensor_text.text()

    def test_toggle_starts_and_stops_the_helper(self, with_helper, ctrl, sim, qtbot):
        show(ctrl, sim, qtbot, gpu_busy_pct=5.0, gpu_temp_c=48.0)
        page = ctrl.window.pages["settings"]
        ctrl.window.navigate("settings", animate=False)
        assert page.sensor_box.isEnabled() and not page.sensor_box.isChecked()
        text = page.sensor_text.text()
        assert "GPU load: from simulator" in text and "CPU temperature: needs admin" in text
        page.sensor_box.setChecked(True)
        sim.engine.sensor_helper._thread.join(5)
        page._sensor_status()
        assert with_helper.calls and sim.engine.base_cfg.sensor_helper
        assert "admin permission" in page.sensor_text.text()
        page.sensor_box.setChecked(False)
        assert sim.engine.sensor_helper.state == "off" and not sim.engine.base_cfg.sensor_helper
