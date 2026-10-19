"""Reusable "Soft Serve" widgets: cards, chips, soft ring gauges, jelly bars, icons."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLayout, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget, QWidgetItem)

from .theme import LEVEL_FILL, LEVEL_STROKE, STATUS_FILL, THEME


# ------------------------------------------------------------------------ cards
class Card(QFrame):
    """A rounded surface that floats on a soft warm shadow instead of a border."""

    def __init__(self, parent: QWidget | None = None, alt: bool = False, padding: int = 20, spacing: int = 12,
                 shadow: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("CardAlt" if alt else "Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(padding, padding, padding, padding)
        self.lay.setSpacing(spacing)
        if shadow and not THEME.high_contrast:
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(16)
            eff.setOffset(0, 3)
            c = THEME.color("shadow")
            c.setAlphaF(0.07 if THEME.palette.name == "light" else 0.35)
            eff.setColor(c)
            self.setGraphicsEffect(eff)

    def add(self, w, stretch: int = 0):
        if isinstance(w, QLayout):
            self.lay.addLayout(w, stretch)
        else:
            self.lay.addWidget(w, stretch)
        return w


def label(text: str = "", kind: str = "", wrap: bool = False, muted: bool = False, sel: bool = False) -> QLabel:
    lab = QLabel(text)
    if kind:
        lab.setObjectName(kind)
    if muted:
        lab.setProperty("muted", True)
    lab.setWordWrap(wrap)
    if sel:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


def button(text: str, kind: str = "", tip: str = "", on_click=None, icon_name: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if tip:
        b.setToolTip(tip)
    b.setAccessibleName(text or tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.TabFocus)
    if icon_name:
        b.setIcon(icon(icon_name))
        b.setIconSize(QSize(18, 18))
    if on_click:
        b.clicked.connect(on_click)
    return b


def hbox(*items, spacing: int = 10, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


def vbox(*items, spacing: int = 8, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, QLayout):
            lay.addLayout(it)
        else:
            lay.addWidget(it)
    return lay


def clear_layout(lay: QLayout) -> None:
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


# ------------------------------------------------------------------------ chips
class Chip(QLabel):
    """A small rounded tag: alert level, check status, activity."""

    def __init__(self, text: str, fill: str = "surface_alt", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.fill = fill
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.restyle()

    def restyle(self) -> None:
        p = THEME.palette
        bg = getattr(p, self.fill)
        fg = p.on_fill if self.fill != "surface_alt" else p.text
        self.setStyleSheet(f"background:{bg}; color:{fg}; border-radius: 11px; padding: 3px 11px;"
                           f" font-size: 12px; font-weight: 800;")


def level_chip(level: int) -> Chip:
    names = {1: "Psst", 2: "Heads up", 3: "Uh-oh"}
    c = Chip(names.get(level, "Info"), LEVEL_FILL.get(level, "surface_alt"))
    c.setAccessibleName(f"Level: {names.get(level, 'info')}")
    return c


def status_chip(status: str) -> Chip:
    from ..checkup import STATUS_LABEL

    c = Chip(STATUS_LABEL.get(status, status), STATUS_FILL.get(status, "surface_alt"))
    c.setAccessibleName(f"Status: {STATUS_LABEL.get(status, status)}")
    return c


# ------------------------------------------------------------------- gauges
class RingGauge(QWidget):
    """A soft ring with a rounded end and the 'your normal' band drawn around it."""

    def __init__(self, title: str, parent: QWidget | None = None, size: int = 132) -> None:
        super().__init__(parent)
        self.title = title
        self.value = 0.0          # 0..100: how full the ring is
        self.value_text = "--"
        self.caption = ""         # a small word inside the ring, under the number
        self.sub = ""             # one line under the title
        self.band: tuple[float, float] | None = None
        self.state = "good"       # good | warning | critical | info
        self._size = size
        self.setMinimumSize(size, size + 52)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setAccessibleName(title)

    def sizeHint(self) -> QSize:
        return QSize(self._size + 24, self._size + 52)

    def set(self, value: float | None, text: str, sub: str = "", band: tuple[float, float] | None = None,
            state: str = "good", caption: str = "") -> None:
        self.value = max(0.0, min(100.0, value or 0.0))
        self.value_text, self.sub, self.band, self.state, self.caption = text, sub, band, state, caption
        self.setAccessibleDescription(f"{self.title}: {text} {caption}. {sub}".replace("  ", " "))
        self.setToolTip(f"{self.title}: {text} {caption}\n{sub}".replace("  ", " "))
        self.update()

    def _fit(self, text: str, max_w: float, start: int, low: int, weight: int, tabular: bool = False):
        from PySide6.QtGui import QFontMetricsF

        for px in range(start, low - 1, -1):
            f = THEME.font(px, weight, tabular=tabular)
            if QFontMetricsF(f).horizontalAdvance(text) <= max_w:
                return f
        return THEME.font(low, weight, tabular=tabular)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = THEME.palette
        d = float(min(self.width() - 16, self._size))       # ring diameter
        thick = max(9.0, round(d * 0.085))
        cx = self.width() / 2
        rect = QRectF(cx - d / 2 + thick / 2, 4 + thick / 2, d - thick, d - thick)
        # Track: one soft ring, open at the bottom.
        p.setPen(QPen(QColor(pal.surface_alt), thick, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 225 * 16, -270 * 16)
        # Value: ONE clean arc in the state colour (>= 3:1 against the card).
        color = {"good": pal.good_strong, "warning": pal.warning_strong, "critical": pal.critical_strong,
                 "info": pal.info_strong}.get(self.state, pal.good_strong)
        if self.value > 0.5:
            p.setPen(QPen(QColor(color), thick, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 225 * 16, int(-self.value * 2.7 * 16))
        # Number (shrunk to fit inside the ring) and its caption.
        inner = d - 2 * thick - 12
        p.setPen(QColor(pal.text))
        p.setFont(self._fit(self.value_text, inner * 0.92, 24, 13, 800, tabular=True))
        num_box = QRectF(rect.x(), rect.center().y() - 18, rect.width(), 28)
        if not self.caption:
            num_box.moveTop(rect.center().y() - 14)
        p.drawText(num_box, Qt.AlignCenter, self.value_text)
        if self.caption:
            p.setPen(QColor(pal.text_muted))
            p.setFont(self._fit(self.caption, inner * 0.92, 12, 10, 600))
            p.drawText(QRectF(rect.x(), rect.center().y() + 10, rect.width(), 18), Qt.AlignHCenter, self.caption)
        # Title and one line under the whole ring, never on it.
        y = 4 + d + 6
        p.setPen(QColor(pal.text))
        p.setFont(self._fit(self.title, self.width() - 8, 14, 11, 700))
        p.drawText(QRectF(0, y, self.width(), 20), Qt.AlignHCenter | Qt.AlignVCenter, self.title)
        p.setPen(QColor(pal.text_muted))
        p.setFont(self._fit(self.sub, self.width() - 8, 12, 10, 600))
        p.drawText(QRectF(0, y + 20, self.width(), 18), Qt.AlignHCenter | Qt.AlignVCenter, self.sub)
        p.end()


class JellyBar(QWidget):
    """A rounded 'jelly' bar: one continuous stacked bar, flat segments with small gaps.

    ``segments`` = [(fraction, fill token, strong token)]. The strong token colours the
    segment (>= 3:1 against the track); fill token "muted" draws a quiet grey segment.
    """

    def __init__(self, parent: QWidget | None = None, height: int = 16) -> None:
        super().__init__(parent)
        self.segments: list[tuple[float, str, str]] = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_segments(self, segments: list[tuple[float, str, str]], description: str = "") -> None:
        self.segments = segments
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = THEME.palette
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rad = r.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal.surface_alt))
        p.drawRoundedRect(r, rad, rad)
        clip = QPainterPath()
        clip.addRoundedRect(r, rad, rad)
        p.setClipPath(clip)
        x = r.x()
        gap = 2.0
        n = len(self.segments)
        for i, (frac, fill, strong) in enumerate(self.segments):
            w = max(0.0, min(1.0, frac)) * r.width()
            if w <= 0.5:
                continue
            if fill == "muted":
                c = QColor(pal.text_muted)
                c.setAlphaF(0.45)
            else:
                c = QColor(getattr(pal, strong))
            p.setBrush(c)
            # flat segments; the bar's rounded clip gives the two outer ends their curve
            seg_w = w - (gap if i < n - 1 and w > gap * 2 else 0.0)
            p.drawRect(QRectF(x, r.y(), seg_w, r.height()))
            x += w
        p.end()


STATE_TOKEN = {"good": "good_strong", "warning": "warning_strong", "critical": "critical_strong",
               "info": "info_strong", "off": "text_muted"}


class StatTile(QFrame):
    """One reading in a soft box: what it is, the number, one quiet line, and an optional bar or button."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Tile")
        self.title = title
        self.state = "off"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)
        self.dot = Dot("text_muted", 10)
        self.title_label = label(title, muted=True)
        lay.addLayout(hbox(self.dot, self.title_label, None, spacing=6))
        self.value_label = label("--", "Stat")
        lay.addWidget(self.value_label)
        self.sub_label = label("", muted=True, wrap=True)
        lay.addWidget(self.sub_label)
        self.bar = JellyBar(height=10)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.action = button("", "Ghost")
        self.action.hide()
        self._on_action = None
        self.action.clicked.connect(lambda: self._on_action() if self._on_action else None)
        lay.addWidget(self.action, 0, Qt.AlignLeft)
        lay.addStretch(1)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setAccessibleName(title)

    def set(self, value: str, sub: str = "", state: str = "good", bar: float | None = None) -> None:
        """``state``: good | warning | critical | info | off (no reading). ``bar``: 0..1 or None."""
        self.state = state
        self.dot.token = STATE_TOKEN.get(state, "info_strong")
        self.dot.update()
        self.value_label.setText(value)
        self.sub_label.setText(sub)
        self.sub_label.setVisible(bool(sub))
        if bar is None:
            self.bar.hide()
        else:
            self.bar.set_segments([(bar, state, STATE_TOKEN.get(state, "info_strong"))], f"{self.title}: {value}")
            self.bar.show()
        text = f"{self.title}: {value}" + (f". {sub}" if sub else "")
        self.setAccessibleDescription(text)
        self.setToolTip(text.replace(". ", "\n", 1))

    def set_action(self, text: str | None, on_click=None) -> None:
        self._on_action = on_click
        self.action.setText(text or "")
        self.action.setAccessibleName(text or "")
        self.action.setVisible(bool(text))


