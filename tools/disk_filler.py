"""Fill a drive until free space drops below a target, hold, then delete everything.

    python tools/disk_filler.py --until-free-pct 9 --max-gb 50 --hold-s 120

Safety: never writes more than --max-gb, always leaves at least 2 GB free, and deletes
its folder on exit, Ctrl+C or timeout.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
from _safety import Stopper  # noqa: E402

CHUNK = 64 * 1024 * 1024


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until-free-pct", type=float, default=9.0)
    ap.add_argument("--max-gb", type=float, default=50.0)
    ap.add_argument("--hold-s", type=float, default=120.0)
    ap.add_argument("--dir", default=None)
    a = ap.parse_args(argv)
    folder = tempfile.mkdtemp(prefix="mochi_fill_", dir=a.dir)
    usage = shutil.disk_usage(folder)
    target_free = usage.total * a.until_free_pct / 100.0
    need = usage.free - target_free
    if need <= 0:
        print("disk_filler: already below the target", flush=True)
    if need > a.max_gb * 1024 ** 3 or usage.free - need < 2 * 1024 ** 3:
        print(f"disk_filler: would need {need / 1024 ** 3:.0f} GB (limit {a.max_gb:.0f} GB, keeps 2 GB free); refusing",
              file=sys.stderr)
        shutil.rmtree(folder, ignore_errors=True)
        return 2
    stop = Stopper(a.hold_s + 600, cleanup=lambda: shutil.rmtree(folder, ignore_errors=True))
    written = 0
    i = 0
    block = b"\0" * CHUNK
    while written < need and not stop.stopped:
        with open(os.path.join(folder, f"fill_{i:05d}.bin"), "wb") as fh:
            fh.write(block)
        written += CHUNK
        i += 1
    print(f"disk_filler: wrote {written / 1024 ** 3:.1f} GB; holding {a.hold_s:.0f} s", flush=True)
    stop.wait(a.hold_s)
    stop.cleanup()
    print("disk_filler: deleted", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
