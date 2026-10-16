"""Rotating log file in the app-data folder (LIF-8): at most 5 x 2 MB = 10 MB."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 4


def setup(log_dir: Path, level: int = logging.INFO) -> RotatingFileHandler:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "mochi.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root = logging.getLogger("mochi")
    root.setLevel(level)
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler):
            root.removeHandler(h)
            h.close()
    root.addHandler(handler)
    return handler
