"""Plain data types shared by every layer. No OS or Qt imports here."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

GB = 1024 ** 3
MB = 1024 ** 2


class Level(IntEnum):
    NONE = 0
    PSST = 1
    HEADS_UP = 2
    UH_OH = 3

    @property
    def title(self) -> str:
        return LEVEL_TITLES.get(self, "")

    @property
    def slug(self) -> str:
        return LEVEL_SLUGS[self]


LEVEL_TITLES = {Level.PSST: "Psst...", Level.HEADS_UP: "Heads up!", Level.UH_OH: "Uh-oh."}
LEVEL_SLUGS = {Level.NONE: "none", Level.PSST: "psst", Level.HEADS_UP: "heads_up", Level.UH_OH: "uh_oh"}

ACTIVITIES = ("idle", "browsing", "coding", "gaming", "media", "office", "other")

# Metrics that get contextual z-score baselines and can raise alerts.
Z_METRICS = ("cpu", "ram", "paging", "disk", "temp", "discharge")
# Metrics that learn a "your normal" band for the timeline but never raise alerts on their own.
WATCH_METRICS = ("gpu", "gpu_temp", "fan")
BASELINE_METRICS = Z_METRICS + WATCH_METRICS


@dataclass(frozen=True, slots=True)
class MetricInfo:
    key: str
    label: str
    unit: str
    min_std: float      # floor for sigma, in metric units (DET-4)
    min_delta: float    # minimum absolute rise above the mean before it can count (DET-4)
    one_sided: str = "high"  # only "higher than normal" is an anomaly (DET-5)


METRICS: dict[str, MetricInfo] = {
    "cpu": MetricInfo("cpu", "CPU", "%", min_std=3.0, min_delta=15.0),
    "ram": MetricInfo("ram", "Memory in use", "GB", min_std=0.25, min_delta=1.0),
    "paging": MetricInfo("paging", "Paging (hard faults)", "pages/s", min_std=20.0, min_delta=200.0),
    "disk": MetricInfo("disk", "Disk busy", "%", min_std=5.0, min_delta=25.0),
    "temp": MetricInfo("temp", "CPU temperature", "°C", min_std=2.0, min_delta=8.0),
    "discharge": MetricInfo("discharge", "Battery drain", "W", min_std=1.0, min_delta=5.0),
    "gpu": MetricInfo("gpu", "GPU busy", "%", min_std=3.0, min_delta=15.0),
    "gpu_temp": MetricInfo("gpu_temp", "GPU temperature", "°C", min_std=2.0, min_delta=8.0),
    "fan": MetricInfo("fan", "Fan speed", "RPM", min_std=150.0, min_delta=500.0),
}


@dataclass(slots=True)
class ProcRaw:
    """One process as reported by the platform. Counters are cumulative."""

    pid: int
    ppid: int
    create_time: float          # epoch seconds
    name: str                   # image name, e.g. "chrome.exe"
    cpu_time: float             # user + kernel seconds, cumulative
    private_bytes: int | None   # committed private memory
    ws_private: int | None      # private working set (memory actually in RAM)
    io_bytes: int | None        # cumulative read + write transfer bytes
    hard_faults: int | None     # cumulative hard page faults
    threads: int = 0


@dataclass(slots=True)
class Proc:
    """A process with per-sample rates derived from two readings."""

    pid: int
    ppid: int
    create_time: float
    name: str
    cpu_core: float             # % of one core (can exceed 100 for multi-threaded work)
    cpu_total: float            # % of the whole machine
    private_bytes: int | None
    ws_private: int | None
    io_bps: float | None
    hard_faults_ps: float | None
    threads: int = 0
    app: str = ""

    @property
    def ident(self) -> tuple[int, float]:
        return (self.pid, round(self.create_time, 3))


@dataclass(slots=True)
class App:
    """Processes grouped into the app a person would recognise (COL-5)."""

    key: str
    display: str
    exe_name: str
    pids: list[int]
    root_pid: int
    root_create_time: float
    cpu_core: float = 0.0
    cpu_total: float = 0.0
    private_bytes: int = 0
    ws_private: int | None = 0
    io_bps: float = 0.0
    hard_faults_ps: float = 0.0
    proc_count: int = 0
    is_system: bool = False
    is_self: bool = False


@dataclass(slots=True)
class Volume:
    mount: str
    total: int
    free: int
    fixed: bool = True

    @property
    def free_pct(self) -> float:
        return 100.0 * self.free / self.total if self.total else 0.0

    @property
    def free_gb(self) -> float:
        return self.free / GB


@dataclass(slots=True)
class Battery:
    percent: float
    on_ac: bool
    discharge_w: float | None = None
    present: bool = True


@dataclass(slots=True)
class Fan:
    label: str
    rpm: float
    max_rpm: float | None = None

    @property
    def pct(self) -> float | None:
        return min(100.0, 100.0 * self.rpm / self.max_rpm) if self.max_rpm else None


@dataclass(slots=True)
class SystemRaw:
    cpu_total: float | None = None
    cpu_per_core: list[float] = field(default_factory=list)
    ram_total: int = 0
    ram_available: int = 0
    commit_pct: float | None = None
    paging_rate: float | None = None        # hard page reads per second
    disk_busy_pct: float | None = None
    disk_latency_ms: float | None = None
    disk_queue: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None
    cpu_perf_pct: float | None = None       # % of nominal frequency (turbo > 100)
    perf_limit_pct: float | None = None     # 100 = not limited; lower = throttled
    run_queue: float | None = None
    temp_c: float | None = None             # CPU temperature
    gpu_busy_pct: float | None = None       # busiest GPU engine, like Task Manager
    gpu_temp_c: float | None = None
    gpu_mem_used: int | None = None         # dedicated GPU memory in use, bytes
    gpu_mem_total: int | None = None
    gpu_power_w: float | None = None
    gpu_name: str | None = None
    fans: list[Fan] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)   # reading -> where it came from
    battery: Battery | None = None
    ctx_switches: int | None = None
    interrupts: int | None = None
    syscalls: int | None = None
    idle_seconds: float = 0.0
    foreground_exe: str | None = None
    foreground_path: str | None = None      # kept in memory only, never stored (SEC-3)
    foreground_pid: int | None = None
    fullscreen: bool = False
    fullscreen_d3d: bool = False
    dnd: bool = False
    locked: bool = False
    volumes: list[Volume] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)  # metric -> reason (COL-8)

    @property
    def ram_used(self) -> int:
        return max(0, self.ram_total - self.ram_available)

    @property
    def ram_available_pct(self) -> float:
        return 100.0 * self.ram_available / self.ram_total if self.ram_total else 100.0


@dataclass(slots=True)
class Sample:
    ts: float
    mono: float
    sys: SystemRaw
    procs: list[Proc]
    apps: dict[str, App]
    late_ms: float = 0.0
    activity: str = "other"
    raw_activity: str = "other"
    slot: int = 0
    metrics: dict[str, float | None] = field(default_factory=dict)

    def top_procs(self, n: int = 15) -> list[Proc]:
        """Union of the top n by CPU and the top n by memory (COL-2)."""
        by_cpu = sorted(self.procs, key=lambda p: p.cpu_core, reverse=True)[:n]
        by_mem = sorted(self.procs, key=lambda p: p.private_bytes or 0, reverse=True)[:n]
        seen: dict[tuple[int, float], Proc] = {}
        for p in by_cpu + by_mem:
            seen.setdefault(p.ident, p)
        return list(seen.values())


def derive_metrics(sys: SystemRaw) -> dict[str, float | None]:
    discharge = None
    if sys.battery is not None and sys.battery.present and not sys.battery.on_ac:
        discharge = sys.battery.discharge_w
    return {
        "cpu": sys.cpu_total,
        "ram": sys.ram_used / GB if sys.ram_total else None,
        "paging": sys.paging_rate,
        "disk": sys.disk_busy_pct,
        "temp": sys.temp_c,
        "discharge": discharge,
        "gpu": sys.gpu_busy_pct,
        "gpu_temp": sys.gpu_temp_c,
        "fan": max(f.rpm for f in sys.fans) if sys.fans else None,
    }


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    if n >= GB:
        return f"{n / GB:.1f} GB"
    if n >= MB:
        return f"{n / MB:.0f} MB"
    return f"{n / 1024:.0f} KB"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} days"
