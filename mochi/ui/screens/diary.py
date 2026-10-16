"""Diary: Mochi's weekly entries, achievements, calm streak and personal records."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ...moods import Mood
from ..mascot import mochi_pixmap
from ..theme import THEME
from ..widgets import Card, FlowLayout, RiceCakes, clear_layout, hbox, icon_pixmap, label, vbox
from .common import Page


class Badge(QWidget):
    def __init__(self, title: str, desc: str, unlocked: bool, progress: str) -> None:
        super().__init__()
        self.unlocked = unlocked
        self.setFixedSize(210, 96)
        self.setAccessibleName(f"{title}: {'unlocked' if unlocked else 'locked'}. {desc} {progress}")
        self.setToolTip(f"{desc}\n{progress}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        medal = QLabel()
        medal.setFixedSize(52, 52)
        medal.setPixmap(self._medal())
        lay.addWidget(medal)
        lay.addLayout(vbox(label(title, "H3"), label(desc if unlocked else progress, muted=True, wrap=True), spacing=2), 1)

    def _medal(self):
        from PySide6.QtGui import QPixmap

        pal = THEME.palette
        px = QPixmap(104, 104)
        px.setDevicePixelRatio(2.0)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(pal.warning_strong if self.unlocked else pal.text_muted), 2.5))
        p.setBrush(QColor(pal.warning if self.unlocked else pal.surface_alt))
        p.drawEllipse(QRectF(3, 3, 46, 46))
        p.drawPixmap(14, 14, icon_pixmap("sparkle", pal.on_fill if self.unlocked else pal.text_muted, 24))
        p.end()
        return px


class DiaryPage(Page):
    key = "diary"
    title = "Mochi's diary"
    subtitle = "The week in my own words, plus badges and records"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        top = QHBoxLayout()
        top.setSpacing(18)
        self.entries_card = Card(padding=20)
        self.entries_card.add(label("Diary", "H2"))
        self.entries = vbox(spacing=12)
        self.entries_card.add(self.entries)
        self.entries_card.lay.addStretch(1)
        top.addWidget(self.entries_card, 6)
        side = vbox(spacing=18)
        calm = Card(padding=18)
        calm.add(label("Calm streak", "H3"))
        self.cakes = RiceCakes()
        self.calm_text = label("", muted=True)
        calm.add(self.cakes)
        calm.add(self.calm_text)
        side.addWidget(calm)
        rec = Card(padding=18)
        rec.add(label("Personal records", "H3"))
        self.records = vbox(spacing=6)
        rec.add(self.records)
        side.addWidget(rec)
        side.addStretch(1)
        top.addLayout(side, 4)
        self.body.addLayout(top)
        ach = Card(padding=20)
        ach.add(label("Achievements", "H2"))
        self.badges_host = QWidget()
        self.badges = FlowLayout(self.badges_host, spacing=12)
        ach.add(self.badges_host)
        self.body.addWidget(ach)
        self.body.addStretch(1)

    def on_show(self) -> None:
        eng = self.ctx.engine
        clear_layout(self.entries)
        try:
            entries = eng.diary()
        except Exception:
            entries = []
        if not entries:
            self.entries.addWidget(label("My first entry will appear after we've spent some time together.", muted=True,
                                         wrap=True))
        for i, e in enumerate(entries):
            pic = QLabel()
            mood = Mood.HAPPY if "calm" in e.text.lower() else Mood.PUFFY if "puffy" in e.text else Mood.SLEEPY
            pic.setPixmap(mochi_pixmap(mood, 48, face_only=False))
            pic.setFixedSize(52, 52)
            text = label(e.text, wrap=True)
            text.setStyleSheet("font-size: 14px;")
            self.entries.addLayout(hbox(pic, vbox(label(e.title, "H3"), text, spacing=4), spacing=12))
        streak = eng.calm_streak()
        self.cakes.set_count(streak)
        self.calm_text.setText(f"{streak} day{'s' if streak != 1 else ''} without an Uh-oh")
        clear_layout(self.records)
        recs = eng.records()
        if not recs:
            self.records.addWidget(label("No records yet. Give it a few days!", muted=True))
        for k, v in recs.items():
            self.records.addLayout(hbox(label(k, muted=True), None, label(v, "H3")))
        clear_layout(self.badges)
        for a in eng.achievements():
            if a.hidden:
                continue
            self.badges.addWidget(Badge(a.title, a.description, a.unlocked_ts is not None, a.progress))
