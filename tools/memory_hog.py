"""Allocate memory step by step, hold it, then free it. Capped and auto-stopping.

    python tools/memory_hog.py --gb 2 --ramp-s 20 --hold-s 60

Safety: never allocates more than 90% of the memory that is currently available unless
--allow-swap is given, and then never more than 110% of physical RAM.
"""

from __future__ import annotations

import argparse
import os
import sys

import psutil

sys.path.insert(0, os.path.dirname(__file__))
from _safety import Stopper, touch  # noqa: E402

CHUNK_MB = 64


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gb", type=float, default=2.0)
    ap.add_argument("--ramp-s", type=float, default=20.0)
    ap.add_argument("--hold-s", type=float, default=60.0)
    ap.add_argument("--allow-swap", action="store_true", help="allow pushing the system into paging")
    a = ap.parse_args(argv)
    vm = psutil.virtual_memory()
    limit = vm.total * 1.10 if a.allow_swap else vm.available * 0.90
    target = min(a.gb * 1024 ** 3, limit)
    chunks: list[bytearray] = []
    stop = Stopper(a.ramp_s + a.hold_s + 5, cleanup=chunks.clear)
    n = max(1, int(target // (CHUNK_MB * 1024 ** 2)))
    step = a.ramp_s / n if n else 0
    print(f"memory_hog: allocating {n * CHUNK_MB / 1024:.1f} GB over {a.ramp_s:.0f} s, holding {a.hold_s:.0f} s", flush=True)
    for _ in range(n):
        if stop.stopped:
            break
        b = bytearray(CHUNK_MB * 1024 ** 2)
        touch(b)
        chunks.append(b)
        if stop.wait(step):
            break
    print(f"memory_hog: holding {len(chunks) * CHUNK_MB / 1024:.1f} GB", flush=True)
    stop.wait(a.hold_s)
    stop.cleanup()
    print("memory_hog: freed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
