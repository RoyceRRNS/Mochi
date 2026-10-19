"""Application controller: wires the engine, window, tray and popups together."""

from __future__ import annotations

import getpass
import logging
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from ..alerts import AlertEvent
from ..config import Config
from ..engine import Engine
from ..moods import Mood
from ..paths import Paths
from .bridge import EngineBridge
from .main_window import MainWindow
from .onboarding import Onboarding
from .popup import PopupManager
from .theme import THEME
from .tray import TrayController

log = logging.getLogger(__name__)


def server_name(profile: str) -> str:
    user = "".join(c for c in getpass.getuser() if c.isalnum()) or "user"
    return f"mochi-{user}-{profile}"


def signal_running_instance(profile: str, message: bytes = b"show", timeout_ms: int = 1500) -> bool:
    """Ask a running Mochi to show its window (LIF-2), or with ``b"quit"`` to save and quit.

    Waits for the running copy's "ok" before hanging up: bytes written to a pipe the server
    hasn't accepted yet can be dropped when the client disconnects.
    """
    from PySide6.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    sock = QLocalSocket()
    sock.connectToServer(server_name(profile))
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(message + b"\n")
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    acked = sock.waitForReadyRead(timeout_ms) and bytes(sock.readAll()).strip() == b"ok"
    sock.disconnectFromServer()
    return acked or message == b"show"   # an older Mochi shows its window without replying


class MochiApp(QObject):
    def __init__(self, qapp: QApplication, engine: Engine, profile: str, start_hidden: bool = False,
                 show_onboarding: bool = True, sim_driver=None) -> None:
        super().__init__()
        self.qapp = qapp
        self.engine = engine
        self.profile = profile
        self.sim = sim_driver
        self.bridge = EngineBridge(engine)
        self.apply_theme(rebuild=False)
        self.window = MainWindow(engine, self.bridge, self)
        self.popups = PopupManager(engine, self._open_page, self.window.respond, self.window.act)
        self.popups.reduce_motion = THEME.reduce_motion
        self.tray = TrayController(self.window.show_and_raise, self.window.run_checkup, self._snooze, self.quit)
        self.bridge.sample.connect(self._on_sample)
        self.bridge.notice.connect(self.popups.show_notice)
        self.bridge.feed.connect(self.popups.show_feed)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(server_name(profile))
        self.server.listen(server_name(profile))
        self.server.newConnection.connect(self._on_connection)
        hints = qapp.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self.apply_theme())
        qapp.aboutToQuit.connect(self._shutdown)
        if hasattr(qapp, "commitDataRequest"):
            qapp.commitDataRequest.connect(lambda _m: self._shutdown())  # Windows sign-out/shutdown (LIF-3)
        if show_onboarding and not engine.base_cfg.onboarded:
            QTimer.singleShot(300, self._onboard)
        if not start_hidden or not self.tray.available:
            self.window.show()

    # ---------------------------------------------------------------- theme
    def apply_theme(self, rebuild: bool = True) -> None:
        cfg = self.engine.base_cfg
        hints = self.qapp.styleHints()
        system_dark = hasattr(hints, "colorScheme") and hints.colorScheme() == Qt.ColorScheme.Dark
        THEME.high_contrast = self.engine.platform.high_contrast()
        sys_rm = self.engine.platform.reduce_motion()
        THEME.reduce_motion = cfg.reduce_motion == "on" or (cfg.reduce_motion == "system" and sys_rm)
        THEME.set_mode(cfg.theme, system_dark)
        self.qapp.setStyleSheet(THEME.qss())
        if rebuild and hasattr(self, "window"):
            self.window.rebuild()
            self.popups.reduce_motion = THEME.reduce_motion
            for page in self.window.pages.values():
                if hasattr(page, "mochi"):
                    page.mochi.set_reduce_motion(THEME.reduce_motion)
        if hasattr(self, "window"):
            home = self.window.pages.get("home")
            if home is not None:
                home.mochi.set_reduce_motion(THEME.reduce_motion)

    # ---------------------------------------------------------------- events
    def _on_sample(self, v) -> None:
        self.tray.update(v.mood, v.badge, v.snoozed_until, now=v.ts)

    def alert_event(self, ev: AlertEvent) -> None:
        if ev.kind == "summary" and ev.held:
            self.popups.show_summary(ev.held)
        elif ev.kind in ("opened", "escalated") and ev.notify == "popup" and ev.episode is not None:
            self.popups.show_episode(ev.episode)

    def _open_page(self, key: str) -> None:
        self.window.show_and_raise()
        self.window.navigate(key)

    def _snooze(self, secs) -> None:
        until = self.engine.snooze(secs)
        self.tray.update(self.tray.mood, self.engine.alerts.badge_count, until, now=self.engine.clock.time())
        msg = "Alerts are back on." if not until else f"Snoozed until {time.strftime('%a %H:%M', time.localtime(until))}."
        self.window.toast(msg)

    def _on_connection(self) -> None:
        while (sock := self.server.nextPendingConnection()) is not None:
            state = {"done": False}
            sock.readyRead.connect(lambda s=sock, st=state: self._on_message(s, st))
            # A client that hangs up without a readable message just wants the window.
            sock.disconnected.connect(lambda st=state: None if st["done"] else self._handle(b"show", st))
            sock.disconnected.connect(sock.deleteLater)
            if sock.bytesAvailable():
                self._on_message(sock, state)

    def _on_message(self, sock: QLocalSocket, state: dict) -> None:
        message = bytes(sock.readAll()).strip()
        if not message or state["done"]:
            return
        sock.write(b"ok\n")
        sock.flush()
        self._handle(message, state)

    def _handle(self, message: bytes, state: dict) -> None:
        state["done"] = True
        if message == b"quit":
            QTimer.singleShot(0, self.quit)
        else:
            self.window.show_and_raise()

    def _onboard(self) -> None:
        dlg = Onboarding(self.window, autostart_default=True)
        if dlg.exec():
            self.engine.update_config(onboarded=True, autostart=bool(dlg.autostart.isChecked()))
        else:
            self.engine.update_config(onboarded=True)

    def quit(self) -> None:
        self.window.quitting = True
        self.qapp.quit()

    def _shutdown(self) -> None:
        if getattr(self, "_down", False):
            return
        self._down = True
        self.popups.close_all()
        if self.sim is not None:
            self.sim.stop()
        try:
            self.engine.stop(timeout=2.0)
        except Exception:
            log.exception("engine stop failed")
        self.tray.hide()


