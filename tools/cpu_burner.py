"""Burn CPU on one core or all cores for a limited time.

    python tools/cpu_burner.py --threads 1 --seconds 60     # single-thread runaway
    python tools/cpu_burner.py --threads all --seconds 60   # all-core runaway
"""

from __future__ import annotations

import argparse
import hashlib
import multiprocessing
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from _safety import MAX_SECONDS  # noqa: E402


def burn(seconds: float) -> int:
    until = time.monotonic() + seconds
    data = b"m" * 4096
    n = 0
    while time.monotonic() < until:
        hashlib.sha256(data).digest()
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", default="1", help="'all' or a number of processes")
    ap.add_argument("--seconds", type=float, default=60.0)
    a = ap.parse_args(argv)
    seconds = min(a.seconds, MAX_SECONDS)
    procs = (os.cpu_count() or 1) if a.threads == "all" else max(1, int(a.threads))
    print(f"cpu_burner: {procs} process(es) for {seconds:.0f} s", flush=True)
    if procs == 1:
        burn(seconds)
    else:
        with multiprocessing.Pool(procs) as pool:
            pool.map(burn, [seconds] * procs)
    print("cpu_burner: done", flush=True)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
