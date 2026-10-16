"""Explain an anomaly: cause, culprit and one tip (DIA-1..14).

Rules run in a fixed, documented order and the first one that matches wins. A rule only
explains the metrics it lists, so a full disk never "explains" a CPU spike. The order
puts memory pressure before disk I/O (paging *is* disk I/O) and runaway processes before
throttling (a runaway causes the heat), which is what DIA-3 asks for.

Every number in the text comes from live data; there are no claims Mochi can't measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .detector import Signal
from .grouping import KNOWN_MAINTENANCE, VM_HOSTS
from .model import GB, MB, METRICS, App, Level, Sample, fmt_bytes, fmt_duration
from .platform.base import FileInfo, Platform

RULE_ORDER = ("memory_pressure", "runaway", "disk_io", "leak", "memory_hog", "throttling", "storage", "idle_cpu",
              "cpu_hog", "battery")

RULE_TITLES = {
    "memory_pressure": "Memory pressure",
    "runaway": "Runaway process",
    "disk_io": "Disk bottleneck",
    "leak": "Possible memory leak",
    "memory_hog": "Memory hog",
    "throttling": "Thermal throttling",
    "storage": "Disk space",
    "idle_cpu": "Busy while you're away",
    "cpu_hog": "Busy CPU",
    "battery": "Battery draining fast",
    "unknown": "Something's unusual",
}


@dataclass
class Culprit:
    app: str
    display: str
    pid: int | None
    create_time: float | None
    exe_path: str | None
    is_system: bool = False
    file: FileInfo | None = None


@dataclass
class Action:
    kind: str     # end_process | lower_priority | open_location | startup_settings | biggest_folders |
                  # show_detective | show_timeline | show_checkup
    label: str
    target: dict = field(default_factory=dict)


@dataclass
class Diagnosis:
    rule: str
    cause: str
    tip: str
    culprit: Culprit | None
    actions: list[Action]
    metric: str
    evidence: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return RULE_TITLES.get(self.rule, "Something's unusual")

    @property
    def key(self) -> tuple[str, str]:
        """One episode per culprit (its cause can sharpen as evidence arrives, e.g. a memory
        hog that turns into memory pressure); without a culprit, one per (cause, target)."""
        if self.culprit:
            return ("app", self.culprit.app)
        return (self.rule, str(self.evidence.get("target") or self.metric))

    @property
    def rank(self) -> int:
        return RULE_ORDER.index(self.rule) if self.rule in RULE_ORDER else len(RULE_ORDER)


@dataclass
class DiagContext:
    sample: Sample
    history: list[Sample]
    bands: dict[str, tuple[float, float] | None]
    cfg: Config
    platform: Platform
    activity: str
    user_idle: bool
    runaway: list[dict] = field(default_factory=list)
    leaks: dict[str, dict] = field(default_factory=dict)

    def window(self, seconds: float) -> list[Sample]:
        cutoff = self.sample.mono - seconds - 0.5
        return [s for s in self.history if s.mono >= cutoff] or [self.sample]

    def avg(self, fn: Callable[[Sample], float | None], seconds: float) -> float | None:
        vals = [v for s in self.window(seconds) if (v := fn(s)) is not None]
        return sum(vals) / len(vals) if vals else None

    def frac(self, fn: Callable[[Sample], bool | None], seconds: float) -> float:
        vals = [v for s in self.window(seconds) if (v := fn(s)) is not None]
        return sum(1 for v in vals if v) / len(vals) if vals else 0.0

    def mu(self, metric: str) -> float | None:
        b = self.bands.get(metric)
        return b[0] if b else None


def _user_apps(sample: Sample) -> list[App]:
    return [a for a in sample.apps.values() if not a.is_self and a.key != "system"]


def _avg_app(ctx: DiagContext, key: str, attr: str, seconds: float) -> float:
    vals = [getattr(s.apps[key], attr) or 0.0 for s in ctx.window(seconds) if key in s.apps]
    return sum(vals) / len(vals) if vals else 0.0


def _culprit(ctx: DiagContext, app: App) -> Culprit:
    path = ctx.platform.exe_path(app.root_pid, app.root_create_time)
    info = ctx.platform.file_info(path) if path else None
    return Culprit(app.key, app.display, app.root_pid, app.root_create_time, path, app.is_system, info)


def vm_note(app_key: str) -> str | None:
    """Work inside WSL/Docker/VMs shows up as one host process (DIA-13)."""
    base = app_key.split(":")[0]
    what = VM_HOSTS.get(base) or VM_HOSTS.get(base.removesuffix(".exe"))
    if what:
        return f"That's work running inside {what}; I can't see which program inside it is busy."
    return None


def _end_actions(c: Culprit) -> list[Action]:
    if c.is_system:
        return [Action("lower_priority", "Let it run slower", {"pid": c.pid, "create_time": c.create_time, "app": c.app})]
    return [
        Action("end_process", f"End {c.display}…", {"pid": c.pid, "create_time": c.create_time, "app": c.app,
                                                     "display": c.display}),
        Action("lower_priority", "Let it run slower", {"pid": c.pid, "create_time": c.create_time, "app": c.app}),
    ]


def _with_vm(c: Culprit | None, text: str) -> str:
    if c is not None and (note := vm_note(c.app)):
        return f"{text} {note}"
    return text


# ----------------------------------------------------------------------------- rules
def rule_memory_pressure(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    cfg = ctx.cfg
    paging = ctx.avg(lambda s: s.sys.paging_rate, 30)
    if paging is None:
        return None
    mu = ctx.mu("paging")
    threshold = max(cfg.paging_k * mu, cfg.paging_abs / 5) if mu else cfg.paging_abs
    avail = ctx.avg(lambda s: s.sys.ram_available_pct, 30) or 100.0
    commit = ctx.avg(lambda s: s.sys.commit_pct, 30)
    low_mem = avail < 10.0 or (commit is not None and commit > 90.0)
    if paging < threshold or not low_mem:
        return None
    apps = sorted(_user_apps(ctx.sample), key=lambda a: a.private_bytes, reverse=True)
    apps = [a for a in apps if not a.is_system] or apps
    usual = f"usually about {mu:.0f}" if mu else "far above normal"
    cause = f"Your PC is shuffling memory to disk ({paging:.0f} pages/s, {usual}) and only {avail:.0f}% of memory is free."
    if not apps:
        return Diagnosis("memory_pressure", cause, "Closing an app you aren't using would help.", None,
                         [Action("show_detective", "Show me")], sig.metric, {"paging": paging, "available_pct": avail})
    c = _culprit(ctx, apps[0])
    tip = _with_vm(c, f"Closing {c.display} ({fmt_bytes(apps[0].private_bytes)}) would help most.")
    return Diagnosis("memory_pressure", cause, tip, c, [Action("show_detective", "Show me")] + _end_actions(c),
                     sig.metric, {"paging": paging, "paging_mu": mu, "available_pct": avail,
                                  "culprit_bytes": apps[0].private_bytes})


def rule_runaway(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    finding = None
    if sig.kind == "proc" and sig.detail.get("rule") == "runaway":
        finding = {"app": sig.detail["target"], "cpu_core": sig.detail.get("cpu_core", sig.value or 0.0),
                   "cpu_total": sig.detail.get("cpu_total", 0.0), "minutes": sig.detail.get("minutes", 0.0)}
    elif ctx.runaway:
        finding = max(ctx.runaway, key=lambda f: f["cpu_core"])
    if finding is None:
        return None
    app = ctx.sample.apps.get(finding["app"])
    if app is None:
        return None
    c = _culprit(ctx, app)
    minutes = max(1, round(finding["minutes"]))
    total = app.cpu_total
    if total >= ctx.cfg.runaway_total_pct:
        cause = f"{c.display} has been using {total:.0f}% of your whole CPU for {minutes} minutes."
    else:
        cause = f"{c.display} has been at {min(app.cpu_core, 100 * max(1, len(app.pids))):.0f}% of a CPU core for {minutes} minutes."
    if c.is_system:
        name = KNOWN_MAINTENANCE.get(app.exe_name.lower(), "It's part of Windows")
        tip = f"{name}. It usually settles by itself; I'll keep an eye on it."
    else:
        tip = "Is it stuck? If you didn't expect it to work this hard, you can end it or let it run slower."
    tip = _with_vm(c, tip)
    return Diagnosis("runaway", cause, tip, c, _end_actions(c) + [Action("open_location", "Where is it?",
                                                                            {"path": c.exe_path})],
                     sig.metric, {"cpu_core": app.cpu_core, "cpu_total": total, "minutes": finding["minutes"]})


def rule_disk_io(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    cfg = ctx.cfg
    secs = cfg.disk_io_s

    def busy_or_slow(s: Sample) -> bool | None:
        if s.sys.disk_busy_pct is None and s.sys.disk_latency_ms is None:
            return None
        return (s.sys.disk_busy_pct or 0.0) >= cfg.disk_busy_pct or (s.sys.disk_latency_ms or 0.0) >= cfg.disk_latency_ms

    window = ctx.window(secs)
    if len(window) * cfg.sample_interval_s < secs * 0.8 or ctx.frac(busy_or_slow, secs) < cfg.sustain_frac:
        return None
    busy = ctx.avg(lambda s: s.sys.disk_busy_pct, secs) or 0.0
    lat = ctx.avg(lambda s: s.sys.disk_latency_ms, secs)
    apps = sorted(_user_apps(ctx.sample), key=lambda a: _avg_app(ctx, a.key, "io_bps", secs), reverse=True)
    minutes = max(1, round(secs / 60))
    cause = f"Your disk is the bottleneck: it's been {busy:.0f}% busy for {minutes} minutes"
    cause += f" and each request takes {lat:.0f} ms." if lat else "."
    if not apps or _avg_app(ctx, apps[0].key, "io_bps", secs) <= 0:
        return Diagnosis("disk_io", cause, "Pausing big copies or downloads for now would help.", None,
                         [Action("show_timeline", "Show me")], sig.metric, {"busy": busy, "latency_ms": lat})
    top = apps[0]
    rate = _avg_app(ctx, top.key, "io_bps", secs)
    c = _culprit(ctx, top)
    tip = _with_vm(c, f"{c.display} is probably the cause ({rate / MB:.0f} MB/s). Pausing it for now would help.")
    return Diagnosis("disk_io", cause, tip, c, [Action("show_timeline", "Show me")] + _end_actions(c), sig.metric,
                     {"busy": busy, "latency_ms": lat, "io_bps": rate})


def rule_leak(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    detail = None
    if sig.kind == "trend" and sig.detail.get("rule") == "leak":
        detail = sig.detail
    elif ctx.leaks:
        top = max(ctx.sample.apps.values(), key=lambda a: a.private_bytes, default=None)
        if top is not None and top.key in ctx.leaks:
            detail = ctx.leaks[top.key]
    if detail is None:
        return None
    app = ctx.sample.apps.get(detail["target"])
    if app is None:
        return None
    c = _culprit(ctx, app)
    span = fmt_duration(detail["span_min"] * 60)
    cause = (f"{c.display} has grown by {detail['growth_mb']:.0f} MB in {span} and never let go "
             f"(about {detail['rate_mb_h']:.0f} MB per hour).")
    tip = _with_vm(c, "Restarting it will give that memory back.")
    return Diagnosis("leak", cause, tip, c, [Action("show_detective", "Show me")] + _end_actions(c), "ram",
                     {"rate_mb_h": detail["rate_mb_h"], "growth_mb": detail["growth_mb"], "span_min": detail["span_min"]})


def rule_memory_hog(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    """Memory well above your normal, without swapping yet: name the biggest app."""
    if sig.kind not in ("z", "abs"):
        return None
    apps = sorted((a for a in _user_apps(ctx.sample) if not a.is_system), key=lambda a: a.private_bytes, reverse=True)
    if not apps:
        return None
    top = apps[0]
    used = ctx.sample.sys.ram_used / GB if ctx.sample.sys.ram_total else None
    mu = ctx.mu("ram")
    c = _culprit(ctx, top)
    if used is not None and mu:
        ratio = used / mu if mu > 0 else 0
        usual = "about twice your usual" if 1.8 <= ratio < 2.2 else f"{ratio:.1f}x your usual"
        cause = f"{c.display} is using {fmt_bytes(top.private_bytes)}; memory in use is {used:.1f} GB, {usual} ({mu:.1f} GB)."
    else:
        cause = f"{c.display} is using {fmt_bytes(top.private_bytes)} and memory is {ctx.sample.sys.ram_available_pct:.0f}% free."
    tip = _with_vm(c, f"Closing {c.display} would free the most memory.")
    return Diagnosis("memory_hog", cause, tip, c, [Action("show_detective", "Show me")] + _end_actions(c), "ram",
                     {"culprit_bytes": top.private_bytes, "used_gb": used, "mu": mu})


def rule_throttling(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    cfg = ctx.cfg
    busy = ctx.avg(lambda s: s.sys.cpu_total, 30) or 0.0
    limit = ctx.avg(lambda s: s.sys.perf_limit_pct, 15)   # recent, so the number shown is current
    perf = ctx.avg(lambda s: s.sys.cpu_perf_pct, 15)
    temp = ctx.sample.sys.temp_c
    throttled = busy >= 50.0 and ((limit is not None and limit < cfg.throttle_limit_pct) or (perf is not None and perf < 70.0))
    hot = temp is not None and temp >= cfg.tjmax_c - cfg.temp_margin_c
    if not throttled and not (sig.metric == "temp" and hot):
        return None
    if throttled:
        speed = limit if limit is not None and limit < cfg.throttle_limit_pct else perf
        cause = f"I'm overheating, so the CPU is slowing itself down (running at {speed:.0f}% of its normal speed)."
        if temp is not None:
            cause += f" It's {temp:.0f} °C."
    else:
        cause = f"My CPU is at {temp:.0f} °C, close to its limit of {cfg.tjmax_c:.0f} °C."
    tip = "Check the vents aren't blocked and use a flat, hard surface."
    return Diagnosis("throttling", cause, tip, None, [Action("show_timeline", "Show me")], sig.metric,
                     {"busy": busy, "perf_limit": limit, "perf": perf, "temp": temp})


def rule_storage(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    if sig.metric != "disk_free":
        return None
    d = sig.detail
    mount = d.get("target", "?")
    if d.get("rule") == "disk_projection":
        cause = (f"{mount} is filling up by about {d['rate_gb_day']:.1f} GB a day; at this pace it'll be full in "
                 f"{d['days_left']:.0f} days ({d['free_gb']:.0f} GB left).")
    else:
        cause = f"Only {d.get('free_gb', 0):.0f} GB left on {mount} ({d.get('free_pct', 0):.0f}% free)."
    tip = "Want me to show the biggest folders?"
    return Diagnosis("storage", cause, tip, None,
                     [Action("biggest_folders", "Show biggest folders", {"mount": mount})], "disk_free",
                     {"target": mount, **{k: v for k, v in d.items() if k != "target"}})


def rule_idle_cpu(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    if sig.kind != "z" or not (ctx.user_idle or ctx.activity == "idle"):
        return None
    apps = sorted(_user_apps(ctx.sample), key=lambda a: _avg_app(ctx, a.key, "cpu_core", 120), reverse=True)
    if not apps:
        return None
    top = apps[0]
    c = _culprit(ctx, top)
    cpu = _avg_app(ctx, top.key, "cpu_core", 120)
    exe_l = top.exe_name.lower()
    if exe_l in KNOWN_MAINTENANCE:
        cause = f"{KNOWN_MAINTENANCE[exe_l]} while you're away ({c.display}, {cpu:.0f}% of a core)."
        tip = "This is normal Windows housekeeping and it stops when you're back. Nothing to do."
        return Diagnosis("idle_cpu", cause, tip, c, [Action("show_timeline", "Show me")], sig.metric,
                         {"maintenance": True, "cpu_core": cpu})
    info = c.file
    if info is not None and info.unfamiliar:
        where = " and runs from Downloads/Temp" if info.in_temp_or_downloads else ""
        cause = (f"Something unfamiliar is busy while you're away: {c.display} ({cpu:.0f}% of a core). "
                 f"It has no publisher info{where}.")
        tip = "If you don't recognise it, I can show you where it lives."
    else:
        publisher = info.publisher if info else "unknown publisher"
        cause = f"{c.display} ({publisher}) is busy while you're away ({cpu:.0f}% of a core)."
        tip = "If you weren't expecting background work, you can check what it is."
    tip = _with_vm(c, tip)
    return Diagnosis("idle_cpu", cause, tip, c,
                     [Action("open_location", "Where is it?", {"path": c.exe_path})] + _end_actions(c), sig.metric,
                     {"maintenance": False, "cpu_core": cpu, "unfamiliar": bool(info and info.unfamiliar)})


def rule_cpu_hog(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    """CPU well above your normal before the runaway rule has 5 minutes of evidence."""
    if sig.kind not in ("z", "abs"):
        return None
    apps = sorted(_user_apps(ctx.sample), key=lambda a: _avg_app(ctx, a.key, "cpu_total", 120), reverse=True)
    if not apps:
        return None
    top = apps[0]
    share = _avg_app(ctx, top.key, "cpu_total", 120)
    total = ctx.avg(lambda s: s.sys.cpu_total, 120) or 0.0
    if share < 10.0 or (total and share < 0.4 * total):
        return None  # no single app explains it
    c = _culprit(ctx, top)
    mu = ctx.mu("cpu")
    usual = f", well above your usual {mu:.0f}% for this time" if mu is not None else ""
    cause = f"{c.display} is using {share:.0f}% of your CPU{usual}."
    tip = _with_vm(c, "If you weren't expecting that, you can let it run slower or end it.")
    return Diagnosis("cpu_hog", cause, tip, c, _end_actions(c) + [Action("show_timeline", "Show me")], "cpu",
                     {"share": share, "total": total})


def rule_battery(sig: Signal, ctx: DiagContext) -> Diagnosis | None:
    if sig.metric != "discharge" or not sig.value:
        return None
    mu = ctx.mu("discharge")
    ratio = sig.value / mu if mu else None
    apps = sorted(_user_apps(ctx.sample), key=lambda a: _avg_app(ctx, a.key, "cpu_core", 120), reverse=True)
    faster = f"{ratio:.1f}x faster than usual" if ratio else "faster than usual"
    cause = f"Battery is draining {faster} ({sig.value:.0f} W)."
    if apps:
        c = _culprit(ctx, apps[0])
        tip = f"{c.display} is the top CPU user on battery right now."
        return Diagnosis("battery", cause, tip, c, [Action("show_detective", "Show me")] + _end_actions(c),
                         sig.metric, {"watts": sig.value, "ratio": ratio})
    return Diagnosis("battery", cause, "Lowering screen brightness would help.", None, [], sig.metric, {"watts": sig.value})


Rule = Callable[[Signal, DiagContext], "Diagnosis | None"]
RULES: list[tuple[str, frozenset[str], Rule]] = [
    ("memory_pressure", frozenset({"ram", "paging", "disk", "cpu"}), rule_memory_pressure),
    ("runaway", frozenset({"cpu", "temp", "discharge"}), rule_runaway),
    ("disk_io", frozenset({"disk"}), rule_disk_io),
    ("leak", frozenset({"ram"}), rule_leak),
    ("memory_hog", frozenset({"ram"}), rule_memory_hog),
    ("throttling", frozenset({"cpu", "temp"}), rule_throttling),
    ("storage", frozenset({"disk_free"}), rule_storage),
    ("idle_cpu", frozenset({"cpu"}), rule_idle_cpu),
    ("cpu_hog", frozenset({"cpu"}), rule_cpu_hog),
    ("battery", frozenset({"discharge"}), rule_battery),
]
assert tuple(r[0] for r in RULES) == RULE_ORDER


def fallback(sig: Signal, ctx: DiagContext) -> Diagnosis:
    info = METRICS.get(sig.metric)
    label = info.label if info else sig.metric
    attr = "private_bytes" if sig.metric in ("ram", "paging") else ("io_bps" if sig.metric == "disk" else "cpu_core")
    apps = sorted(_user_apps(ctx.sample), key=lambda a: getattr(a, attr) or 0, reverse=True)[:3]

    def fmt(a: App) -> str:
        if attr == "private_bytes":
            return f"{a.display}: {fmt_bytes(a.private_bytes)}"
        if attr == "io_bps":
            return f"{a.display}: {a.io_bps / MB:.0f} MB/s"
        return f"{a.display}: {a.cpu_core:.0f}% of a core"

    unit = info.unit if info else ""
    value = f"{sig.value:.1f} {unit}".strip() if sig.value is not None else "high"
    usual = f" vs about {sig.mu:.1f} {unit} usually" if sig.mu is not None else ""
    cause = f"Your {label.lower()} is higher than usual ({value}{usual})."
    top = ", ".join(fmt(a) for a in apps) if apps else "nothing stands out"
    tip = f"I can't pin down the cause yet. The top users right now are {top}."
    return Diagnosis("unknown", cause, tip, None, [Action("show_timeline", "Show me")], sig.metric,
                     {"value": sig.value, "mu": sig.mu})


class DiagnosisEngine:
    def diagnose(self, sig: Signal, ctx: DiagContext) -> Diagnosis:
        for name, explains, rule in RULES:
            if sig.metric not in explains:
                continue
            if name == "leak" and sig.kind not in ("trend", "z"):
                continue
            d = rule(sig, ctx)
            if d is not None:
                return d
        return fallback(sig, ctx)