class SimDriver(QObject):
    """Feeds the real engine from the scripted platform, 5x faster than real time.

    Used by ``python -m mochi --simulate`` (UI demos and screenshots) and by UI tests.
    The script cycles: normal use -> a memory hog ramps up (Puffy, then an alert) ->
    the hog is closed (Mochi gets fed and turns proud) -> calm again.
    """

    def __init__(self, root: Path, interval_ms: int = 1000, cfg: Config | None = None, warm_days: int = 4) -> None:
        super().__init__()
        from ..platform.fake import FakeProc
        from ..replay import Simulation, add_browser, normal_use

        self.FakeProc = FakeProc
        now = datetime.now().replace(second=0, microsecond=0)
        start = (now - timedelta(days=warm_days)).replace(hour=8, minute=0)
        cfg = cfg or Config(onboarded=True, demo_mode=True)
        self.sim = Simulation(root, cfg=cfg, start=start.timestamp(), ncpu=8, ram_gb=16.0)
        self.sim.platform.noise = 0.08
        sim = self.sim
        for d in range(warm_days, 0, -1):
            day = (now - timedelta(days=d)).replace(hour=9, minute=0)
            sim.jump_to(day.timestamp())
            normal_use(sim)
            sim.run(3 * 3600)
            sim.engine.storage.maintain(sim.clock.ts)
        sim.jump_to((now - timedelta(minutes=10)).timestamp())
        normal_use(sim)
        sim.run(600)
        self.engine = sim.engine
        self.t = 0
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.step)

    def start(self) -> None:
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()

    def step(self) -> None:
        sim = self.sim
        p = sim.platform
        phase = self.t % 600
        if phase == 60:
            p.add(self.FakeProc("python.exe", 7000, 1000, cpu_core=25.0, private_mb=300,
                                cmdline=["python", "memory_hog.py"]))
        if 60 < phase < 300 and 7000 in p.procs:
            p.update(7000, private_mb=min(7800, p.procs[7000].private_mb + 160))
        if phase == 330 and 7000 in p.procs:
            p.remove(7000)
        self.t += 5
        try:
            sim.step(5)
        except Exception:
            log.exception("simulation step failed")


def run_app(paths: Paths, profile: str, start_hidden: bool = False, simulate: bool = False) -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication.instance() or QApplication(sys.argv)
    qapp.setApplicationName("Mochi")
    qapp.setOrganizationName("Mochi")
    qapp.setQuitOnLastWindowClosed(False)
    THEME.load_fonts()
    qapp.setFont(THEME.font(14, 500))
    sim = None
    if simulate:
        sim = SimDriver(Path(tempfile.mkdtemp(prefix="mochi_sim_")))
        engine = sim.engine
    else:
        from ..platform import get_platform

        engine = Engine(get_platform(), paths, profile=profile)
    start_hidden = start_hidden or (engine.base_cfg.start_hidden and not simulate)
    ctrl = MochiApp(qapp, engine, profile, start_hidden=start_hidden, sim_driver=sim)
    if sim is not None:
        sim.start()
        if engine.latest is not None:
            ctrl.bridge.sample.emit(engine.latest)
    qapp._mochi = ctrl  # keep a reference
    return qapp.exec()
