"""Full user journeys through the real UI (popups, tray, onboarding, single instance, quit)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from mochi.model import Level
from mochi.moods import Mood
from mochi.platform.fake import FakeProc
from mochi.ui.onboarding import PAGES, Onboarding

from .conftest import pump

pytestmark = pytest.mark.ui


def test_memory_hog_journey(ctrl, sim, qtbot):
    """Alert popup -> "I meant to do that" -> toast; close the hog -> fed -> proud in the tray."""
    ctrl.window.show()
    sim.platform.add(FakeProc("python.exe", 7000, 1000, cpu_core=30.0, private_mb=300, create_time=sim.clock.ts,
                              cmdline=["python", "memory_hog.py"]))

    def grow(_i):
        if 7000 in sim.platform.procs:
            sim.platform.update(7000, private_mb=min(9600, sim.platform.procs[7000].private_mb + 400))

    pump(qtbot, sim, 240, grow)
    popups = [p for p in ctrl.popups.popups if getattr(p, "episode_id", None)]
    assert popups, "a Heads-up popup should be on screen"
    pop = popups[0]
    assert pop.title_label.text() in ("Heads up!", "Uh-oh.") and "memory_hog.py" in pop.cause_label.text() + pop.tip_label.text()
    intent = next(b for b in pop.buttons if b.text() == "I meant to do that")
    qtbot.mouseClick(intent, Qt.LeftButton)
    assert any(s.app == "python.exe:memory_hog.py" for s in sim.engine.suppressions())
    assert "meant to be busy" in ctrl.window.toast_label.history[-1]
    # close the hog after the tip: Mochi is fed and turns proud
    sim.platform.remove(7000)
    pump(qtbot, sim, 60)
    assert any(p.title_label.text() == "Yum!" for p in ctrl.popups.popups) or \
        any("Yum!" in t for t in ctrl.window.toast_label.history)
    qtbot.waitUntil(lambda: (pump(qtbot, sim, 5) or True) and ctrl.tray.mood == Mood.PROUD, timeout=10000)
    assert "Proud" in ctrl.tray.tray.toolTip()


def test_popup_primary_button_opens_the_right_page(ctrl, sim, qtbot):
    ctrl.window.show()
    sim.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=600.0, private_mb=300, create_time=sim.clock.ts))
    pump(qtbot, sim, 200)
    pops = [p for p in ctrl.popups.popups if getattr(p, "episode_id", None)]
    assert pops
    primary = pops[0].buttons[0]
    qtbot.mouseClick(primary, Qt.LeftButton)
    qtbot.wait(50)
    assert ctrl.window.current in ("timeline", "detective", "home") or ctrl.window.toast_label.history


def test_shh_summary_popup(ctrl, sim, qtbot):
    sim.platform.set(fullscreen=True)
    sim.platform.add(FakeProc("encoder.exe", 6100, 1000, cpu_core=600.0, private_mb=300, create_time=sim.clock.ts))
    pump(qtbot, sim, 200)
    assert not [p for p in ctrl.popups.popups if getattr(p, "episode_id", None) and p.level < Level.UH_OH]
    sim.platform.set(fullscreen=False)
    pump(qtbot, sim, 10)
    assert any(p.title_label.text() == "While you were busy" for p in ctrl.popups.popups)


def test_tray_snooze_and_quit(ctrl, sim, qtbot, monkeypatch):
    ctrl.tray.act_snooze_hour.trigger()
    assert sim.engine.quiet.snoozed_until > sim.clock.ts and "Snoozed until" in ctrl.window.toast_label.history[-1]
    ctrl.tray.act_resume.trigger()
    assert sim.engine.quiet.snoozed_until == 0.0
    quits = []
    monkeypatch.setattr(ctrl.qapp, "quit", lambda: quits.append(1))
    ctrl.tray.act_quit.trigger()
    assert quits and ctrl.window.quitting
    ctrl._shutdown()
    assert sim.engine.storage._wconn is None  # saved and closed (LIF-3)
    ctrl._shutdown()  # idempotent


def test_tray_check_up_opens_the_page(ctrl, monkeypatch):
    started = []
    monkeypatch.setattr(ctrl.window.pages["checkup"], "start", lambda: started.append(1))
    ctrl.tray.act_check.trigger()
    assert ctrl.window.current == "checkup" and started


def test_onboarding_steps(qtbot):
    dlg = Onboarding(autostart_default=True)
    qtbot.addWidget(dlg)
    assert dlg.stack.currentIndex() == 0 and not dlg.back.isEnabled() and not dlg.autostart.isVisibleTo(dlg)
    for i in range(1, len(PAGES)):
        qtbot.mouseClick(dlg.next, Qt.LeftButton)
        assert dlg.stack.currentIndex() == i
    assert dlg.next.text() == "Let's go!" and dlg.autostart.isVisibleTo(dlg) and dlg.autostart.isChecked()
    assert "Step 4 of 4" in dlg.dots.text()
    qtbot.mouseClick(dlg.back, Qt.LeftButton)
    assert dlg.stack.currentIndex() == len(PAGES) - 2
    dlg.go(1)
    with qtbot.waitSignal(dlg.accepted, timeout=1000):
        dlg.go(1)
    texts = " ".join(t for _m, _t, t in PAGES)
    assert "never connect to the internet" in texts and "Other system tray icons" in texts and "admin" in texts


def test_onboarding_saves_choice(ctrl, sim, monkeypatch):
    from mochi.autostart import Autostart, MemoryRegistry

    sim.engine.autostart = Autostart(MemoryRegistry())
    monkeypatch.setattr(Onboarding, "exec", lambda self: True)
    ctrl._onboard()
    assert sim.engine.base_cfg.onboarded and sim.engine.base_cfg.autostart and sim.engine.autostart.is_enabled()


def second_launch(message: bytes = b"show"):
    """A real second process asking the running Mochi for something (Qt's pipe I/O needs its own process)."""
    import subprocess
    import sys

    code = f"from mochi.ui.app import signal_running_instance as s; print(s('uitest', {message!r}))"
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            cwd=str(Path(__file__).resolve().parents[2]))


