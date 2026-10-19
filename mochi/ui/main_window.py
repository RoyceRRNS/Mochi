"""The dashboard window: sidebar navigation, header, banners and the six screens."""

from __future__ import annotations

import time

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from ..alerts import AlertEvent
from ..engine import Engine, SampleView
from ..moods import LABELS, Mood
from .bridge import EngineBridge
from .mascot import mochi_pixmap
from .screens.checkup import CheckupPage
from .screens.common import UiContext
from .screens.detective import DetectivePage
from .screens.diary import DiaryPage
from .screens.home import HomePage
from .screens.settings import SettingsPage
from .screens.timeline import TimelinePage
from .theme import THEME
from .tray import tray_icon
from .widgets import Banner, button, hbox, icon, label

NAV = [("home", "Home", "home"), ("timeline", "Timeline", "timeline"), ("detective", "Detective", "detective"),
       ("checkup", "Check-up", "checkup"), ("diary", "Diary", "diary"), ("settings", "Settings", "settings")]
PAGE_CLASSES = {"home": HomePage, "timeline": TimelinePage, "detective": DetectivePage, "checkup": CheckupPage,
                "diary": DiaryPage, "settings": SettingsPage}


class Toast(QLabel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.history: list[str] = []

    def show_text(self, text: str, ms: int = 3500) -> None:
        p = THEME.palette
        self.history.append(text)
        self.setStyleSheet(f"background:{p.text}; color:{p.background}; border-radius: 16px; padding: 10px 18px;"
                           f" font-weight: 700; font-size: 14px;")
        self.setText(text)
        self.setMaximumWidth(560)
        self.adjustSize()
        par = self.parentWidget()
        self.move((par.width() - self.width()) // 2, par.height() - self.height() - 26)
        self.show()
        self.raise_()
        self._timer.start(ms)


class MainWindow(QMainWindow):
    def __init__(self, engine: Engine, bridge: EngineBridge, controller=None) -> None:
        super().__init__()
        self.engine = engine
        self.bridge = bridge
        self.controller = controller
        self.quitting = False
        self.hint_shown = False
        self.setWindowTitle("Mochi")
        self.setWindowIcon(tray_icon(Mood.HAPPY))
        self.resize(1260, 830)
        self.setMinimumSize(980, 640)
        self.nerd_on = engine.base_cfg.nerd_stats
        self.pages: dict[str, object] = {}
        # Late-bound so pages always use the window's current handlers.
        self.ctx = UiContext(engine, bridge, lambda k: self.navigate(k), lambda t: self.toast(t),
                             lambda *a: self.confirm(*a), lambda i, k: self.respond(i, k), lambda a, i: self.act(a, i),
                             lambda: self.run_checkup(), lambda: self.run_benchmark(), lambda: self.nerd_on, self)
        self._build()
        self._wire()

    # ----------------------------------------------------------------- build
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("Page")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(232)
        sb = QVBoxLayout(self.sidebar)
        sb.setContentsMargins(16, 22, 16, 18)
        sb.setSpacing(6)
        self.mini = QLabel()
        self.mini.setFixedSize(56, 56)
        brand = label("Mochi", "H1")
        sb.addLayout(hbox(self.mini, vbox_(brand, label("PC health buddy", muted=True)), spacing=10))
        sb.addSpacing(18)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for i, (key, text, ic) in enumerate(NAV):
            b = QPushButton(f"  {text}")
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.TabFocus)  # focus ring only for keyboard users
            b.setIcon(icon(ic))
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"{text} (Ctrl+{i + 1})")
            b.setAccessibleName(text)
            b.clicked.connect(lambda _=False, k=key: self.navigate(k))
            self.nav_group.addButton(b)
            self.nav_buttons[key] = b
            sb.addWidget(b)
        sb.addStretch(1)
        self.side_status = label("", muted=True, wrap=True)
        sb.addWidget(self.side_status)
        root.addWidget(self.sidebar)

        content = QVBoxLayout()
        content.setContentsMargins(0, 18, 0, 0)
        content.setSpacing(10)
        header = QHBoxLayout()
        header.setContentsMargins(28, 0, 28, 0)
        self.h_title = label("Home", "H1")
        self.h_sub = label("", muted=True)
        header.addLayout(vbox_(self.h_title, self.h_sub))
        header.addStretch(1)
        self.nerd = QCheckBox("Nerd stats")
        self.nerd.setChecked(self.nerd_on)
        self.nerd.setToolTip("Show z-scores, baselines and raw counters")
        self.nerd.toggled.connect(self.set_nerd)
        header.addWidget(self.nerd)
        self.bell = button("", "", "Inbox (Ctrl+I)", lambda: self.navigate("inbox"), "bell")
        self.bell.setFixedHeight(38)
        header.addWidget(self.bell)
        content.addLayout(header)
        self.banners = QVBoxLayout()
        self.banners.setContentsMargins(28, 0, 28, 0)
        self.demo_banner = Banner("Demo mode is on: I alert after much shorter waits than usual. "
                                  "Turn it off in Settings after your presentation.", "warning")
        self.stale_banner = Banner("", "critical")
        self.snooze_banner = Banner("", "info")
        for b in (self.demo_banner, self.stale_banner, self.snooze_banner):
            b.hide()
            self.banners.addWidget(b)
        content.addLayout(self.banners)
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)
        root.addLayout(content, 1)
        self.setCentralWidget(central)
        central.setFocusPolicy(Qt.ClickFocus)
        self.toast_label = Toast(central)
        self._make_pages()
        for i, (key, _t, _i) in enumerate(NAV):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda k=key: self.navigate(k))
        QShortcut(QKeySequence("Ctrl+I"), self, activated=lambda: self.navigate("inbox"))
        self.current = "home"
        self.navigate("home", animate=False)
        self.update_mood(Mood.HAPPY)

    def _make_pages(self) -> None:
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()
        self.pages = {}
        for key, cls in PAGE_CLASSES.items():
            page = cls(self.ctx)
            self.pages[key] = page
            self.stack.addWidget(page)

    def rebuild(self) -> None:
        """Re-create pages after a theme change so every inline colour updates."""
        current = getattr(self, "current", "home")
        self._make_pages()
        for key, (_k, _t, ic) in zip(self.nav_buttons, NAV):
            self.nav_buttons[key].setIcon(icon(ic))
        self.bell.setIcon(icon("bell"))
        for b in (self.demo_banner, self.stale_banner, self.snooze_banner):
            b.restyle()
        self.navigate(current, animate=False)
        if self.engine.latest:
            self.on_sample(self.engine.latest)

    def _wire(self) -> None:
        br = self.bridge
        br.sample.connect(self.on_sample)
        br.alert.connect(self.on_alert)
        br.status.connect(self.on_status)
        br.notice.connect(lambda _n: self._refresh_inbox())
        br.feed.connect(lambda fe: self.toast(fe.text))

    # ----------------------------------------------------------- navigation
    def navigate(self, key: str, animate: bool = True) -> None:
        scroll_inbox = key == "inbox"
        if scroll_inbox:
            key = "home"
            self.engine.alerts.clear_badge()
        page = self.pages[key]
        self.current = key
        self.nav_buttons[key].setChecked(True)
        self.h_title.setText(page.title)
        self.h_sub.setText(page.subtitle)
        self.stack.setCurrentWidget(page)
        page.on_show()
        if self.engine.latest is not None:
            page.on_view(self.engine.latest)
        if scroll_inbox:
            QTimer.singleShot(0, lambda: page.scroll.ensureWidgetVisible(page.inbox_card))
        if animate and not THEME.reduce_motion:
            eff = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(200)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(lambda: page.setGraphicsEffect(None))
            anim.start()
            self._anim = anim

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.centralWidget().setFocus(Qt.OtherFocusReason)

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # --------------------------------------------------------------- events
    def on_sample(self, v: SampleView) -> None:
        self.update_mood(v.mood)
        home = self.pages.get("home")
        if home is not None:
            home.on_view(v)
        cur = self.pages.get(self.current)
        if cur is not None and cur is not home and self.isVisible():
            cur.on_view(v)
        self.demo_banner.setVisible(v.demo)
        if v.snoozed_until and v.snoozed_until > v.ts:
            self.snooze_banner.text.setText(
                f"Alerts are snoozed until {time.strftime('%a %H:%M', time.localtime(v.snoozed_until))}. "
                "Uh-oh alerts still get through.")
            self.snooze_banner.show()
        else:
            self.snooze_banner.hide()
        badge = v.badge
        self.bell.setText(f" {badge}" if badge else "")
        self.bell.setAccessibleName(f"Inbox, {badge} new" if badge else "Inbox")
        label_, desc = LABELS[v.mood]
        learn = f"Learning: {int(v.progress * 100)}%" + (" (day %d of 3)" % min(v.learning_day, 3) if v.learning else "")
        extra = f"\nMochi uses {v.self_cpu:.1f}% CPU, {v.self_mem_mb:.0f} MB" if self.nerd_on else ""
        self.side_status.setText(f"{label_}: {desc}\n{learn}{extra}")

    def update_mood(self, mood: Mood) -> None:
        self.mini.setPixmap(mochi_pixmap(mood, 56, face_only=False, dpr=self.devicePixelRatioF() or 1.0))
        self.mini.setAccessibleName(f"Mochi is {LABELS[mood][0].lower()}")

    def on_alert(self, ev: AlertEvent) -> None:
        if self.controller is not None:
            self.controller.alert_event(ev)
        self._refresh_inbox()

    def _refresh_inbox(self) -> None:
        home = self.pages.get("home")
        if home is not None:
            QTimer.singleShot(0, home.refresh_inbox)

    def on_status(self, st: dict) -> None:
        if st.get("stale") and st.get("last_sample_age") is not None:
            self.stale_banner.text.setText(f"I haven't had a reading for {st['last_sample_age']:.0f} s. "
                                           "I'm restarting my sensors; nothing is lost.")
            self.stale_banner.show()
        else:
            self.stale_banner.hide()

    def set_nerd(self, on: bool) -> None:
        self.nerd_on = on
        self.engine.update_config(nerd_stats=on)
        for p in self.pages.values():
            p.on_nerd(on)

    def config_changed(self, changes: dict) -> None:
        if self.controller is not None and ({"theme", "reduce_motion"} & set(changes)):
            self.controller.apply_theme()

    # -------------------------------------------------------------- actions
    def toast(self, text: str) -> None:
        self.toast_label.show_text(text)

    def confirm(self, title: str, text: str, ok_label: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(f"<b>{title}</b>")
        box.setInformativeText(text)
        ok = box.addButton(ok_label, QMessageBox.AcceptRole)
        ok.setObjectName("Danger" if "Delete" in ok_label or "End" in ok_label else "Primary")
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is ok

    def respond(self, episode_id: int, kind: str) -> None:
        if kind == "dismiss_silent":
            self.engine.respond(episode_id, "dismiss")
            return
        msg = self.engine.respond(episode_id, kind)
        self.toast(msg)
        self._refresh_inbox()

    def act(self, action, episode_id: int | None) -> None:
        if action.kind == "end_process":
            t = action.target
            text = self.engine.actions.confirm_text(t.get("display") or t.get("app", "?"), t.get("pid") or 0)
            if not self.confirm(f"End {t.get('display') or t.get('app')}?", text.split("\n\n", 1)[-1], "End it"):
                return
        if action.kind in ("biggest_folders",):
            self.navigate("checkup")
            self.pages["checkup"].start_scan(action.target.get("mount"))
            return

        def done(res):
            if isinstance(res, BaseException):
                self.toast(f"That didn't work: {res}")
                return
            if res.message:
                self.toast(res.message)
            if self.current == "detective":
                QTimer.singleShot(1500, self.pages["detective"].refresh)
            self._refresh_inbox()

        self.bridge.run_async(lambda: self.engine.act(action, episode_id), done, done)

    def run_checkup(self) -> None:
        self.show_and_raise()
        self.navigate("checkup")
        self.pages["checkup"].start()

    def run_benchmark(self) -> None:
        self.navigate("checkup")
        self.pages["checkup"].speed_only()

    def closeEvent(self, e) -> None:
        if self.quitting:
            e.accept()
            return
        e.ignore()  # LIF-3: closing the window keeps Mochi in the tray
        self.hide()
        if not self.hint_shown and self.controller is not None:
            self.hint_shown = True
            self.controller.tray.hint("Mochi is still here", "I'm keeping an eye on things from the tray. "
                                      "Right-click my face to quit.")


def vbox_(*widgets, spacing: int = 2) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        lay.addWidget(w)
    return lay
