"""Keep the disk busy with a large continuous write + read loop, then clean up.

    python tools/disk_thrash.py --mb 1024 --seconds 90
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _safety import Stopper  # noqa: E402

CHUNK = 4 * 1024 * 1024


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=1024)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--dir", default=None)
    a = ap.parse_args(argv)
    folder = tempfile.mkdtemp(prefix="mochi_thrash_", dir=a.dir)
    free = shutil.disk_usage(folder).free
    size = min(a.mb * 1024 * 1024, max(0, free - 5 * 1024 ** 3))
    if size < CHUNK:
        print("disk_thrash: not enough free space (keeps 5 GB spare)", file=sys.stderr)
        shutil.rmtree(folder, ignore_errors=True)
        return 2
    path = os.path.join(folder, "thrash.bin")
    stop = Stopper(a.seconds, cleanup=lambda: shutil.rmtree(folder, ignore_errors=True))
    block = os.urandom(CHUNK)
    rounds = 0
    print(f"disk_thrash: {size // 1024 ** 2} MB loops for {a.seconds:.0f} s in {folder}", flush=True)
    while not stop.stopped:
        with open(path, "wb", buffering=0) as fh:
            written = 0
            while written < size and not stop.stopped:
                fh.write(block)
                written += CHUNK
            os.fsync(fh.fileno())
        with open(path, "rb", buffering=0) as fh:
            while fh.read(CHUNK) and not stop.stopped:
                pass
        rounds += 1
    stop.cleanup()
    print(f"disk_thrash: done after {rounds} rounds, cleaned up", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
