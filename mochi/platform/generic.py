"""Best-effort adapter for Linux/macOS using psutil (and /proc where present).

Windows is the primary target. This adapter keeps Mochi usable elsewhere and reports
every metric it can't read as "unavailable (reason)" instead of zero (COL-8).
"""

from __future__ import annotations

import os
import subprocess
import time

import psutil

from ..model import Battery, ProcRaw, SystemRaw, Volume
from .base import AccessDenied, Platform, PlatformError, ProcessGone


class GenericPlatform(Platform):
    name = "generic"

    def __init__(self) -> None:
        self._procs: dict[int, psutil.Process] = {}
        self._ncpu = psutil.cpu_count() or 1
        self._prev_majflt: tuple[float, int] | None = None
        self._prev_busy: tuple[float, float] | None = None
        psutil.cpu_percent(interval=None, percpu=True)

    def cpu_count(self) -> int:
        return self._ncpu

    def boot_time(self) -> float:
        return psutil.boot_time()

    def read_processes(self) -> list[ProcRaw]:
        out: list[ProcRaw] = []
        alive: set[int] = set()
        for p in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
            info = p.info
            pid = info["pid"]
            alive.add(pid)
            try:
                with p.oneshot():
                    ct = p.cpu_times()
                    mem = p.memory_info()
                    private = getattr(mem, "private", None)
                    if private is None:
                        shared = getattr(mem, "shared", 0) or 0
                        private = max(0, mem.rss - shared)
                    try:
                        io = p.io_counters()
                        io_bytes = io.read_bytes + io.write_bytes
                    except (psutil.AccessDenied, AttributeError, NotImplementedError):
                        io_bytes = None
                    faults = None
                    if hasattr(p, "page_faults"):
                        try:
                            faults = p.page_faults()[1]
                        except Exception:
                            faults = None
                out.append(
                    ProcRaw(pid, info.get("ppid") or 0, info.get("create_time") or 0.0, info.get("name") or "?",
                            ct.user + ct.system, private, None, io_bytes, faults, p.num_threads())
                )
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                out.append(ProcRaw(pid, info.get("ppid") or 0, info.get("create_time") or 0.0, info.get("name") or "?",
                                   0.0, None, None, None, None, 0))
        return out

    def read_system(self) -> SystemRaw:
        raw = SystemRaw()
        unavailable: dict[str, str] = {}
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        raw.cpu_per_core = per_core
        raw.cpu_total = sum(per_core) / len(per_core) if per_core else None
        vm = psutil.virtual_memory()
        raw.ram_total, raw.ram_available = vm.total, vm.available
        now = time.monotonic()
        # Hard faults from /proc/vmstat (Linux).
        try:
            with open("/proc/vmstat", encoding="ascii") as fh:
                majflt = next(int(line.split()[1]) for line in fh if line.startswith("pgmajfault "))
            if self._prev_majflt:
                t0, v0 = self._prev_majflt
                raw.paging_rate = max(0.0, (majflt - v0) / max(1e-6, now - t0))
            self._prev_majflt = (now, majflt)
        except (OSError, StopIteration, ValueError):
            unavailable["paging_rate"] = "no /proc/vmstat on this system"
        try:
            io = psutil.disk_io_counters()
            busy = getattr(io, "busy_time", None)
            if busy is not None:
                if self._prev_busy:
                    t0, b0 = self._prev_busy
                    raw.disk_busy_pct = max(0.0, min(100.0, (busy - b0) / 10.0 / max(1e-6, now - t0)))
                self._prev_busy = (now, busy)
            else:
                unavailable["disk_busy_pct"] = "disk busy time isn't reported here"
        except Exception:
            unavailable["disk_busy_pct"] = "disk counters unavailable"
        try:
            raw.run_queue = psutil.getloadavg()[0]
        except Exception:
            pass
        try:
            temps = psutil.sensors_temperatures()  # type: ignore[attr-defined]
            vals = [t.current for entries in temps.values() for t in entries if t.current]
            raw.temp_c = max(vals) if vals else None
        except Exception:
            raw.temp_c = None
        if raw.temp_c is None:
            unavailable["temp"] = "no temperature sensor reported"
        try:
            sb = psutil.sensors_battery()
        except Exception:
            sb = None
        if sb is not None:
            raw.battery = Battery(float(sb.percent), bool(sb.power_plugged), None, True)
        unavailable["discharge"] = "battery drain rate isn't reported here"
        try:
            st = psutil.cpu_stats()
            raw.ctx_switches, raw.interrupts, raw.syscalls = st.ctx_switches, st.interrupts, st.syscalls
        except Exception:
            pass
        unavailable["idle_seconds"] = "keyboard/mouse idle time isn't available on this platform"
        unavailable["foreground"] = "the focused app isn't available on this platform (e.g. Wayland)"
        raw.volumes = []
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                raw.volumes.append(Volume(part.mountpoint, u.total, u.free, True))
            except Exception:
                continue
        raw.unavailable = unavailable
        return raw

    def exe_path(self, pid: int, create_time: float) -> str | None:
        try:
            return psutil.Process(pid).exe() or None
        except psutil.Error:
            return None

    def cmdline(self, pid: int, create_time: float) -> list[str] | None:
        try:
            return psutil.Process(pid).cmdline()
        except psutil.Error:
            return None

    def process_exists(self, pid: int, create_time: float) -> bool:
        try:
            return abs(psutil.Process(pid).create_time() - create_time) < 1.0
        except psutil.Error:
            return False

    def terminate(self, pid: int) -> None:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess as exc:
            raise ProcessGone(str(exc)) from exc
        except psutil.AccessDenied as exc:
            raise AccessDenied("The OS won't let me end that process.") from exc

    def set_low_priority(self, pid: int) -> None:
        try:
            psutil.Process(pid).nice(10)
        except psutil.NoSuchProcess as exc:
            raise ProcessGone(str(exc)) from exc
        except psutil.AccessDenied as exc:
            raise AccessDenied("The OS won't let me change that process's priority.") from exc

    def open_location(self, path: str) -> None:
        if not path or not os.path.exists(path):
            raise PlatformError("I can't find that file any more.")
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