def test_single_instance_handshake(ctrl, qtbot):
    from mochi.ui.app import signal_running_instance

    ctrl.window.hide()
    p = second_launch()
    qtbot.waitUntil(lambda: ctrl.window.isVisible() and p.poll() is not None, timeout=10000)
    assert p.stdout.read().strip() == "True"
    assert not signal_running_instance("nobody-listens", timeout_ms=200)


def test_old_style_client_still_shows_the_window(ctrl, qtbot):
    from PySide6.QtNetwork import QLocalSocket

    from mochi.ui.app import server_name

    ctrl.window.hide()
    sock = QLocalSocket()
    sock.connectToServer(server_name("uitest"))
    assert sock.waitForConnected(1000)
    sock.disconnectFromServer()                       # hangs up without a message
    qtbot.waitUntil(lambda: ctrl.window.isVisible(), timeout=3000)


def test_quit_command_saves_and_quits(ctrl, qtbot, monkeypatch):
    from mochi.__main__ import main

    quits = []
    monkeypatch.setattr(ctrl, "quit", lambda: quits.append(1))
    ctrl.window.hide()
    p = second_launch(b"quit")
    qtbot.waitUntil(lambda: quits == [1] and p.poll() is not None, timeout=10000)
    assert p.stdout.read().strip() == "True"                 # the running copy acknowledged
    assert not ctrl.window.isVisible()                       # quitting doesn't pop the window up first
    assert main(["--quit", "--profile", "nobody-runs-this"]) == 1


def test_sim_driver_runs_the_demo_script(qapp, qtbot):
    from mochi.ui.app import SimDriver

    d = SimDriver(Path(tempfile.mkdtemp()), interval_ms=10, warm_days=1)
    try:
        before = d.engine.latest.ts
        d.start()
        qtbot.waitUntil(lambda: d.t >= 400, timeout=20000)
        d.stop()
        assert d.engine.latest.ts > before
        assert any(e.kind in ("feed",) for e in d.sim.events) or d.engine.alerts.all_recent()
    finally:
        d.engine.stop()
