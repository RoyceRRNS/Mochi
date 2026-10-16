"""The system-tray icon: Mochi's face is the status light (UI-1, ALR-6).

The icon is drawn at several sizes (16 px is just the face) with a dark outline on a
light body, so it reads on both light and dark taskbars. A small dot means there's
something in the inbox. Windows 11 hides new tray icons in the overflow menu, so
onboarding tells people how to pin it.
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..moods import LABELS, Mood
from .mascot import mochi_pixmap
from .theme import LIGHT

TRAY_SIZES = (16, 20, 24, 32, 40, 48, 64)


def tray_icon(mood: Mood, badge: str | None = None) -> QIcon:
    ic = QIcon()
    for s in TRAY_SIZES:
        ic.addPixmap(mochi_pixmap(mood, s, LIGHT, face_only=s <= 32, badge=badge))
    return ic


def tray_tooltip(mood: Mood, badge_count: int, snoozed_until: float, now: float | None = None) -> str:
    name, desc = LABELS[mood]
    text = f"Mochi: {name}. {desc}"
    now = time.time() if now is None else now
    if snoozed_until and snoozed_until > now:
        text += f" Alerts snoozed until {time.strftime('%H:%M', time.localtime(snoozed_until))}."
    if badge_count:
        text += f" {badge_count} new in your inbox."
    return text[:127]  # Windows tray tooltips are limited to 127 characters


class TrayController(QObject):
    def __init__(self, open_window: Callable[[], None], run_checkup: Callable[[], None],
                 snooze: Callable[[float | None], None], quit_app: Callable[[], None]) -> None:
        super().__init__()
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray = QSystemTrayIcon()
        self.mood = Mood.HAPPY
        self.badge: str | None = None
        self.menu = QMenu()
        self.act_open = QAction("Open Mochi", self.menu, triggered=open_window)
        self.act_check = QAction("Run a check-up", self.menu, triggered=run_checkup)
        self.snooze_menu = QMenu("Snooze alerts", self.menu)
        self.act_snooze_hour = QAction("For 1 hour", self.snooze_menu, triggered=lambda: snooze(3600))
        self.act_snooze_tomorrow = QAction("Until tomorrow morning", self.snooze_menu, triggered=lambda: snooze(None))
        self.act_resume = QAction("Resume alerts", self.snooze_menu, triggered=lambda: snooze(0))
        for a in (self.act_snooze_hour, self.act_snooze_tomorrow, self.act_resume):
            self.snooze_menu.addAction(a)
        self.act_quit = QAction("Quit Mochi", self.menu, triggered=quit_app)
        self.menu.addAction(self.act_open)
        self.menu.addAction(self.act_check)
        self.menu.addMenu(self.snooze_menu)
        self.menu.addSeparator()
        self.menu.addAction(self.act_quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(lambda reason: open_window()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.update(Mood.HAPPY, 0, 0.0)
        if self.available:
            self.tray.show()

    def update(self, mood: Mood, badge_count: int, snoozed_until: float, psst: bool = False,
               now: float | None = None) -> None:
        now = time.time() if now is None else now
        badge = "psst" if (badge_count or psst) else None
        if mood != self.mood or badge != self.badge or self.tray.icon().isNull():
            self.tray.setIcon(tray_icon(mood, badge))
            self.mood, self.badge = mood, badge
        self.tray.setToolTip(tray_tooltip(mood, badge_count, snoozed_until, now))
        self.act_resume.setEnabled(bool(snoozed_until and snoozed_until > now))

    def hint(self, title: str, text: str) -> None:
        if self.available:
            self.tray.showMessage(title, text, tray_icon(self.mood), 4000)

    def hide(self) -> None:
        self.tray.hide()
