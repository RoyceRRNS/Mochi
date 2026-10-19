"""App startup timing (STR-1..5) [stretch].

- A fast watcher (every 0.5 s) spots new app launches; 5-second sampling can't tell a
  2.1 s launch from a 7.8 s one.
- "Ready" means the app's processes stay under 5% of a core for 2 s. The reported time
  ends where that quiet stretch began. No settle within 60 s -> discarded.
- The first launch of an app since boot is "cold" (disk caches empty); cold launches are
  compared only with other cold launches.
- Relaunching an app that is already open (it hands over to the running copy and exits)
  isn't counted.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import psutil

from .grouping import HELPERS, INTERPRETERS, PSEUDO_NAMES, is_system_name, script_from_cmdline

log = logging.getLogger(__name__)


@dataclass
class Launch:
    app: str
    display: str
    ts: float
    duration_s: float | None
    cold: bool
    settled: bool
    reason: str = ""


@dataclass
class _Watch:
    app: str
    display: str
    create_time: float
    cold: bool
    existing_instance: bool
    quiet_since: float | None = None
    done: bool = False


class LaunchAnalyzer:
    """Pure state machine fed with events; the thread wrapper feeds it from psutil."""

    def __init__(self, settle_pct: float = 5.0, settle_s: float = 2.0, timeout_s: float = 60.0,
                 quick_exit_s: float = 3.0) -> None:
        self.settle_pct = settle_pct
        self.settle_s = settle_s
        self.timeout_s = timeout_s
        self.quick_exit_s = quick_exit_s
        self.watching: dict[tuple[int, float], _Watch] = {}

    def on_new(self, key: tuple[int, float], app: str, display: str, create_time: float, cold: bool,
               existing_instance: bool) -> None:
        self.watching[key] = _Watch(app, display, create_time, cold, existing_instance)

    def on_cpu(self, key: tuple[int, float], tree_cpu_core: float, now: float) -> Launch | None:
        w = self.watching.get(key)
        if w is None or w.done:
            return None
        if now - w.create_time > self.timeout_s:
            self.watching.pop(key, None)
            return Launch(w.app, w.display, w.create_time, None, w.cold, False, "didn't settle within 60 s")
        if tree_cpu_core < self.settle_pct:
            if w.quiet_since is None:
                w.quiet_since = now
            if now - w.quiet_since >= self.settle_s:
                self.watching.pop(key, None)
                return Launch(w.app, w.display, w.create_time, max(0.0, w.quiet_since - w.create_time), w.cold, True)
        else:
            w.quiet_since = None
        return None

    def on_exit(self, key: tuple[int, float], now: float) -> Launch | None:
        w = self.watching.pop(key, None)
        if w is None:
            return None
        if w.existing_instance and now - w.create_time <= self.quick_exit_s:
            return Launch(w.app, w.display, w.create_time, None, w.cold, False, "handed over to the open copy")
        return Launch(w.app, w.display, w.create_time, None, w.cold, False, "exited before it was ready")


def slow_apps(launches: list[Launch], now: float, min_prior: int = 10, recent_n: int = 5,
              factor: float = 1.5, window_days: float = 30.0) -> list[dict]:
    """Apps whose last 5 launches are > 1.5x their 30-day median (per cold/warm kind)."""
    out = []
    by: dict[tuple[str, bool], list[Launch]] = {}
    for la in launches:
        if la.settled and la.duration_s is not None and now - la.ts <= window_days * 86400:
            by.setdefault((la.app, la.cold), []).append(la)
    for (app, cold), items in by.items():
        items.sort(key=lambda la: la.ts)
        if len(items) < min_prior + recent_n:
            continue
        recent = items[-recent_n:]
        prior = items[:-recent_n]
        rm = statistics.median(la.duration_s for la in recent)
        pm = statistics.median(la.duration_s for la in prior)
        if pm > 0 and rm > factor * pm:
            out.append({"app": app, "display": recent[-1].display, "cold": cold, "recent_median": rm,
                        "usual_median": pm, "ratio": rm / pm})
    return out


def several_apps_slow(slow: list[dict], at_least: int = 3) -> bool:
    return len({s["app"] for s in slow}) >= at_least


class StartupWatcher:
    """Background thread that feeds LaunchAnalyzer from the real process list."""

    def __init__(self, on_launch: Callable[[Launch], None], self_pid: int, boot_time: float,
                 known_cold: Callable[[str, float], bool], settle_pct: float = 5.0,
                 poll_s: float = 0.5, cpu_poll_s: float = 0.25) -> None:
        self.on_launch = on_launch
        self.self_pid = self_pid
        self.boot_time = boot_time
        self.known_cold = known_cold        # (app, boot_time) -> already launched since boot?
        self.analyzer = LaunchAnalyzer(settle_pct=settle_pct)
        self.poll_s = poll_s
        self.cpu_poll_s = cpu_poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._procs: dict[tuple[int, float], psutil.Process] = {}
        self._poll_cpu: dict[tuple[int, float], float] = {}
        self.errors = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mochi-startup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _app_for(self, p: psutil.Process, name: str) -> tuple[str, str]:
        name_l = name.lower()
        if name_l in INTERPRETERS:
            try:
                script = script_from_cmdline(p.cmdline())
            except psutil.Error:
                script = None
            if script:
                return f"{name_l}:{script.lower()}", f"{name} ({script})"
        return name_l, name

    def _tree_cpu(self, root: psutil.Process) -> float:
        total = 0.0
        procs = [root]
        try:
            procs += root.children(recursive=True)
        except psutil.Error:
            pass
        for p in procs:
            try:
                key = (p.pid, round(p.create_time(), 3)) if p.is_running() else None
            except psutil.Error:
                continue
            if key is None:
                continue
            # Read each process once per poll: two launches can share a child (cmd.exe starting
            # python.exe), and a second cpu_percent() microseconds later would read ~0%.
            if key in self._poll_cpu:
                total += self._poll_cpu[key]
                continue
            cached = self._procs.get(key)
            try:
                if cached is None:
                    # psutil's first reading of a new process is always 0%; count a brand-new
                    # child as busy so a launch never looks settled too early.
                    self._procs[key] = p
                    p.cpu_percent(None)
                    value = 100.0
                else:
                    value = cached.cpu_percent(None)
            except psutil.Error:
                continue
            self._poll_cpu[key] = value
            total += value
        return total

    def _run(self) -> None:
        known = set(psutil.pids())
        last_cpu = 0.0
        while not self._stop.is_set():
            try:
                now = time.time()
                pids = set(psutil.pids())
                for pid in pids - known:
                    try:
                        p = psutil.Process(pid)
                        name = p.name()
                        ct = p.create_time()
                        if (pid == self.self_pid or name.lower() in PSEUDO_NAMES or name.lower() in HELPERS
                                or is_system_name(name)):
                            continue
                        if now - ct > 2.0:
                            continue
                        parent = p.parent()
                        if parent is not None and parent.name().lower() == name.lower():
                            continue  # a helper of an app that is already running
                        if parent is not None and parent.pid == self.self_pid:
                            continue  # Mochi's own benchmark processes
                        app, display = self._app_for(p, name)
                        existing = any(
                            q.info["name"] and q.info["name"].lower() == name.lower() and q.info["pid"] != pid
                            for q in psutil.process_iter(["pid", "name"])
                        )
                        cold = not self.known_cold(app, self.boot_time)
                        key = (pid, round(ct, 3))
                        self._procs[key] = p
                        p.cpu_percent(None)
                        self.analyzer.on_new(key, app, display, ct, cold, existing)
                    except psutil.Error:
                        continue
                known = pids
                if time.monotonic() - last_cpu >= self.cpu_poll_s:
                    last_cpu = time.monotonic()
                    self._poll_cpu = {}
                    for key in list(self.analyzer.watching):
                        p = self._procs.get(key)
                        result = None
                        if p is None or not p.is_running():
                            result = self.analyzer.on_exit(key, now)
                        else:
                            result = self.analyzer.on_cpu(key, self._tree_cpu(p), now)
                        if result is not None:
                            self.on_launch(result)
                    live = {k for k in self._procs if k[0] in pids}
                    self._procs = {k: v for k, v in self._procs.items() if k in live}
            except Exception:  # keep watching (LIF-7)
                self.errors += 1
                log.exception("startup watcher iteration failed")
            self._stop.wait(min(self.poll_s, self.cpu_poll_s))
