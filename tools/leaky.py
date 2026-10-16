"""A deliberate memory leak: grows by N MB per minute and never frees, for a limited time.

    python tools/leaky.py --mb-per-min 50 --minutes 25
"""

from __future__ import annotations

import argparse
import os
import sys

import psutil

sys.path.insert(0, os.path.dirname(__file__))
from _safety import Stopper, touch  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb-per-min", type=float, default=50.0)
    ap.add_argument("--minutes", type=float, default=25.0)
    a = ap.parse_args(argv)
    cap = psutil.virtual_memory().available * 0.5
    leaked: list[bytearray] = []
    stop = Stopper(a.minutes * 60 + 5, cleanup=leaked.clear)
    per_tick = int(a.mb_per_min * 1024 ** 2 / 12)  # every 5 s
    total = 0
    print(f"leaky: +{a.mb_per_min:.0f} MB/min for {a.minutes:.0f} min", flush=True)
    while not stop.stopped and total + per_tick < cap:
        b = bytearray(per_tick)
        touch(b)
        leaked.append(b)
        total += per_tick
        if stop.wait(5.0):
            break
    stop.cleanup()
    print(f"leaky: released {total / 1024 ** 2:.0f} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