class RiceCakes(QWidget):
    """Calm streak shown as a row of little rice cakes."""

    def __init__(self, parent: QWidget | None = None, max_cakes: int = 14) -> None:
        super().__init__(parent)
        self.count = 0
        self.max_cakes = max_cakes
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_count(self, n: int) -> None:
        self.count = n
        self.setAccessibleName(f"Calm streak: {n} days")
        self.setToolTip(f"{n} calm day{'s' if n != 1 else ''} in a row (no Uh-oh alerts)")
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = THEME.palette
        shown = min(self.count, self.max_cakes)
        for i in range(self.max_cakes):
            x = 4 + i * 24
            if x + 22 > self.width():
                break
            filled = i < shown
            empty_edge = QColor(pal.text_muted)
            empty_edge.setAlphaF(0.45)
            p.setPen(QPen(QColor(pal.mochi_outline) if filled else empty_edge, 1.6))
            p.setBrush(QColor(pal.mochi_body) if filled else Qt.NoBrush)
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, 8, 20, 16), 8, 8)
            p.drawPath(path)
            if filled:
                p.setPen(Qt.NoPen)
                b = QColor(pal.blush)
                p.setBrush(b)
                p.drawEllipse(QRectF(x + 3, 16, 4, 3))
                p.drawEllipse(QRectF(x + 13, 16, 4, 3))
        p.end()


