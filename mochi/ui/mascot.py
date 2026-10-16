"""Mochi, drawn from simple shapes (MOOD-3, MOOD-4).

A squashed rounded blob, two dot eyes, a small curved mouth and pink blush ovals. Each
mood changes only a few features: eye shape, mouth, body squash and one small prop.
The same painter draws the big Home mascot, the notification face and the tray icon
(simplified to just the face at small sizes). Original artwork, no existing characters.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..moods import LABELS, Mood
from .theme import THEME, Palette


def ease_out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _body_path(cx: float, base: float, w: float, h: float) -> QPainterPath:
    """A soft rice-cake blob with a slightly flattened bottom."""
    left, right, top = cx - w / 2, cx + w / 2, base - h
    path = QPainterPath()
    path.moveTo(cx, base)
    path.cubicTo(cx + w * 0.42, base, right, base - h * 0.08, right, base - h * 0.42)
    path.cubicTo(right, base - h * 0.82, cx + w * 0.30, top, cx, top)
    path.cubicTo(cx - w * 0.30, top, left, base - h * 0.82, left, base - h * 0.42)
    path.cubicTo(left, base - h * 0.08, cx - w * 0.42, base, cx, base)
    path.closeSubpath()
    return path


def paint_mochi(p: QPainter, rect: QRectF, mood: Mood, phase: float = 0.0, squish: float = 1.0,
                palette: Palette | None = None, face_only: bool = False, animate: bool = True) -> None:
    pal = palette or THEME.palette
    s = min(rect.width(), rect.height())
    ox = rect.x() + (rect.width() - s) / 2
    oy = rect.y() + (rect.height() - s) / 2
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(ox, oy)
    p.scale(s, s)

    breathe = 1.0 + (0.015 + 0.015 * math.sin(phase * 2 * math.pi)) if animate else 1.0
    w, h = 0.84, 0.60
    bounce = 0.0
    tilt = 0.0
    if mood == Mood.PUFFY:
        w, h = 0.92, 0.70
    elif mood == Mood.GRUMPY:
        w, h = 0.90, 0.46
    elif mood == Mood.SLEEPY:
        h = 0.56
    elif mood == Mood.ZOOMIES and animate:
        bounce = -abs(math.sin(phase * 4 * math.pi)) * 0.07
    elif mood == Mood.DIZZY and animate:
        tilt = math.sin(phase * 4 * math.pi) * 6.0
    if face_only:
        w, h = 0.96, 0.84
        breathe = 1.0
    w *= breathe / math.sqrt(squish)
    h *= breathe * squish
    cx, base = 0.5, (0.97 if face_only else 0.90) + bounce

    if tilt:
        p.translate(cx, base)
        p.rotate(tilt)
        p.translate(-cx, -base)

    outline = QColor(pal.mochi_outline)
    if not face_only:
        shadow = QColor(pal.shadow)
        shadow.setAlphaF(0.10)
        p.setPen(Qt.NoPen)
        p.setBrush(shadow)
        p.drawEllipse(QRectF(cx - w * 0.42, 0.9 - 0.035, w * 0.84, 0.07))

    body = _body_path(cx, base, w, h)
    pen = QPen(outline, 0.06 if face_only else 0.022)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(QColor(pal.mochi_body))
    p.drawPath(body)

    # Face anchor
    fy = base - h * 0.52
    ex = w * 0.17
    eye_r = (0.075 if face_only else 0.042)
    ink = QColor(pal.mochi_outline)

    # Blush
    if not face_only or s >= 24:
        blush = QColor(pal.blush)
        blush.setAlphaF(0.75)
        p.setPen(Qt.NoPen)
        p.setBrush(blush)
        bw = 0.11 if mood != Mood.PUFFY else 0.15
        p.drawEllipse(QRectF(cx - w * 0.33 - bw / 2, fy + 0.05, bw, bw * 0.55))
        p.drawEllipse(QRectF(cx + w * 0.33 - bw / 2, fy + 0.05, bw, bw * 0.55))

    p.setBrush(ink)
    lw = 0.07 if face_only else 0.022
    line = QPen(ink, lw)
    line.setCapStyle(Qt.RoundCap)

    # Eyes
    if mood == Mood.SLEEPY:
        p.setPen(line)
        p.setBrush(Qt.NoBrush)
        for sx in (-1, 1):
            r = QRectF(cx + sx * ex - eye_r * 1.3, fy - eye_r * 0.6, eye_r * 2.6, eye_r * 1.6)
            p.drawArc(r, 200 * 16, 140 * 16)
    elif mood == Mood.DIZZY:
        p.setPen(QPen(ink, lw * 0.8, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        for sx in (-1, 1):
            spiral = QPainterPath()
            ccx, ccy = cx + sx * ex, fy
            turns = 2.2
            steps = 40
            rot = phase * 2 * math.pi if animate else 0
            for i in range(steps + 1):
                t = i / steps
                ang = rot + t * turns * 2 * math.pi * sx
                rr = eye_r * 1.5 * t
                pt = QPointF(ccx + rr * math.cos(ang), ccy + rr * math.sin(ang))
                spiral.moveTo(pt) if i == 0 else spiral.lineTo(pt)
            p.drawPath(spiral)
    elif mood == Mood.PROUD:
        p.setPen(line)
        p.setBrush(Qt.NoBrush)
        for sx in (-1, 1):  # happy closed eyes ^ ^
            r = QRectF(cx + sx * ex - eye_r * 1.2, fy - eye_r * 0.4, eye_r * 2.4, eye_r * 2.0)
            p.drawArc(r, 20 * 16, 140 * 16)
    elif mood == Mood.GRUMPY:
        p.setPen(Qt.NoPen)
        for sx in (-1, 1):
            p.drawEllipse(QPointF(cx + sx * ex, fy + eye_r * 0.3), eye_r * 0.9, eye_r * 0.8)
        p.setPen(line)
        for sx in (-1, 1):  # frowning brows
            p.drawLine(QPointF(cx + sx * (ex + eye_r * 1.4), fy - eye_r * 1.9), QPointF(cx + sx * (ex - eye_r * 0.6), fy - eye_r * 1.2))
    else:
        p.setPen(Qt.NoPen)
        er = eye_r * (1.15 if mood == Mood.ZOOMIES else 1.0)
        for sx in (-1, 1):
            p.drawEllipse(QPointF(cx + sx * ex, fy), er, er * (1.1 if not face_only else 1.0))
        if not face_only:
            hl = QColor("#FFFFFF")
            p.setBrush(hl)
            for sx in (-1, 1):
                p.drawEllipse(QPointF(cx + sx * ex + er * 0.35, fy - er * 0.4), er * 0.32, er * 0.32)
            p.setBrush(ink)

    # Mouth
    p.setPen(line)
    p.setBrush(Qt.NoBrush)
    my = fy + (0.085 if not face_only else 0.16)
    mw = 0.10 if not face_only else 0.20
    if mood == Mood.GRUMPY:
        p.drawArc(QRectF(cx - mw / 2, my, mw, mw * 0.6), 20 * 16, 140 * 16)
    elif mood == Mood.PUFFY:
        p.setBrush(ink)
        p.drawEllipse(QPointF(cx, my + 0.01), mw * 0.18, mw * 0.16)
        p.setBrush(Qt.NoBrush)
    elif mood == Mood.DIZZY:
        wave = QPainterPath()
        wave.moveTo(cx - mw / 2, my + 0.01)
        for i in range(1, 5):
            wave.quadTo(cx - mw / 2 + (i - 0.5) * mw / 4, my + (0.03 if i % 2 else -0.01), cx - mw / 2 + i * mw / 4, my + 0.01)
        p.drawPath(wave)
    elif mood == Mood.SLEEPY:
        p.drawEllipse(QPointF(cx, my + 0.015), mw * 0.12, mw * 0.10)
    elif mood in (Mood.ZOOMIES, Mood.PROUD):
        smile = QPainterPath()
        smile.moveTo(cx - mw * 0.6, my - 0.005)
        smile.quadTo(cx, my + mw * 0.9, cx + mw * 0.6, my - 0.005)
        smile.closeSubpath()
        p.setBrush(QColor(pal.critical))
        p.drawPath(smile)
        p.setBrush(Qt.NoBrush)
    else:
        p.drawArc(QRectF(cx - mw / 2, my - mw * 0.35, mw, mw * 0.7), 200 * 16, 140 * 16)

    if face_only:
        p.restore()
        return

    # Props
    top = base - h
    if mood == Mood.SWEATY:
        drop = QColor(pal.info_strong)
        p.setPen(Qt.NoPen)
        p.setBrush(drop)
        for i, (dx, dy, sc) in enumerate(((0.36, 0.06, 1.0), (0.44, 0.18, 0.7))):
            off = (math.sin((phase + i * 0.3) * 2 * math.pi) * 0.012) if animate else 0
            x, y = cx + w * dx, top + dy + off
            path = QPainterPath()
            path.moveTo(x, y - 0.05 * sc)
            path.cubicTo(x + 0.035 * sc, y, x + 0.03 * sc, y + 0.04 * sc, x, y + 0.04 * sc)
            path.cubicTo(x - 0.03 * sc, y + 0.04 * sc, x - 0.035 * sc, y, x, y - 0.05 * sc)
            p.drawPath(path)
    elif mood == Mood.SLEEPY:
        f = QFont(THEME.family)
        f.setPixelSize(10)
        f.setWeight(QFont.Weight.Black)
        p.setPen(QColor(pal.info_strong))
        for i in range(3):
            t = ((phase + i / 3) % 1.0) if animate else i / 3
            x = cx + w * 0.28 + t * 0.16
            y = top + 0.04 - t * 0.18
            size = 0.085 + t * 0.06
            p.save()
            p.translate(x, y)
            p.scale(size / 10, size / 10)
            col = QColor(pal.info_strong)
            col.setAlphaF(max(0.0, 1.0 - t))
            p.setPen(col)
            p.setFont(f)
            p.drawText(QPointF(0, 0), "z")
            p.restore()
    elif mood == Mood.PROUD:
        crown = QPainterPath()
        cw, ch = 0.22, 0.11
        x0, y0 = cx - cw / 2, top + 0.035
        crown.moveTo(x0, y0)
        crown.lineTo(x0, y0 - ch * 0.55)
        crown.lineTo(x0 + cw * 0.25, y0 - ch * 0.25)
        crown.lineTo(x0 + cw * 0.5, y0 - ch)
        crown.lineTo(x0 + cw * 0.75, y0 - ch * 0.25)
        crown.lineTo(x0 + cw, y0 - ch * 0.55)
        crown.lineTo(x0 + cw, y0)
        crown.closeSubpath()
        p.setPen(QPen(QColor(pal.warning_strong), 0.012, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(pal.warning))
        p.drawPath(crown)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal.warning_strong))
        for i, (sx, sy) in enumerate(((-0.42, -0.02), (0.45, 0.06), (-0.36, 0.22))):
            tw = (0.6 + 0.4 * math.sin((phase + i * 0.33) * 2 * math.pi)) if animate else 1.0
            _sparkle(p, QPointF(cx + w * sx, top + sy), 0.035 * tw)
    elif mood == Mood.ZOOMIES:
        p.setPen(QPen(QColor(pal.info_strong), 0.014, Qt.SolidLine, Qt.RoundCap))
        for i in range(3):
            y = base - h * (0.25 + i * 0.2)
            p.drawLine(QPointF(cx - w * 0.62 - i * 0.02, y), QPointF(cx - w * 0.54, y))
    p.restore()


def _sparkle(p: QPainter, c: QPointF, r: float) -> None:
    path = QPainterPath()
    path.moveTo(c.x(), c.y() - r)
    path.quadTo(c.x(), c.y(), c.x() + r, c.y())
    path.quadTo(c.x(), c.y(), c.x(), c.y() + r)
    path.quadTo(c.x(), c.y(), c.x() - r, c.y())
    path.quadTo(c.x(), c.y(), c.x(), c.y() - r)
    p.drawPath(path)


def mochi_pixmap(mood: Mood, size: int, palette: Palette | None = None, face_only: bool | None = None,
                 dpr: float = 1.0, badge: str | None = None) -> QPixmap:
    face_only = size <= 32 if face_only is None else face_only
    px = QPixmap(int(size * dpr), int(size * dpr))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    p = QPainter(px)
    paint_mochi(p, QRectF(0, 0, size, size), mood, 0.25, 1.0, palette, face_only=face_only, animate=False)
    if badge:
        pal = palette or THEME.palette
        r = max(4.0, size * 0.20)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(pal.mochi_outline), max(1.0, size / 16)))
        p.setBrush(QColor(pal.info_strong if badge == "psst" else pal.critical_strong))
        p.drawEllipse(QPointF(size - r - 0.5, r + 0.5), r, r)
    p.end()
    return px


class MochiWidget(QWidget):
    poked = Signal()

    def __init__(self, size: int = 220, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.mood = Mood.HAPPY
        self.phase = 0.0
        self.squish = 1.0
        self._squish_start: float | None = None
        self.reduce_motion = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._last = time.monotonic()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setMinimumSize(QSize(int(size * 0.6), int(size * 0.6)))
        self._update_a11y()

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def _update_a11y(self) -> None:
        name, desc = LABELS[self.mood]
        self.setAccessibleName(f"Mochi is {name.lower()}")
        self.setAccessibleDescription(desc)
        self.setToolTip(f"{name}: {desc}\nClick to poke me.")

    def set_mood(self, mood: Mood) -> None:
        if mood != self.mood:
            self.mood = mood
            self._update_a11y()
            self.start_squish()
            self.update()

    def set_reduce_motion(self, on: bool) -> None:
        self.reduce_motion = on
        self._sync_timer()
        self.update()

    @property
    def animating(self) -> bool:
        return self._timer.isActive()

    def _sync_timer(self) -> None:
        want = self.isVisible() and (not self.reduce_motion or self._squish_start is not None)
        if want and not self._timer.isActive():
            self._last = time.monotonic()
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self._timer.stop()  # UI-8: no animation while hidden

    def start_squish(self) -> None:
        if self.reduce_motion:
            return
        self._squish_start = time.monotonic()
        self._sync_timer()

    def _tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if not self.reduce_motion:
            self.phase = (self.phase + dt / 3.0) % 1.0  # 3-second breathing loop
        if self._squish_start is not None:
            t = (now - self._squish_start) / 0.25
            if t >= 1:
                self.squish = 1.0
                self._squish_start = None
                self._sync_timer()
            else:
                self.squish = 0.86 + 0.14 * ease_out_back(t)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        paint_mochi(p, QRectF(self.rect()), self.mood, self.phase, self.squish, animate=not self.reduce_motion)
        if self.hasFocus():
            p.setPen(QPen(THEME.color("accent_strong"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 16, 16)
        p.end()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.start_squish()
            self.poked.emit()
        super().mousePressEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.start_squish()
            self.poked.emit()
            return
        super().keyPressEvent(e)
