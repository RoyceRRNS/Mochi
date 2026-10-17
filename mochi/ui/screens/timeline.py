"""Timeline: live and historical graphs with the "your normal" band and anomaly markers."""

from __future__ import annotations

import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout

from ...engine import SampleView
from ...model import METRICS
from ..theme import LEVEL_STROKE, THEME
from ..widgets import Card, Dot, button, hbox, label
from .common import Page

RANGES = [("1 h", 3600), ("24 h", 86400), ("7 d", 7 * 86400)]
LABELS = {"cpu": "CPU", "ram": "Memory", "paging": "Paging", "disk": "Disk busy", "temp": "Temperature",
          "discharge": "Battery drain"}

pg.setConfigOptions(antialias=True)


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as [start, end) index pairs."""
    out = []
    start = None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


class TimelinePage(Page):
    key = "timeline"
    title = "Timeline"
    subtitle = "Live and past readings against your normal band"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.metric_key = "cpu"
        self.seconds = 3600
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.metric_group = QButtonGroup(self)
        for key in ("cpu", "ram", "paging", "disk", "temp", "discharge"):
            b = button(LABELS[key], "Chip")
            b.setCheckable(True)
            b.setChecked(key == self.metric_key)
            b.clicked.connect(lambda _=False, k=key: self.set_metric(k))
            b.setProperty("metric", key)
            self.metric_group.addButton(b)
            controls.addWidget(b)
        controls.addStretch(1)
        self.range_group = QButtonGroup(self)
        for text, secs in RANGES:
            b = button(text, "Chip")
            b.setCheckable(True)
            b.setChecked(secs == self.seconds)
            b.clicked.connect(lambda _=False, s=secs: self.set_range(s))
            b.setProperty("seconds", secs)
            self.range_group.addButton(b)
            controls.addWidget(b)
        self.body.addLayout(controls)

        card = Card(padding=16)
        self.unavailable = label("", muted=True, wrap=True)
        self.unavailable.hide()
        card.add(self.unavailable)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setMinimumHeight(380)
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=False, y=True, alpha=0.12)
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        self.plot.getPlotItem().setClipToView(True)
        self.plot.setAccessibleName("Timeline graph")
        card.add(self.plot)
        legend = hbox(self._legend_swatch("good", "Your normal (usual range)"), self._legend_swatch("text", "Reading"),
                      self._legend_swatch("warning_strong", "Something unusual (hover a star)"), None, spacing=18)
        card.add(legend)
        self.nerd_label = label("", muted=True, wrap=True, sel=True)
        card.add(self.nerd_label)
        self.body.addWidget(card, 1)
        self.summary = label("", muted=True, wrap=True)
        self.body.addWidget(self.summary)
        self._items: list = []
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60_000)
        self._refresh_timer.timeout.connect(self.refresh)
        self._last_refresh = 0.0
        self.last_data: dict | None = None
        self._style_plot()

    def _legend_swatch(self, token: str, text: str):
        return hbox(Dot(token, 12), label(text, muted=True), spacing=6)

    def _style_plot(self) -> None:
        pal = THEME.palette
        self.plot.setBackground(pal.surface)
        for ax in ("left", "bottom"):
            a = self.plot.getAxis(ax)
            a.setPen(pg.mkPen(pal.text_muted))
            a.setTextPen(pg.mkPen(pal.text_muted))
            a.setStyle(tickFont=THEME.font(11, 600))

    def set_metric(self, key: str) -> None:
        self.metric_key = key
        for b in self.metric_group.buttons():
            b.setChecked(b.property("metric") == key)
        self.refresh()

    def set_range(self, seconds: int) -> None:
        self.seconds = seconds
        for b in self.range_group.buttons():
            b.setChecked(b.property("seconds") == seconds)
        self.refresh()

    def on_show(self) -> None:
        self.refresh()
        self._refresh_timer.start()

    def on_view(self, v: SampleView) -> None:
        if not self.isVisible():
            return
        if self.seconds <= 3600 or time.monotonic() - self._last_refresh > 60:
            self.refresh()
        self._nerd(v)

    def _nerd(self, v: SampleView) -> None:
        m = self.metric_key
        info = METRICS[m]
        val = v.metrics.get(m)
        b = v.bands.get(m)
        if not self.ctx.nerd():
            self.nerd_label.setText(f"Now: {val:.1f} {info.unit}" if val is not None else "")
            return
        z = v.z.get(m)
        parts = [f"now {val:.2f} {info.unit}" if val is not None else "now: n/a"]
        if b:
            parts.append(f"usual mean {b[0]:.2f} ± {b[1]:.2f} (sigma floor {info.min_std})")
        parts.append(f"z = {z:.2f}" if z is not None else "z: not trusted yet")
        parts.append(f"bucket: slot {v.slot} · {v.activity} · level {v.bucket_levels.get(m) or 'none'}")
        parts.append(f"min rise to count: {info.min_delta} {info.unit}")
        self.nerd_label.setText(" · ".join(parts))

    def refresh(self) -> None:
        self._last_refresh = time.monotonic()
        eng = self.ctx.engine
        latest = eng.latest
        reason = latest.unavailable.get(self.metric_key) if latest else None
        if self.metric_key == "discharge" and latest is not None and latest.battery is not None and latest.battery.on_ac:
            reason = reason or "You're plugged in, so there's no battery drain to show right now."
        self.unavailable.setVisible(bool(reason))
        if reason:
            self.unavailable.setText(f"{LABELS[self.metric_key]} isn't available: {reason}.")
        try:
            data = eng.timeline(self.metric_key, self.seconds)
        except Exception as exc:
            self.summary.setText(f"Couldn't load the timeline ({exc.__class__.__name__}).")
            return
        self.last_data = data
        self._draw(data)
        if latest is not None:
            self._nerd(latest)

    def _draw(self, data: dict) -> None:
        pal = THEME.palette
        for it in self._items:
            self.plot.removeItem(it)
        self._items.clear()
        rows = data["rows"]
        if not rows:
            self.summary.setText("No readings in this range yet.")
            return
        arr = np.array([[r[0], np.nan if r[1] is None else r[1], np.nan if r[2] is None else r[2],
                         np.nan if r[3] is None else r[3]] for r in rows], dtype=float)
        ts, val, mu, sd = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
        lo, hi = mu - 2 * sd, mu + 2 * sd
        if self.metric_key in ("cpu", "disk"):
            lo = np.clip(lo, 0, 100)
            hi = np.clip(hi, 0, 100)
        good = QColor(pal.good)
        good.setAlphaF(0.30 if pal.name == "light" else 0.22)
        edge = pg.mkPen(QColor(pal.good_strong), width=1)
        mask = ~np.isnan(lo) & ~np.isnan(hi)
        for a, b in runs(mask)[-200:]:
            if b - a < 2:
                continue
            c1 = pg.PlotDataItem(ts[a:b], lo[a:b], pen=edge)
            c2 = pg.PlotDataItem(ts[a:b], hi[a:b], pen=edge)
            fill = pg.FillBetweenItem(c1, c2, brush=pg.mkBrush(good))
            for it in (fill, c1, c2):
                self.plot.addItem(it)
                self._items.append(it)
        line = pg.PlotDataItem(ts, val, pen=pg.mkPen(QColor(pal.text), width=2), connect="finite")
        self.plot.addItem(line)
        self._items.append(line)
        spots = []
        for m in data["markers"]:
            x = m["opened_ts"]
            idx = int(np.clip(np.searchsorted(ts, x), 0, len(ts) - 1))
            y = val[idx] if not math.isnan(val[idx]) else np.nanmax(val) if np.any(~np.isnan(val)) else 0
            col = QColor(getattr(pal, LEVEL_STROKE.get(int(m["max_level"] or 1), "info_strong")))
            spots.append({"pos": (x, y), "data": f"{m['title']}: {m['cause']}", "brush": pg.mkBrush(col),
                          "pen": pg.mkPen(QColor(pal.surface), width=1.5), "size": 18, "symbol": "star"})
        if spots:
            sc = pg.ScatterPlotItem(hoverable=True, tip=lambda x, y, data: data, hoverSize=24)
            sc.addPoints(spots)
            self.plot.addItem(sc)
            self._items.append(sc)
        info = METRICS[self.metric_key]
        self.plot.setLabel("left", f"{LABELS[self.metric_key]} ({info.unit})", color=pal.text_muted)
        self.plot.setXRange(ts[-1] - self.seconds, ts[-1], padding=0.01)
        finite = val[~np.isnan(val)]
        if finite.size:
            within = np.mean((val[mask] >= lo[mask]) & (val[mask] <= hi[mask])) * 100 if mask.any() else None
            parts = [f"{len(rows)} readings", f"average {finite.mean():.1f} {info.unit}", f"peak {finite.max():.1f} {info.unit}"]
            if within is not None:
                parts.append(f"{within:.0f}% inside your normal band")
            parts.append(f"{len(data['markers'])} unusual moment{'s' if len(data['markers']) != 1 else ''}")
            self.summary.setText(" · ".join(parts))
