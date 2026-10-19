"""The optional elevated sensor helper: ``python -m mochi.sensor_helper --out FILE ...``.

It reads CPU temperature, GPU temperature and fan speeds (Lenovo firmware sensors need
admin) every few seconds and writes them to ``--out`` as JSON. It takes no other input,
runs nothing else, and exits when Mochi exits, when ``--stop`` appears, or when the
sensors stop answering.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Callable

from .sensors import LenovoGameZone, SensorError, SensorReading, read_lhm


def parent_alive(pid: int) -> bool:
    import psutil

    return psutil.pid_exists(pid)


def is_link(p: Path) -> bool:
    """A symlink or junction (any reparse point). The helper runs as admin, so it never writes through one."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def safe_folder(folder: Path) -> bool:
    """The feed folder and its parent must be real folders, so a planted junction can't redirect writes."""
    return folder.is_dir() and not is_link(folder) and not is_link(folder.parent)


def write_atomic(path: Path, text: str) -> None:
    # A fresh, randomly named temp file created exclusively (fails rather than following a planted link),
    # then renamed over the feed file (a rename replaces a link instead of writing through it).
    tmp = path.with_name(f".sensors-{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_reader() -> Callable[[], SensorReading | None]:
    try:
        gz = LenovoGameZone()
        return gz.read
    except SensorError:
        return read_lhm


def run(out: Path, stop: Path, parent: int, interval: float, reader: Callable[[], SensorReading | None],
        alive: Callable[[int], bool] | None = None, sleep: Callable[[float], None] = time.sleep,
        max_failures: int = 20, max_loops: int | None = None) -> str:
    """The helper loop. Returns why it stopped."""
    alive = alive or parent_alive
    failures = 0
    loops = 0
    while True:
        if stop.exists():
            return "stop file"
        if not alive(parent):
            return "Mochi exited"
        try:
            reading = reader()
        except Exception:
            reading = None
        if reading is None or reading.empty:
            failures += 1
            if failures >= max_failures:
                return "sensors stopped answering"
        else:
            failures = 0
            try:
                write_atomic(out, reading.to_json())
            except OSError:
                failures += 1
        loops += 1
        if max_loops is not None and loops >= max_loops:
            return "done"
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mochi.sensor_helper")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stop", required=True)
    ap.add_argument("--parent", type=int, required=True)
    ap.add_argument("--interval", type=float, default=3.0)
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.name != "sensors.json" or not out.is_absolute() or not safe_folder(out.parent):
        return 2  # only ever writes Mochi's own feed file, in a real folder
    run(out, Path(a.stop), a.parent, max(1.0, min(60.0, a.interval)), default_reader())
    try:
        if not is_link(out):
            out.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
