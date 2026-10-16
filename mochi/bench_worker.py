"""Micro-benchmarks, run in a separate process (BEN-1).

Running them in a thread would hold the GIL, freeze the UI and delay samples. As a
subprocess they also measure the machine more cleanly.

    python -m mochi.bench_worker --tests cpu,mem,disk --reps 3 --dir <folder> --disk-mb 256
    python -m mochi.bench_worker --load 20            # all-core load for the check-up

The disk test bypasses the OS cache (FILE_FLAG_NO_BUFFERING on Windows, O_DIRECT on
Linux); otherwise reading back a file you just wrote measures RAM, not the disk (BEN-5).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import multiprocessing
import os
import sys
import time

CHUNK = 4 * 1024 * 1024


def cpu_test(scale: float = 1.0) -> float:
    data = b"\x5a" * (1024 * 1024)
    rounds = max(1, int(300 * scale))
    t0 = time.perf_counter()
    h = hashlib.sha256()
    for _ in range(rounds):
        h.update(data)
    h.hexdigest()
    return time.perf_counter() - t0


def mem_test(scale: float = 1.0) -> float:
    size = max(1, int(128 * scale)) * 1024 * 1024
    src = bytearray(size)
    src[::4096] = b"\x01" * len(src[::4096])
    dst = bytearray(size)
    t0 = time.perf_counter()
    for _ in range(6):
        dst[:] = src
    return time.perf_counter() - t0


def _disk_windows(path: str, size: int) -> tuple[float, float, str]:
    import win32file

    flags = 0x20000000 | 0x80000000  # FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH
    buf = mmap.mmap(-1, CHUNK)       # page-aligned, as unbuffered I/O requires
    buf.write(os.urandom(CHUNK))
    h = win32file.CreateFile(path, win32file.GENERIC_WRITE, 0, None, win32file.CREATE_ALWAYS, flags, None)
    t0 = time.perf_counter()
    try:
        written = 0
        while written < size:
            win32file.WriteFile(h, buf)
            written += CHUNK
        win32file.FlushFileBuffers(h)
    finally:
        h.Close()
    t_write = time.perf_counter() - t0
    h = win32file.CreateFile(path, win32file.GENERIC_READ, 0, None, win32file.OPEN_EXISTING, 0x20000000, None)
    rbuf = mmap.mmap(-1, CHUNK)
    t0 = time.perf_counter()
    try:
        read = 0
        while read < size:
            _, data = win32file.ReadFile(h, rbuf)
            if not data:
                break
            read += len(data)
    finally:
        h.Close()
    return t_write, time.perf_counter() - t0, "unbuffered"


def _disk_generic(path: str, size: int) -> tuple[float, float, str]:
    mode = "buffered"
    block = os.urandom(CHUNK)
    t0 = time.perf_counter()
    with open(path, "wb") as fh:
        written = 0
        while written < size:
            fh.write(block)
            written += CHUNK
        fh.flush()
        os.fsync(fh.fileno())
    t_write = time.perf_counter() - t0
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    if getattr(os, "O_DIRECT", 0):
        mode = "direct"
    buf = mmap.mmap(-1, CHUNK)
    t0 = time.perf_counter()
    fd = os.open(path, flags)
    try:
        while True:
            n = os.readv(fd, [buf]) if hasattr(os, "readv") else len(os.read(fd, CHUNK))
            if n <= 0:
                break
    finally:
        os.close(fd)
    return t_write, time.perf_counter() - t0, mode


def disk_test(folder: str, mb: int) -> tuple[float, float, str]:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"mochi_bench_{os.getpid()}.tmp")
    size = max(1, mb * 1024 * 1024 // CHUNK) * CHUNK
    try:
        if sys.platform == "win32":
            try:
                return _disk_windows(path, size)
            except Exception:
                return _disk_generic(path, size)
        return _disk_generic(path, size)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _burn(until: float) -> int:
    n = 0
    data = b"x" * 65536
    while time.time() < until:
        hashlib.sha256(data).digest()
        n += 1
    return n


def load(seconds: float, procs: int | None = None) -> dict:
    procs = procs or os.cpu_count() or 1
    until = time.time() + seconds
    with multiprocessing.Pool(procs) as pool:
        counts = pool.map(_burn, [until] * procs)
    return {"load_s": seconds, "procs": procs, "work": sum(counts)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", default="cpu,mem,disk")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--disk-mb", type=int, default=256)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--load", type=float, default=0.0)
    ap.add_argument("--procs", type=int, default=0)
    a = ap.parse_args(argv)
    if a.load > 0:
        print(json.dumps(load(a.load, a.procs or None)), flush=True)
        return 0
    tests = [t for t in a.tests.split(",") if t]
    out: dict[str, object] = {}
    for t in tests:
        if t == "cpu":
            out["cpu"] = [cpu_test(a.scale) for _ in range(a.reps)]
        elif t == "mem":
            out["mem"] = [mem_test(a.scale) for _ in range(a.reps)]
        elif t == "disk":
            w, r, modes = [], [], set()
            for _ in range(a.reps):
                tw, tr, mode = disk_test(a.dir, a.disk_mb)
                w.append(tw)
                r.append(tr)
                modes.add(mode)
            out["disk_write"], out["disk_read"], out["disk_mode"] = w, r, ",".join(sorted(modes))
    print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
