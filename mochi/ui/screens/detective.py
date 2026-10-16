"""'Who ate my RAM?' detective mode: a suspect line-up of the top memory users."""

from __future__ import annotations

import os

from PySide6.QtCore import QFileInfo, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFileIconProvider, QFrame, QLabel, QWidget

from ...diagnosis import Action
from ...model import GB, fmt_bytes, fmt_duration
from ..theme import THEME
from ..widgets import Avatar, Card, Dot, FlowLayout, JellyBar, button, clear_layout, hbox, label, vbox
from .common import Page

SEG_COLORS = [("accent", "accent_strong"), ("info", "info_strong"), ("warning", "warning_strong"),
              ("good", "good_strong"), ("critical", "critical_strong")]


class Stamp(QWidget):
    """A rotated 'PRIME SUSPECT' stamp drawn over the guiltiest card."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(150, 40)
        self.setAccessibleName("Prime suspect")

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = QColor(THEME.palette.critical_strong)
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(-10)
        p.setPen(QPen(col, 2.5))
        p.setBrush(Qt.NoBrush)
        r = QRectF(-66, -16, 132, 32)
        p.drawRoundedRect(r, 8, 8)
        f = QFont(THEME.family)
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Black)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        p.setFont(f)
        p.setPen(col)
        p.drawText(r, Qt.AlignCenter, "PRIME SUSPECT")
        p.end()


class SuspectCard(Card):
    def __init__(self, page: "DetectivePage", row: dict) -> None:
        super().__init__(padding=16, spacing=6)
        self.row = row
        self.setFixedSize(258, 262)
        self.setAccessibleName(f"{row['display']} uses {fmt_bytes(row['in_ram'])}")
        exe = row.get("exe")
        if exe and os.path.exists(exe):
            ic = QLabel()
            ic.setPixmap(QFileIconProvider().icon(QFileInfo(exe)).pixmap(40, 40))
            ic.setFixedSize(44, 44)
        else:
            ic = Avatar(row["display"], 40)
        name = label(row["display"], "H3", wrap=True)
        self.add(hbox(ic, name, spacing=10))
        if row.get("prime"):
            self.stamp = Stamp(self)
            self.add(hbox(None, self.stamp))
        mem = label(fmt_bytes(row["in_ram"]) + " in memory", wrap=True)
        mem.setStyleSheet("font-size: 20px; font-weight: 800;")
        self.add(mem)
        growing = row["growing_s"]
        grow_txt = f"growing for {fmt_duration(growing)}" if growing >= 600 else "steady"
        if row.get("leak"):
            grow_txt = "growing steadily and never letting go"
        self.add(label(f"{row['procs']} process{'es' if row['procs'] != 1 else ''} · {grow_txt}", muted=True, wrap=True))
        self.add(label(f"committed {fmt_bytes(row['private'])} · CPU {row['cpu_core']:.0f}% of a core", muted=True,
                       wrap=True))
        self.lay.addStretch(1)
        ctx = page.ctx
        target = {"pid": row["pid"], "create_time": row["create_time"], "app": row["app"], "display": row["display"]}
        if row.get("is_system"):
            self.add(label("Part of Windows; best left alone.", muted=True))
        else:
            self.add(hbox(
                button("End…", "Primary", on_click=lambda: ctx.act(Action("end_process", "End", target), None)),
                button("", "", "Let it run slower", lambda: ctx.act(Action("lower_priority", "Slower", target), None), "slow"),
                button("", "", "Show where it lives", lambda: ctx.act(Action("open_location", "Where", {"path": row.get("exe")}), None),
                       "folder"),
                None, spacing=6))


class DetectivePage(Page):
    key = "detective"
    title = "Who ate my RAM?"
    subtitle = "The suspect line-up: apps holding the most memory right now"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        top = Card(padding=18)
        self.summary = label("", wrap=True)
        self.summary.setStyleSheet("font-size: 15px; font-weight: 700;")
        row = hbox(self.summary, button("Refresh", "", "Look again", self.refresh, "refresh"))
        row.setStretch(0, 1)
        top.add(row)
        self.bar = JellyBar(height=22)
        top.add(self.bar)
        self.legend = QWidget()
        self.legend_lay = FlowLayout(self.legend, spacing=12)
        top.add(self.legend)
        self.body.addWidget(top)
        self.cards_host = QWidget()
        self.cards = FlowLayout(self.cards_host, spacing=16)
        self.body.addWidget(self.cards_host)
        self.note = label("Memory shown is what each app actually has in RAM right now (its private working set), "
                          "so the numbers never add up to more than your RAM. 'Committed' includes memory parked in "
                          "the page file.", muted=True, wrap=True)
        self.body.addWidget(self.note)
        self.body.addStretch(1)
        self.rows: list[dict] = []

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        eng = self.ctx.engine
        self.rows = eng.detective()
        clear_layout(self.cards)
        clear_layout(self.legend_lay)
        latest = eng.latest
        if not self.rows or latest is None:
            self.summary.setText("I'm still gathering suspects...")
            return
        total = latest.ram_total_gb * GB
        used = latest.ram_used_gb * GB
        segs = []
        shown = 0
        for i, r in enumerate(self.rows[:5]):
            fill, stroke = SEG_COLORS[i % len(SEG_COLORS)]
            segs.append((r["in_ram"] / total, fill, stroke))
            shown += r["in_ram"]
            dot = Dot(stroke)
            w = QWidget()
            w.setLayout(hbox(dot, label(f"{r['display']} {fmt_bytes(r['in_ram'])}", muted=True), spacing=4))
            self.legend_lay.addWidget(w)
        others = max(0.0, used - shown)
        segs.append((others / total, "surface_alt", "text_muted"))
        w = QWidget()
        w.setLayout(hbox(label("Everything else " + fmt_bytes(others), muted=True)))
        self.legend_lay.addWidget(w)
        self.bar.set_segments(segs, f"{fmt_bytes(used)} of {fmt_bytes(total)} in use")
        prime = next((r for r in self.rows if r.get("prime")), self.rows[0])
        verdict = "growing and never letting go" if prime.get("leak") else "holding the most memory"
        self.summary.setText(f"{fmt_bytes(used)} of {fmt_bytes(total)} in use. Prime suspect: {prime['display']}, {verdict}.")
        for r in self.rows:
            self.cards.addWidget(SuspectCard(self, r))
        self.cards_host.updateGeometry()
