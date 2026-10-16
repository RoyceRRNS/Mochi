"""Shared page scaffolding and small formatting helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from ...engine import Engine, SampleView
from ..bridge import EngineBridge


@dataclass
class UiContext:
    engine: Engine
    bridge: EngineBridge
    navigate: Callable[[str], None]
    toast: Callable[[str], None]
    confirm: Callable[[str, str, str], bool]
    respond: Callable[[int, str], None]
    act: Callable[[Any, int | None], None]
    run_checkup: Callable[[], None]
    run_benchmark: Callable[[], None]
    nerd: Callable[[], bool]
    window: Any = None


class Page(QWidget):
    key = "page"
    title = ""
    subtitle = ""

    def __init__(self, ctx: UiContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.setObjectName("Page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("ScrollBody")
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(28, 8, 28, 28)
        self.body.setSpacing(18)
        self.scroll.setWidget(inner)
        outer.addWidget(self.scroll)

    def on_view(self, view: SampleView) -> None:  # live sample
        pass

    def on_show(self) -> None:  # navigated to
        pass

    def on_nerd(self, on: bool) -> None:
        pass


def time_ago(ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    d = max(0, now - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return time.strftime("%a %d %b, %H:%M", time.localtime(ts))


def pct(v: float | None, digits: int = 0) -> str:
    return "--" if v is None else f"{v:.{digits}f}%"