class SpeechBubble(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(44)
        self.restyle()

    def restyle(self) -> None:
        p = THEME.palette
        self.setStyleSheet(f"background:{p.surface_alt}; color:{p.text}; border-radius: 16px; padding: 10px 16px;"
                           f" font-size: 14px; font-weight: 600;")


class Banner(QFrame):
    """A gentle inline notice (demo mode, learning mode, warnings)."""

    def __init__(self, text: str, fill: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.fill = fill
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        self.text = QLabel(text)
        self.text.setWordWrap(True)
        lay.addWidget(self.text, 1)
        self.restyle()

    def restyle(self) -> None:
        p = THEME.palette
        self.setStyleSheet(f"QFrame {{ background:{getattr(p, self.fill)}; border-radius: 14px; }}"
                           f" QLabel {{ color:{p.on_fill}; font-weight: 700; }}")


# ---------------------------------------------------------------- flow layout
class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 14) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def addWidget(self, w: QWidget) -> None:
        self.addChildWidget(w)
        self.addItem(QWidgetItem(w))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _do_layout(self, rect: QRect, test: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + sp
            if nx - sp > rect.right() and line_h > 0:
                x = rect.x()
                y += line_h + sp
                nx = x + hint.width() + sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


# ---------------------------------------------------------------------- icons
def icon(name: str, color: str | None = None, size: int = 20) -> QIcon:
    return QIcon(icon_pixmap(name, color, size))


def icon_pixmap(name: str, color: str | None = None, size: int = 20, dpr: float = 2.0) -> QPixmap:
    col = QColor(color or THEME.palette.text)
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24.0, size / 24.0)
    pen = QPen(col, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    if name == "home":
        path.moveTo(4, 11)
        path.lineTo(12, 4)
        path.lineTo(20, 11)
        path.moveTo(6.5, 9.5)
        path.lineTo(6.5, 19.5)
        path.lineTo(17.5, 19.5)
        path.lineTo(17.5, 9.5)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(10.2, 13.5, 3.6, 6), 1.2, 1.2)
    elif name == "timeline":
        path.moveTo(3, 15)
        path.cubicTo(6, 15, 6, 7, 9, 7)
        path.cubicTo(12, 7, 12, 17, 15, 17)
        path.cubicTo(18, 17, 18, 10, 21, 10)
        p.drawPath(path)
        p.drawLine(QPointF(3, 20.5), QPointF(21, 20.5))
    elif name == "detective":
        p.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        p.drawLine(QPointF(15, 15), QPointF(20.5, 20.5))
    elif name == "checkup":
        path.moveTo(12, 20)
        path.cubicTo(4, 14, 3, 10, 3.5, 8)
        path.cubicTo(4.5, 4, 9.5, 3.5, 12, 7)
        path.cubicTo(14.5, 3.5, 19.5, 4, 20.5, 8)
        path.cubicTo(21, 10, 20, 14, 12, 20)
        p.drawPath(path)
        p.drawPolyline([QPointF(6, 11.5), QPointF(9, 11.5), QPointF(10.5, 9), QPointF(12.5, 14), QPointF(14, 11.5),
                        QPointF(18, 11.5)])
    elif name == "diary":
        p.drawRoundedRect(QRectF(5, 3.5, 14, 17), 2.5, 2.5)
        p.drawLine(QPointF(8.5, 3.5), QPointF(8.5, 20.5))
        p.drawLine(QPointF(11.5, 8.5), QPointF(16, 8.5))
        p.drawLine(QPointF(11.5, 12), QPointF(16, 12))
    elif name == "settings":
        for y, x in ((7, 15), (12, 8), (17, 13)):
            p.drawLine(QPointF(4, y), QPointF(20, y))
            p.setBrush(QColor(THEME.palette.surface))
            p.drawEllipse(QPointF(x, y), 2.4, 2.4)
            p.setBrush(Qt.NoBrush)
    elif name == "bell":
        path.moveTo(6, 16.5)
        path.cubicTo(7, 15, 7, 13, 7, 10.5)
        path.cubicTo(7, 7.5, 9.2, 5.5, 12, 5.5)
        path.cubicTo(14.8, 5.5, 17, 7.5, 17, 10.5)
        path.cubicTo(17, 13, 17, 15, 18, 16.5)
        path.closeSubpath()
        p.drawPath(path)
        p.drawArc(QRectF(10, 17, 4, 3.5), 180 * 16, 180 * 16)
    elif name == "refresh":
        p.drawArc(QRectF(5, 5, 14, 14), 30 * 16, 290 * 16)
        p.drawPolyline([QPointF(16, 3.5), QPointF(18.3, 7.2), QPointF(14.3, 8.2)])
    elif name == "folder":
        path.moveTo(3.5, 7)
        path.lineTo(9.5, 7)
        path.lineTo(11, 9)
        path.lineTo(20.5, 9)
        path.lineTo(20.5, 18.5)
        path.lineTo(3.5, 18.5)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "slow":
        p.drawEllipse(QPointF(12, 13), 7.5, 7.5)
        p.drawLine(QPointF(12, 13), QPointF(12, 9))
        p.drawLine(QPointF(12, 13), QPointF(15, 14.5))
        p.drawLine(QPointF(10, 3.5), QPointF(14, 3.5))
    elif name == "close":
        p.drawLine(QPointF(7, 7), QPointF(17, 17))
        p.drawLine(QPointF(17, 7), QPointF(7, 17))
    elif name == "stop":
        p.drawRoundedRect(QRectF(6, 6, 12, 12), 3, 3)
    elif name == "play":
        path.moveTo(8, 5.5)
        path.lineTo(18.5, 12)
        path.lineTo(8, 18.5)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "moon":
        path.moveTo(15, 4)
        path.cubicTo(8, 4, 5, 9.5, 6.5, 14)
        path.cubicTo(8, 19, 15, 21, 19.5, 16.5)
        path.cubicTo(13, 17.5, 10, 10, 15, 4)
        p.drawPath(path)
    elif name == "export":
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawPolyline([QPointF(8, 8), QPointF(12, 4), QPointF(16, 8)])
        p.drawPolyline([QPointF(5, 13), QPointF(5, 19.5), QPointF(19, 19.5), QPointF(19, 13)])
    elif name == "trash":
        p.drawLine(QPointF(5, 7), QPointF(19, 7))
        p.drawPolyline([QPointF(7, 7), QPointF(8, 20), QPointF(16, 20), QPointF(17, 7)])
        p.drawPolyline([QPointF(10, 7), QPointF(10.5, 4.5), QPointF(13.5, 4.5), QPointF(14, 7)])
    elif name == "sparkle":
        path.moveTo(12, 3)
        path.quadTo(12.5, 11.5, 21, 12)
        path.quadTo(12.5, 12.5, 12, 21)
        path.quadTo(11.5, 12.5, 3, 12)
        path.quadTo(11.5, 11.5, 12, 3)
        p.drawPath(path)
    else:  # dot
        p.drawEllipse(QPointF(12, 12), 4, 4)
    p.end()
    return px


def sky_pixmap(sky: str, size: int = 40) -> QPixmap:
    """Little weather drawings for the daily forecast."""
    pal = THEME.palette
    px = QPixmap(size * 2, size * 2)
    px.setDevicePixelRatio(2.0)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 40.0, size / 40.0)
    sun = "sunny" in sky.lower() or "cloudy" in sky.lower()
    if sun:
        p.setPen(QPen(QColor(pal.warning_strong), 2, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(QColor(pal.warning))
        cx, cy = (16, 16) if "cloudy" in sky.lower() else (20, 20)
        p.drawEllipse(QPointF(cx, cy), 8, 8)
        for i in range(8):
            a = i * math.pi / 4
            p.drawLine(QPointF(cx + 11 * math.cos(a), cy + 11 * math.sin(a)), QPointF(cx + 14 * math.cos(a), cy + 14 * math.sin(a)))
    if "cloud" in sky.lower() or "shower" in sky.lower() or "storm" in sky.lower():
        p.setPen(QPen(QColor(pal.info_strong), 2))
        p.setBrush(QColor(pal.surface))
        cloud = QPainterPath()
        cloud.addRoundedRect(QRectF(10, 20, 26, 12), 6, 6)
        cloud.addEllipse(QPointF(18, 21), 6, 6)
        cloud.addEllipse(QPointF(26, 19), 7, 7)
        p.drawPath(cloud.simplified())
        if "shower" in sky.lower():
            p.setPen(QPen(QColor(pal.info_strong), 2, Qt.SolidLine, Qt.RoundCap))
            for x in (16, 23, 30):
                p.drawLine(QPointF(x, 35), QPointF(x - 2, 39))
        if "storm" in sky.lower():
            p.setPen(QPen(QColor(pal.warning_strong), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline([QPointF(24, 33), QPointF(20, 37), QPointF(25, 37), QPointF(21, 40)])
    p.end()
    return px


class Dot(QWidget):
    """A small painted colour dot for legends (font glyphs aren't reliable everywhere)."""

    def __init__(self, token: str, size: int = 12, ring: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.token = token
        self.ring = ring
        self.setFixedSize(size, size)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = QColor(getattr(THEME.palette, self.token))
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self.ring:
            p.setPen(QPen(col, 2))
            p.setBrush(Qt.NoBrush)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(col)
        p.drawEllipse(r)
        p.end()


class Avatar(QWidget):
    """Letter avatar used when an app's own icon isn't available."""

    def __init__(self, name: str, size: int = 40, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.letter = (name.strip()[:1] or "?").upper()
        self.setFixedSize(size, size)
        tokens = ("accent", "info", "good", "warning")
        self.fill = tokens[sum(map(ord, name)) % len(tokens)]

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = THEME.palette
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(getattr(pal, self.fill)))
        p.drawRoundedRect(r, 12, 12)
        p.setPen(QColor(pal.on_fill))
        p.setFont(THEME.font(int(self.height() * 0.45), 800))
        p.drawText(r, Qt.AlignCenter, self.letter)
        p.end()


class ClickableCard(Card):
    clicked = Signal()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


def level_stroke(level: int) -> str:
    return getattr(THEME.palette, LEVEL_STROKE.get(level, "info_strong"))
