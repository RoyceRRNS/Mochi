"""Shared safety helpers for the fault-injection scripts (TST-5).

Every script has a hard time limit, cleans up on Ctrl+C, and refuses settings that
could make the PC unusable.
"""

from __future__ import annotations

import atexit
import signal
import sys
import threading
import time
from typing import Callable

MAX_SECONDS = 1800


class Stopper:
    def __init__(self, seconds: float, cleanup: Callable[[], None] | None = None) -> None:
        self.deadline = time.monotonic() + min(seconds, MAX_SECONDS)
        self.event = threading.Event()
        self._cleanup = cleanup
        self._cleaned = False
        atexit.register(self.cleanup)
        for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    pass

    def _on_signal(self, *_):
        self.event.set()

    @property
    def stopped(self) -> bool:
        return self.event.is_set() or time.monotonic() >= self.deadline

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; returns True if it's time to stop."""
        remaining = self.deadline - time.monotonic()
        self.event.wait(max(0.0, min(seconds, remaining)))
        return self.stopped

    def cleanup(self) -> None:
        if not self._cleaned and self._cleanup is not None:
            self._cleaned = True
            try:
                self._cleanup()
            except Exception as exc:  # pragma: no cover - best effort
                print(f"cleanup failed: {exc}", file=sys.stderr)


def touch(buf: bytearray) -> None:
    """Write one byte per page so the OS really commits the memory."""
    buf[::4096] = b"\x01" * len(range(0, len(buf), 4096))
