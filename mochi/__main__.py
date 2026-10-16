"""Command line entry point.

    python -m mochi                     # tray app with the dashboard
    python -m mochi --profile dev       # separate data folder for development (STO-9)
    python -m mochi --start-hidden      # start in the tray (used by autostart)
    python -m mochi --headless          # collector + engine only, no UI (overhead tests)
    python -m mochi --demo              # the clearly labelled demo profile (DEMO-2)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import config as config_mod
from .instance import AlreadyRunning, InstanceLock
from .logging_setup import setup as setup_logging
from .paths import Paths

log = logging.getLogger("mochi")


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="mochi", description="Mochi - a baseline-aware PC health buddy")
    ap.add_argument("--profile", default=None, help="data profile name (default: 'default')")
    ap.add_argument("--start-hidden", action="store_true", help="start in the system tray")
    ap.add_argument("--headless", action="store_true", help="run without a user interface")
    ap.add_argument("--seconds", type=float, default=0.0, help="headless: stop after this many seconds")
    ap.add_argument("--demo", action="store_true", help="turn on the demo profile (shorter waits)")
    ap.add_argument("--simulate", action="store_true", help="UI with simulated data (no real readings)")
    return ap.parse_args(argv)


def run_headless(paths: Paths, seconds: float, profile: str) -> int:
    from .engine import Engine
    from .platform import get_platform

    engine = Engine(get_platform(), paths, profile=profile)
    stop = {"flag": False}

    def handler(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)
    t0 = time.monotonic()
    try:
        while not stop["flag"] and (seconds <= 0 or time.monotonic() - t0 < seconds):
            time.sleep(0.2)
    finally:
        engine.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    try:
        paths = Paths.for_profile(args.profile).ensure()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    profile = paths.root.name
    setup_logging(paths.logs)
    if args.demo:
        cfg = config_mod.load(paths.config).config
        cfg.demo_mode = True
        config_mod.save(cfg, paths.config)
    lock = InstanceLock(paths.lock)
    try:
        lock.acquire()
    except AlreadyRunning:
        if not args.headless:
            from .ui.app import signal_running_instance

            if signal_running_instance(profile):
                return 0
        print(f"Mochi is already running for profile '{profile}'.", file=sys.stderr)
        return 3
    try:
        if args.headless:
            return run_headless(paths, args.seconds, profile)
        from .ui.app import run_app

        return run_app(paths, profile, start_hidden=args.start_hidden, simulate=args.simulate)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
