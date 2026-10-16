"""The guided PC check (CHK-1..6) [stretch].

Each item has documented thresholds for fine / worth a look / needs action, plus an
"unavailable (why)" state. The whole check is kept under ~90 s.

Thresholds:
- Disk space (per fixed drive): fine >= 20% or >= 50 GB free; worth a look 10-20%;
  needs action < 10% and < 20 GB.
- Disk health: Windows Storage health status (no admin needed). Healthy = fine,
  Warning = worth a look, Unhealthy = needs action. SMART via smartctl is used when it is
  installed and Mochi runs as admin.
- Startup programs: fine <= 8 enabled, worth a look 9-15, needs action > 15. Per-app boot
  delay is shown only when measured.
- CPU under load (<= 20 s): fine if no throttling (performance limit >= 95%) and not
  hot; worth a look if the limit dips to 80-95% or temperature is within 15 °C of the
  limit; needs action below 80% or within 5 °C. Stops early if it gets too hot.
- Background apps while idle (last 7 days): fine if no app averages >= 10% of a core
  while you're away; worth a look otherwise.
- Growing memory (leak findings): fine if none; worth a look if any.
- Battery (laptops): fine >= 80% of design capacity, worth a look 60-80%, needs action
  < 60%. Hidden on desktops.
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .config import Config
from .model import GB, Volume
from .platform.base import Platform

SEVERITY = {"action": 2, "look": 1, "fine": 0, "unavailable": -1}
STATUS_LABEL = {"fine": "Fine", "look": "Worth a look", "action": "Needs action", "unavailable": "Unavailable"}


@dataclass
class CheckItem:
    key: str
    title: str
    status: str
    detail: str
    suggestion: str = ""
    data: dict = field(default_factory=dict)

    @property
    def severity(self) -> int:
        return SEVERITY[self.status]


@dataclass
class CheckupReport:
    ts: float
    items: list[CheckItem]
    score: float | None
    top3: list[str]
    seconds: float
    benchmark: dict | None = None

    def to_json(self) -> dict:
        return {"ts": self.ts, "items": [asdict(i) for i in self.items], "score": self.score, "top3": self.top3,
                "seconds": self.seconds, "benchmark": self.benchmark}


def check_disk_space(volumes: list[Volume], biggest: list[tuple[str, int]] | None = None) -> CheckItem:
    vols = [v for v in volumes if v.fixed and v.total]
    if not vols:
        return CheckItem("disk_space", "Free disk space", "unavailable", "No fixed drives were reported.")
    worst = "fine"
    parts = []
    for v in vols:
        if v.free_pct < 10 and v.free_gb < 20:
            s = "action"
        elif v.free_pct < 20 and v.free_gb < 50:
            s = "look"
        else:
            s = "fine"
        if SEVERITY[s] > SEVERITY[worst]:
            worst = s
        parts.append(f"{v.mount} {v.free_gb:.0f} GB free ({v.free_pct:.0f}%)")
    detail = ", ".join(parts)
    if biggest:
        detail += ". Biggest folders: " + ", ".join(f"{p} ({b / GB:.1f} GB)" for p, b in biggest[:3])
    tip = {"action": "Free some space: empty the Recycle Bin and clear out the biggest folders.",
           "look": "Space is getting tight. Have a look at the biggest folders.", "fine": ""}[worst]
    return CheckItem("disk_space", "Free disk space", worst, detail, tip, {"volumes": parts})


def check_disk_health(platform: Platform, smartctl: str | None = None) -> CheckItem:
    disks, reason = platform.disk_health()
    if smartctl is None:
        smartctl = shutil.which("smartctl")
    smart_note = "" if smartctl and platform.is_admin() else " SMART details need smartctl and admin."
    if not disks:
        return CheckItem("disk_health", "Disk health", "unavailable", (reason or "Not reported.") + smart_note)
    worst = "fine"
    for d in disks:
        s = {"healthy": "fine", "warning": "look", "unhealthy": "action"}.get(d.status, "look")
        if SEVERITY[s] > SEVERITY[worst]:
            worst = s
    detail = ", ".join(f"{d.name} ({d.media}): {d.status}" for d in disks) + "." + smart_note
    tip = {"action": "Back up your files now; a drive reports a problem.",
           "look": "A drive's health is uncertain. Keep backups current.", "fine": ""}[worst]
    return CheckItem("disk_health", "Disk health", worst, detail, tip)


def check_startup(platform: Platform, measured_delay: dict[str, float] | None = None) -> CheckItem:
    try:
        items = platform.startup_items()
    except Exception as exc:
        return CheckItem("startup", "Programs that start with Windows", "unavailable", f"Couldn't list them ({exc}).")
    enabled = [i for i in items if i.enabled is not False]
    n = len(enabled)
    status = "fine" if n <= 8 else ("look" if n <= 15 else "action")
    names = ", ".join(sorted({i.name for i in enabled})[:8])
    detail = f"{n} programs start when you sign in" + (f": {names}" if names else "") + "."
    if measured_delay:
        detail += " Measured delay: " + ", ".join(f"{k} {v:.1f} s" for k, v in measured_delay.items())
    else:
        detail += " How much each delays sign-in isn't measured yet."
    tip = "" if status == "fine" else "Turning off ones you don't need makes sign-in faster (Settings > Apps > Startup)."
    return CheckItem("startup", "Programs that start with Windows", status, detail, tip,
                     {"count": n, "items": [i.name for i in enabled]})


def judge_load(perf_limits: list[float], temps: list[float], tjmax: float) -> tuple[str, str]:
    if not perf_limits and not temps:
        return "unavailable", "Windows didn't report CPU speed limits or temperature during the test."
    low = min(perf_limits) if perf_limits else 100.0
    hot = max(temps) if temps else None
    status = "fine"
    if low < 80 or (hot is not None and hot >= tjmax - 5):
        status = "action"
    elif low < 95 or (hot is not None and hot >= tjmax - 15):
        status = "look"
    detail = f"Lowest CPU speed limit under load: {low:.0f}%."
    detail += f" Hottest: {hot:.0f} °C." if hot is not None else " Temperature isn't available (needs admin)."
    return status, detail


def check_idle_background(rows: list[tuple[str, float]], threshold: float = 10.0) -> CheckItem:
    """rows: (app, average % of a core while you were away) over the last 7 days."""
    busy = [(a, c) for a, c in rows if c >= threshold]
    if not rows:
        return CheckItem("idle_background", "Background apps while you're away", "unavailable",
                         "Not enough time away from the PC recorded yet.")
    if not busy:
        return CheckItem("idle_background", "Background apps while you're away", "fine",
                         "Nothing unusual keeps working while you're away.")
    detail = ", ".join(f"{a} ({c:.0f}% of a core)" for a, c in busy[:5])
    return CheckItem("idle_background", "Background apps while you're away", "look",
                     f"These stay busy while you're away: {detail}.", "Check whether you need them running.")


def check_leaks(findings: list) -> CheckItem:
    if not findings:
        return CheckItem("leaks", "Programs with growing memory", "fine", "No program's memory keeps growing.")
    detail = ", ".join(f"{f.display} (+{f.rate_mb_h:.0f} MB/h)" for f in findings[:5])
    return CheckItem("leaks", "Programs with growing memory", "look", f"Growing steadily: {detail}.",
                     "Restarting them gives the memory back.")


def check_battery(platform: Platform, has_battery: bool) -> CheckItem | None:
    if not has_battery:
        return None  # hidden on desktops (CHK-6)
    info = platform.battery_health()
    if info is None or info.health_pct is None:
        return CheckItem("battery", "Battery health", "unavailable", "Windows didn't report the battery's capacity.")
    pct = info.health_pct
    status = "fine" if pct >= 80 else ("look" if pct >= 60 else "action")
    detail = f"Holds {pct:.0f}% of its original charge"
    detail += f" after {info.cycles} charge cycles." if info.cycles else "."
    tip = {"fine": "", "look": "The battery is wearing; avoid keeping it at 100% all day.",
           "action": "The battery is quite worn. A replacement would bring back battery life."}[status]
    return CheckItem("battery", "Battery health", status, detail, tip, {"health_pct": pct, "cycles": info.cycles})


def top_suggestions(items: list[CheckItem], n: int = 3) -> list[str]:
    ranked = sorted((i for i in items if i.status in ("action", "look") and i.suggestion),
                    key=lambda i: (-i.severity, items.index(i)))
    return [i.suggestion for i in ranked[:n]]


class CheckupRunner:
    """Runs the checklist. Heavy parts (benchmarks, load test, folder scan) are injected."""

    def __init__(self, cfg: Config, platform: Platform,
                 volumes: Callable[[], list[Volume]],
                 biggest: Callable[[], list[tuple[str, int]] | None],
                 idle_rows: Callable[[], list[tuple[str, float]]],
                 leak_findings: Callable[[], list],
                 has_battery: Callable[[], bool],
                 run_benchmark: Callable[[], tuple[dict | None, float | None]],
                 load_test: Callable[[threading.Event], tuple[list[float], list[float]]]) -> None:
        self.cfg = cfg
        self.platform = platform
        self.volumes = volumes
        self.biggest = biggest
        self.idle_rows = idle_rows
        self.leak_findings = leak_findings
        self.has_battery = has_battery
        self.run_benchmark = run_benchmark
        self.load_test = load_test

    def run(self, progress: Callable[[str, float], None] | None = None,
            cancel: threading.Event | None = None) -> CheckupReport:
        t0 = time.monotonic()
        cancel = cancel or threading.Event()
        steps: list[tuple[str, Callable[[], CheckItem | None]]] = [
            ("Checking free space", lambda: check_disk_space(self.volumes(), self.biggest())),
            ("Checking disk health", lambda: check_disk_health(self.platform)),
            ("Listing startup programs", lambda: check_startup(self.platform)),
            ("Testing CPU under a short load", self._load_item),
            ("Looking at background apps", lambda: check_idle_background(self.idle_rows())),
            ("Looking for growing memory", lambda: check_leaks(self.leak_findings())),
            ("Checking the battery", lambda: check_battery(self.platform, self.has_battery())),
        ]
        items: list[CheckItem] = []
        bench = None
        score = None
        for i, (label, fn) in enumerate(steps):
            if cancel.is_set():
                break
            if progress:
                progress(label, i / (len(steps) + 1))
            try:
                item = fn()
            except Exception as exc:
                item = CheckItem(label, label, "unavailable", f"Something went wrong ({exc.__class__.__name__}).")
            if item is not None:
                items.append(item)
        if not cancel.is_set():
            if progress:
                progress("Running the speed test", len(steps) / (len(steps) + 1))
            bench, score = self.run_benchmark()
            if score is None:
                items.append(CheckItem("health", "Health Score", "unavailable",
                                       "Still learning: I need a few speed tests before I can compare."))
            else:
                status = "fine" if score >= 90 else ("look" if score >= self.cfg.health_suggest_below else "action")
                items.append(CheckItem("health", "Health Score", status, f"{score:.0f} out of 100 compared with this PC's own early runs.",
                                       "" if status == "fine" else "Your PC is slower than it used to be; the items above show where to start."))
        if progress:
            progress("Done", 1.0)
        return CheckupReport(time.time(), items, score, top_suggestions(items), time.monotonic() - t0, bench)

    def _load_item(self) -> CheckItem:
        stop = threading.Event()
        limits, temps = self.load_test(stop)
        status, detail = judge_load(limits, temps, self.cfg.tjmax_c)
        tip = {"action": "Your CPU slows itself down under load. Clean the vents and use a hard, flat surface.",
               "look": "Your CPU gets close to its limits under load. Keep the vents clear.", "fine": "",
               "unavailable": ""}[status]
        return CheckItem("cpu_load", "CPU temperature and throttling under load", status, detail, tip,
                         {"min_limit": min(limits) if limits else None, "max_temp": max(temps) if temps else None})
