"""A scripted platform and clock for tests, the replay harness and the demo.

Processes are described by *rates* (CPU % of one core, MB, bytes/s). The fake turns
them into cumulative counters, exactly like the real OS, so the collector's rate
derivation is exercised too.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from datetime import datetime

from ..model import GB, MB, Battery, ProcRaw, SystemRaw, Volume
from .base import AccessDenied, BatteryHealth, DiskHealth, FileInfo, Platform, PlatformError, ProcessGone, StartupItem


def local_ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second).timestamp()


# Monday 12 October 2026, 10:00 local time.
DEFAULT_START = local_ts(2026, 10, 12, 10, 0, 0)


class FakeClock:
    def __init__(self, ts: float = DEFAULT_START, mono: float = 10_000.0) -> None:
        self.ts = ts
        self.mono = mono

    def time(self) -> float:
        return self.ts

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.ts += seconds
        self.mono += seconds

    def set_wall(self, ts: float) -> None:
        """Change only the wall clock (like a manual clock change or DST)."""
        self.ts = ts


@dataclass
class FakeProc:
    name: str
    pid: int
    ppid: int = 4
    create_time: float = 0.0
    cpu_core: float = 0.0
    private_mb: float = 50.0
    ws_private_mb: float | None = None
    io_bps: float = 0.0
    hard_faults_ps: float = 0.0
    exe: str | None = None
    cmdline: list[str] | None = None
    threads: int = 1


@dataclass
class FakeSystem:
    cpu: float | None = None            # None = derive from processes
    ram_used_gb: float | None = None    # None = 3 GB + sum of process private memory
    commit_pct: float | None = 40.0
    paging_rate: float | None = 5.0
    disk_busy_pct: float | None = 3.0
    disk_latency_ms: float | None = 1.0
    disk_queue: float | None = 0.0
    cpu_perf_pct: float | None = 110.0
    perf_limit_pct: float | None = 100.0
    run_queue: float | None = 0.0
    temp_c: float | None = None
    battery: Battery | None = None
    idle_seconds: float = 5.0
    foreground_pid: int | None = None
    fullscreen: bool = False
    fullscreen_d3d: bool = False
    dnd: bool = False
    locked: bool = False
    volumes: list[Volume] = field(default_factory=lambda: [Volume("C:\\", 512 * GB, 300 * GB, True)])
    ctx_rate: float = 20_000.0
    interrupt_rate: float = 10_000.0
    unavailable: dict[str, str] = field(default_factory=lambda: {"temp": "needs admin or LibreHardwareMonitor"})


class FakePlatform(Platform):
    name = "fake"

    def __init__(self, clock: FakeClock, ncpu: int = 8, ram_gb: float = 16.0, self_pid: int = 999,
                 boot_time: float | None = None) -> None:
        self.clock = clock
        self.ncpu = ncpu
        self.ram_total = int(ram_gb * GB)
        self._self_pid = self_pid
        self._boot = boot_time if boot_time is not None else clock.time() - 3600.0
        self.sys = FakeSystem()
        self.procs: dict[int, FakeProc] = {}
        self._cum: dict[tuple[int, float], list[float]] = {}
        self._last_mono: float | None = None
        self._ctx = 1_000_000.0
        self._irq = 500_000.0
        self.calls: list[tuple] = []
        self.close_exits: set[int] = set()        # pids that exit when asked to close
        self.access_denied: set[int] = set()
        self.files: dict[str, FileInfo] = {}
        self.startup: list[StartupItem] = []
        self.disks: tuple[list[DiskHealth], str | None] = ([], "Disk health isn't available in the simulator.")
        self.battery_info: BatteryHealth | None = None
        self.power = "balanced"
        self.motion_reduced = False
        self.contrast = False
        self.fail_reads = 0                        # raise on the next N reads (thread-crash tests)
        self.noise = 0.0                           # relative jitter on system readings (0 = exact)
        self._rng = random.Random(1234)
        self.add(FakeProc("System", 4, 0, create_time=self._boot, private_mb=0.2))
        self.add(FakeProc("Mochi.exe", self_pid, 1000, create_time=self._boot + 60, private_mb=60))

    # --- scripting helpers ------------------------------------------------
    def add(self, proc: FakeProc) -> FakeProc:
        if not proc.create_time:
            proc.create_time = self.clock.time()
        self.procs[proc.pid] = proc
        return proc

    def remove(self, pid: int) -> None:
        self.procs.pop(pid, None)

    def update(self, pid: int, **kw) -> None:
        for k, v in kw.items():
            setattr(self.procs[pid], k, v)

    def set(self, **kw) -> None:
        for k, v in kw.items():
            if not hasattr(self.sys, k):
                raise AttributeError(k)
            setattr(self.sys, k, v)

    # --- Platform ---------------------------------------------------------
    def cpu_count(self) -> int:
        return self.ncpu

    def boot_time(self) -> float:
        return self._boot

    def self_pid(self) -> int:
        return self._self_pid

    def _advance_counters(self) -> None:
        mono = self.clock.monotonic()
        dt = 0.0 if self._last_mono is None else max(0.0, mono - self._last_mono)
        self._last_mono = mono
        for p in self.procs.values():
            key = (p.pid, p.create_time)
            cum = self._cum.setdefault(key, [0.0, 0.0, 0.0])
            cum[0] += p.cpu_core / 100.0 * dt
            cum[1] += p.io_bps * dt
            cum[2] += p.hard_faults_ps * dt
        self._ctx += self.sys.ctx_rate * dt
        self._irq += self.sys.interrupt_rate * dt

    def read_processes(self) -> list[ProcRaw]:
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise PlatformError("simulated read failure")
        self._advance_counters()
        out = [ProcRaw(0, 0, 0.0, "System Idle Process", 0.0, 0, 0, 0, 0, self.ncpu)]
        for p in self.procs.values():
            cum = self._cum[(p.pid, p.create_time)]
            ws = p.ws_private_mb if p.ws_private_mb is not None else p.private_mb * 0.85
            out.append(ProcRaw(p.pid, p.ppid, p.create_time, p.name, cum[0], int(p.private_mb * MB),
                               int(ws * MB), int(cum[1]), int(cum[2]), p.threads))
        return out

    def read_system(self) -> SystemRaw:
        s = self.sys
        cpu = s.cpu
        if cpu is None:
            cpu = min(100.0, 2.0 + sum(p.cpu_core for p in self.procs.values()) / self.ncpu)
        used = s.ram_used_gb
        if used is None:
            used = 3.0 + sum(p.private_mb for p in self.procs.values()) / 1024.0
        paging, disk = s.paging_rate, s.disk_busy_pct
        if self.noise:
            j = self._rng.uniform
            cpu = max(0.0, min(100.0, cpu * (1 + j(-self.noise, self.noise)) + j(0, 2 * self.noise * 10)))
            used = max(0.1, used * (1 + j(-self.noise, self.noise) * 0.1))
            if paging is not None:
                paging = max(0.0, paging * (1 + j(-self.noise, self.noise)) + j(0, 20 * self.noise))
            if disk is not None:
                disk = max(0.0, min(100.0, disk * (1 + j(-self.noise, self.noise)) + j(0, 10 * self.noise)))
        used_b = min(self.ram_total, int(used * GB))
        return SystemRaw(
            cpu_total=cpu,
            cpu_per_core=[cpu] * self.ncpu,
            ram_total=self.ram_total,
            ram_available=self.ram_total - used_b,
            commit_pct=s.commit_pct,
            paging_rate=paging,
            disk_busy_pct=disk,
            disk_latency_ms=s.disk_latency_ms,
            disk_queue=s.disk_queue,
            cpu_perf_pct=s.cpu_perf_pct,
            perf_limit_pct=s.perf_limit_pct,
            run_queue=s.run_queue,
            temp_c=s.temp_c,
            battery=copy.copy(s.battery),
            ctx_switches=int(self._ctx),
            interrupts=int(self._irq),
            syscalls=int(self._ctx * 3),
            idle_seconds=s.idle_seconds,
            foreground_pid=s.foreground_pid,
            fullscreen=s.fullscreen,
            fullscreen_d3d=s.fullscreen_d3d,
            dnd=s.dnd,
            locked=s.locked,
            volumes=[Volume(v.mount, v.total, v.free, v.fixed) for v in s.volumes],
            unavailable=dict(s.unavailable),
        )

    def describe_foreground(self, raw: SystemRaw, procs: list[ProcRaw]) -> None:
        if raw.foreground_pid and raw.foreground_pid in self.procs:
            p = self.procs[raw.foreground_pid]
            raw.foreground_exe = p.name
            raw.foreground_path = self.exe_path(p.pid, p.create_time)

    def exe_path(self, pid: int, create_time: float) -> str | None:
        p = self.procs.get(pid)
        if p is None:
            return None
        return p.exe or f"C:\\Program Files\\{p.name.rsplit('.', 1)[0]}\\{p.name}"

    def cmdline(self, pid: int, create_time: float) -> list[str] | None:
        p = self.procs.get(pid)
        return list(p.cmdline) if p and p.cmdline else None

    def file_info(self, path: str | None) -> FileInfo:
        if path in self.files:
            return self.files[path]
        info = super().file_info(path)
        if path:
            info.company = "Contoso Ltd."
            info.description = path.rsplit("\\", 1)[-1]
            info.signed = True
        return info

    def process_exists(self, pid: int, create_time: float) -> bool:
        p = self.procs.get(pid)
        return p is not None and abs(p.create_time - create_time) < 1.0

    def post_close(self, pid: int) -> int:
        self.calls.append(("post_close", pid))
        if pid in self.access_denied:
            return 0
        if pid in self.close_exits:
            self.remove(pid)
            return 1
        return 1 if pid in self.procs else 0

    def terminate(self, pid: int) -> None:
        self.calls.append(("terminate", pid))
        if pid in self.access_denied:
            raise AccessDenied("Windows won't let me end that one (it may need admin).")
        if pid not in self.procs:
            raise ProcessGone("Already gone.")
        self.remove(pid)

    def set_low_priority(self, pid: int) -> None:
        self.calls.append(("low_priority", pid))
        if pid in self.access_denied:
            raise AccessDenied("Windows won't let me change that one's priority.")
        if pid not in self.procs:
            raise ProcessGone("Already gone.")

    def open_location(self, path: str) -> None:
        self.calls.append(("open_location", path))
        if not path:
            raise PlatformError("I can't find that file any more.")

    def open_startup_settings(self) -> None:
        self.calls.append(("startup_settings",))

    def reduce_motion(self) -> bool:
        return self.motion_reduced

    def high_contrast(self) -> bool:
        return self.contrast

    def power_mode(self) -> str | None:
        return self.power

    def startup_items(self) -> list[StartupItem]:
        return list(self.startup)

    def disk_health(self) -> tuple[list[DiskHealth], str | None]:
        return self.disks

    def battery_health(self) -> BatteryHealth | None:
        return self.battery_info
