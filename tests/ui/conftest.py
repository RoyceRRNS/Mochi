"""UI fixtures: offscreen Qt, the scripted engine, and the full app controller."""

from __future__ import annotations

import pytest

from mochi.config import Config
from mochi.replay import Simulation, normal_use


@pytest.fixture(scope="session", autouse=True)
def _fonts(qapp):
    from mochi.ui.theme import THEME

    THEME.load_fonts()
    qapp.setFont(THEME.font(14, 500))
    yield


@pytest.fixture
def sim(tmp_path):
    s = Simulation(tmp_path, cfg=Config(onboarded=True, cold_start_days=0.0, min_days=1, min_samples=20,
                                        heads_up_s=60.0, psst_s=20.0, close_after_s=30.0, cooldown_s=60.0,
                                        commit_delay_s=60.0))
    s.platform.noise = 0.05
    normal_use(s)
    s.run(900)
    yield s
    try:
        s.engine.stop()
    except Exception:
        pass


@pytest.fixture
def ctrl(qapp, qtbot, sim, monkeypatch):
    from mochi.ui.app import MochiApp

    c = MochiApp(qapp, sim.engine, "uitest", start_hidden=False, show_onboarding=False)
    qapp.aboutToQuit.disconnect(c._shutdown)
    monkeypatch.setattr(c.window, "confirm", lambda *a, **k: True)
    c.window.resize(1300, 860)
    qtbot.addWidget(c.window)
    c.bridge.sample.emit(sim.engine.latest)
    yield c
    c.popups.close_all()
    c.window.quitting = True
    c.window.close()
    c.tray.hide()
    c.server.close()


def pump(qtbot, sim, seconds, step_cb=None):
    """Advance the simulation and let Qt process the resulting events."""
    for i in range(int(seconds / sim.interval)):
        if step_cb:
            step_cb(i)
        sim.step()
        qtbot.wait(1)
