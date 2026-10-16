"""Engine -> Qt bridge. Engine events arrive on worker threads; Qt signals queue them onto
the GUI thread automatically. ``run_async`` keeps slow work (ending a process, the
speed test, the check-up, folder scans) off the GUI thread."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot

from ..engine import Engine, EngineEvent

log = logging.getLogger(__name__)


class EngineBridge(QObject):
    sample = Signal(object)
    alert = Signal(object)
    mood = Signal(object)
    notice = Signal(object)
    feed = Signal(object)
    status = Signal(object)
    benchmark = Signal(object)
    benchmark_started = Signal(object)
    checkup = Signal(object)
    config = Signal(object)
    launch = Signal(object)
    progress = Signal(str, float)
    _task_done = Signal(object, object, object)

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self._task_done.connect(self._finish)
        engine.subscribe(self._relay)

    def _relay(self, ev: EngineEvent) -> None:
        sig = getattr(self, ev.kind, None)
        if isinstance(sig, Signal.__class__) or hasattr(sig, "emit"):
            try:
                sig.emit(ev.data)
            except RuntimeError:
                pass  # the bridge is being destroyed at shutdown

    def run_async(self, fn: Callable[[], Any], done: Callable[[Any], None] | None = None,
                  failed: Callable[[BaseException], None] | None = None) -> threading.Thread:
        def work():
            try:
                result = fn()
                self._task_done.emit(done, result, None)
            except BaseException as exc:  # noqa: BLE001 - reported back to the UI
                log.exception("background task failed")
                self._task_done.emit(failed, None, exc)

        t = threading.Thread(target=work, name="mochi-ui-task", daemon=True)
        t.start()
        return t

    @Slot(object, object, object)
    def _finish(self, cb, result, exc) -> None:
        if cb is None:
            return
        try:
            cb(exc if exc is not None else result)
        except RuntimeError as err:
            # The window that asked for this work was closed before it finished.
            if "already deleted" not in str(err):
                raise
            log.debug("dropped a late result: %s", err)
