"""Notification popups (NOT-1..3, ALR-7, ALR-8).

Every alert has the same parts: Mochi's face in the matching mood, a short title by
level, one line on the cause, one tip, and two pill buttons - a primary action and
"I meant to do that". Popups never take keyboard focus. Heads-up popups fade after a few
seconds (paused while the mouse is over them); Uh-oh stays until dismissed.

plyer and QSystemTrayIcon.showMessage can't show buttons or custom images on Windows,
so these are Mochi's own frameless, non-activating windows near the tray.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..alerts import Episode
from ..model import Level
from ..moods import Mood
from .mascot import mochi_pixmap
from .theme import THEME
from .widgets import Card, button, label, level_stroke

RULE_MOOD = {
    "memory_pressure": Mood.DIZZY, "memory_hog": Mood.PUFFY, "leak": Mood.PUFFY, "runaway": Mood.SWEATY,
    "throttling": Mood.SWEATY, "disk_io": Mood.DIZZY, "storage": Mood.GRUMPY, "idle_cpu": Mood.SWEATY,
    "battery": Mood.SWEATY, "unknown": Mood.PUFFY,
}


class Popup(QWidget):
    closed = Signal(object)

    def __init__(self, title: str, cause: str, tip: str, mood: Mood, level: int, persistent: bool,
                 primary: tuple[str, Callable[[], None]] | None, secondary: tuple[str, Callable[[], None]] | None,
                 link: tuple[str, Callable[[], None]] | None = None, timeout_s: float = 12.0,
                 reduce_motion: bool = False) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.NoFocus)
        self.persistent = persistent
        self.level = level
        self.reduce_motion = reduce_motion
        self.setFixedWidth(420)
        self.setAccessibleName(f"{title} {cause}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 18)
        card = Card(padding=16, spacing=8)
        card.setStyleSheet(f"#Card {{ border-left: 6px solid {level_stroke(level)}; }}")
        outer.addWidget(card)
        top = QHBoxLayout()
        face = QLabel()
        face.setPixmap(mochi_pixmap(mood, 56, dpr=self.devicePixelRatioF() or 1.0))
        face.setFixedSize(60, 60)
        face.setAccessibleName(f"Mochi looks {mood.value}")
        top.addWidget(face)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_label = label(title, "H2")
        titles.addWidget(self.title_label)
        self.cause_label = label(cause, wrap=True)
        self.cause_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        titles.addWidget(self.cause_label)
        top.addLayout(titles, 1)
        close = button("", "Ghost", "Dismiss", self.dismiss, "close")
        close.setFixedSize(30, 30)
        close.setFocusPolicy(Qt.NoFocus)
        top.addWidget(close, 0, Qt.AlignTop)
        card.add(top)
        self.tip_label = label(tip, wrap=True, muted=True)
        self.tip_label.setStyleSheet("font-size: 13px;")
        card.add(self.tip_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.buttons = []
        for spec, kind in ((primary, "Primary"), (secondary, "")):
            if spec:
                b = button(spec[0], kind, on_click=self._wrap(spec[1]))
                b.setFocusPolicy(Qt.NoFocus)
                row.addWidget(b)
                self.buttons.append(b)
        row.addStretch(1)
        card.add(row)
        if link:
            lb = button(link[0], "Ghost", on_click=self._wrap(link[1]))
            lb.setFocusPolicy(Qt.NoFocus)
            link_row = QHBoxLayout()
            link_row.setContentsMargins(0, 0, 0, 0)
            link_row.addWidget(lb)
            link_row.addStretch(1)
            card.add(link_row)
            self.buttons.append(lb)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._timeout_ms = int(timeout_s * 1000)
        if not persistent:
            self._timer.start(self._timeout_ms)

    def _wrap(self, fn: Callable[[], None]) -> Callable[[], None]:
        def run():
            fn()
            self.dismiss()
        return run

    def enterEvent(self, e) -> None:
        self._hovered = True
        self._timer.stop()  # pause the fade while being read
        if e is not None:
            super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        if getattr(self, "_hovered", False) and not self.persistent:
            self._timer.start(max(3000, self._timeout_ms // 2))  # a few more seconds after reading
        self._hovered = False
        if e is not None:
            super().leaveEvent(e)

    def dismiss(self) -> None:
        self._timer.stop()
        self.closed.emit(self)
        self.close()

    def fade_in(self) -> None:
        if self.reduce_motion:
            self.show()
            return
        self.setWindowOpacity(0.0)
        self.show()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(220)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()
        self._anim = anim


class PopupManager:
    """Stacks popups above the taskbar, newest at the bottom, at most three at a time."""

    MAX = 3

    def __init__(self, engine, open_page: Callable[[str], None], respond: Callable[[int, str], None],
                 act: Callable[[object, int | None], None]) -> None:
        self.engine = engine
        self.open_page = open_page
        self.respond = respond
        self.act = act
        self.popups: list[Popup] = []
        self.shown_episode_ids: list[int] = []
        self.reduce_motion = False
        self.enabled = True

    def _place(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        y = geo.bottom() - 8
        for pop in reversed(self.popups):
            pop.adjustSize()
            h = pop.sizeHint().height()
            y -= h
            pop.move(QPoint(geo.right() - pop.width() - 8, y))
            y -= 4

    def _add(self, pop: Popup) -> Popup:
        while len(self.popups) >= self.MAX:
            victim = next((p for p in self.popups if not p.persistent), self.popups[0])
            victim.dismiss()
        pop.closed.connect(self._removed)
        self.popups.append(pop)
        if self.enabled:
            self._place()
            pop.fade_in()
        return pop

    def _removed(self, pop: Popup) -> None:
        if pop in self.popups:
            self.popups.remove(pop)
            self._place()

    def show_episode(self, ep: Episode) -> Popup:
        d = ep.diagnosis
        lvl = int(ep.level)
        mood = RULE_MOOD.get(d.rule, Mood.PUFFY)
        actions = [a for a in d.actions if a.kind != "lower_priority"]
        first = actions[0] if actions else None

        def primary():
            if first is None or first.kind.startswith("show_") or first.kind == "biggest_folders":
                page = {"show_detective": "detective", "show_timeline": "timeline", "show_checkup": "checkup",
                        "biggest_folders": "checkup"}.get(first.kind if first else "", "home")
                self.open_page(page)
            else:
                self.act(first, ep.id)

        primary_label = "Show me" if first is None or first.kind.startswith("show_") else first.label
        pop = Popup(Level(lvl).title, d.cause, d.tip, mood, lvl, persistent=lvl >= Level.UH_OH,
                    primary=(primary_label, primary),
                    secondary=("I meant to do that", lambda: self.respond(ep.id, "intentional")),
                    link=("Not a problem", lambda: self.respond(ep.id, "not_a_problem")),
                    timeout_s=self.engine.cfg.heads_up_popup_s, reduce_motion=self.reduce_motion)
        pop.episode_id = ep.id
        pop.destroyed.connect(lambda *_: None)
        if lvl >= Level.UH_OH:
            # Dismissing an Uh-oh is remembered so it doesn't nag again (ALR-8).
            pop.closed.connect(lambda _p, eid=ep.id: self.respond(eid, "dismiss_silent"))
        self.shown_episode_ids.append(ep.id)
        return self._add(pop)

    def show_summary(self, held: list[Episode]) -> Popup:
        n = len(held)
        worst = max((int(e.level) for e in held), default=2)
        names = ", ".join(sorted({e.diagnosis.title for e in held})[:3])
        pop = Popup("While you were busy", f"{n} thing{'s' if n != 1 else ''} came up: {names}.",
                    "Nothing urgent. Have a look whenever you like.", Mood.HAPPY, worst, persistent=False,
                    primary=("Show inbox", lambda: self.open_page("inbox")), secondary=None,
                    timeout_s=self.engine.cfg.heads_up_popup_s * 1.5, reduce_motion=self.reduce_motion)
        return self._add(pop)

    def show_feed(self, fe) -> Popup:
        pop = Popup("Yum!", fe.text, "Thanks for closing it. I feel lighter already.", Mood.PROUD, 1,
                    persistent=False, primary=None, secondary=None, timeout_s=6.0, reduce_motion=self.reduce_motion)
        return self._add(pop)

    def show_notice(self, notice) -> Popup:
        def go():
            self.open_page("checkup" if notice.action in ("run_checkup", "run_benchmark") else "home")

        pop = Popup(notice.title, notice.text, "", Mood.HAPPY, 1, persistent=False,
                    primary=("Let's do it", go), secondary=("Not now", lambda: self.engine.dismiss_notice(
                        notice.id, declined_checkup=notice.action == "run_checkup")),
                    timeout_s=self.engine.cfg.heads_up_popup_s * 1.5, reduce_motion=self.reduce_motion)
        return self._add(pop)

    def close_all(self) -> None:
        for p in list(self.popups):
            p.dismiss()
