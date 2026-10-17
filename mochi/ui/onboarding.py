"""First-run onboarding (UI-2): what Mochi does, privacy, learning mode, pinning the tray
icon (Windows 11 hides new ones), and which features need admin."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from ..moods import Mood
from .mascot import MochiWidget
from .widgets import Card, button, label

PAGES = [
    (Mood.HAPPY, "Hi, I'm Mochi!",
     "I learn what \"normal\" looks like on your PC, then tell you in plain words when something is off, why it's "
     "happening, and one thing to do about it."),
    (Mood.SLEEPY, "Your data stays here",
     "Everything I learn stays on this PC. I never connect to the internet. I note app names and readings, never "
     "window titles, web addresses or what you type. You can export or delete it all in Settings."),
    (Mood.ZOOMIES, "First, I learn",
     "For the first 3 days I'm in learning mode: I only warn about truly risky things (like a nearly full disk). "
     "After that I compare every reading with your own normal for that hour and activity."),
    (Mood.PROUD, "Keep me in sight",
     "Windows 11 tucks new tray icons away. To see my face at a glance: Settings > Personalization > Taskbar > "
     "Other system tray icons > turn on Mochi.\n\nTemperature and detailed disk health (SMART) need admin rights or "
     "LibreHardwareMonitor. Everything else works without them."),
]


class Onboarding(QDialog):
    def __init__(self, parent: QWidget | None = None, autostart_default: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to Mochi")
        self.setModal(True)
        self.setMinimumSize(560, 520)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        card = Card(padding=26)
        outer.addWidget(card)
        self.stack = QStackedWidget()
        self.mascots: list[MochiWidget] = []
        for mood, title, text in PAGES:
            page = QWidget()
            lay = QVBoxLayout(page)
            m = MochiWidget(170)
            m.set_mood(mood)
            m.setFocusPolicy(Qt.NoFocus)
            self.mascots.append(m)
            lay.addWidget(m, 0, Qt.AlignHCenter)
            lay.addWidget(label(title, "H1"), 0, Qt.AlignHCenter)
            body = label(text, wrap=True)
            body.setAlignment(Qt.AlignCenter)
            body.setStyleSheet("font-size: 15px;")
            lay.addWidget(body)
            lay.addStretch(1)
            self.stack.addWidget(page)
        card.add(self.stack)
        self.autostart = QCheckBox("Start Mochi when I sign in (so I don't miss a day of learning)")
        self.autostart.setChecked(autostart_default)
        card.add(self.autostart)
        self.dots = QLabel()
        self.dots.setAlignment(Qt.AlignCenter)
        nav = QHBoxLayout()
        self.back = button("Back", "Ghost", on_click=lambda: self.go(-1))
        self.next = button("Next", "Primary", on_click=lambda: self.go(1))
        nav.addWidget(self.back)
        nav.addStretch(1)
        nav.addWidget(self.dots)
        nav.addStretch(1)
        nav.addWidget(self.next)
        card.add(nav)
        self.go(0)

    def go(self, delta: int) -> None:
        i = self.stack.currentIndex() + delta
        if i >= self.stack.count():
            self.accept()
            return
        i = max(0, i)
        self.stack.setCurrentIndex(i)
        last = i == self.stack.count() - 1
        self.back.setEnabled(i > 0)
        self.next.setText("Let's go!" if last else "Next")
        self.autostart.setVisible(last)
        self.dots.setText(f"Step {i + 1} of {self.stack.count()}")
        self.dots.setAccessibleName(f"Step {i + 1} of {self.stack.count()}")
