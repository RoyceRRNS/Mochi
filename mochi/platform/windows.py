"""Windows adapter.

Design notes (see the critique, problem 5):
- All processes are read with one ``NtQuerySystemInformation(SystemProcessInformation)``
  system call. It returns CPU times, private bytes, the private working set, I/O transfer
  counts and hard faults for every process, with no per-process handles, so it never hits
  AccessDenied and costs a few milliseconds instead of ~400 ms for a psutil scan.
- Paging, commit, disk busy/latency, run queue and throttling come from performance
  counters (PDH), because psutil doesn't expose them on Windows.
- Battery drain comes from ``CallNtPowerInformation(SystemBatteryState)``.
- Temperature needs admin or LibreHardwareMonitor, so it's optional and reported as
  "unavailable (reason)" otherwise.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes

import psutil

from ..model import Battery, ProcRaw, SystemRaw, Volume
from .base import (
    AccessDenied,
    BatteryHealth,
    DiskHealth,
    FileInfo,
    Platform,
    PlatformError,
    ProcessGone,
    StartupItem,
    classify_path,
)

log = logging.getLogger(__name__)

ntdll = ctypes.WinDLL("ntdll")
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")
powrprof = ctypes.WinDLL("powrprof")

EPOCH_DIFF = 11644473600.0  # seconds between 1601-01-01 and 1970-01-01
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
SYSTEM_PROCESS_INFORMATION_CLASS = 5


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    # Layout from the Windows NT headers (phnt). 0x100 bytes on x64.
    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong),
        ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong),
        ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong),
        ("OtherTransferCount", ctypes.c_longlong),
    ]


ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
user32.GetWindowLongW.restype = ctypes.c_long


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class SYSTEM_BATTERY_STATE(ctypes.Structure):
    _fields_ = [
        ("AcOnLine", ctypes.c_ubyte),
        ("BatteryPresent", ctypes.c_ubyte),
        ("Charging", ctypes.c_ubyte),
        ("Discharging", ctypes.c_ubyte),
        ("Spare1", ctypes.c_ubyte * 3),
        ("Tag", ctypes.c_ubyte),
        ("MaxCapacity", wintypes.DWORD),
        ("RemainingCapacity", wintypes.DWORD),
        ("Rate", wintypes.DWORD),
        ("EstimatedTime", wintypes.DWORD),
        ("DefaultAlert1", wintypes.DWORD),
        ("DefaultAlert2", wintypes.DWORD),
    ]


class HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD), ("lpszDefaultScheme", wintypes.LPWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __str__(self) -> str:
        d4 = bytes(self.Data4)
        return f"{self.Data1:08x}-{self.Data2:04x}-{self.Data3:04x}-{d4[:2].hex()}-{d4[2:].hex()}"


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


# --- WinVerifyTrust (signature check, no network: revocation off, cache-only URLs) ---
class WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.c_void_p),
    ]


class WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


WINTRUST_ACTION_GENERIC_VERIFY_V2 = GUID(
    0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE)
)

POWER_MODES = {
    "961cc777-2547-4f9d-8174-7d86181b8a7a": "best power efficiency",
    "00000000-0000-0000-0000-000000000000": "balanced",
    "3af9b8d9-7c97-431d-ad78-34a8bfea439f": "better battery",
    "ded574b5-45a0-4f42-8737-46345c09c238": "best performance",
}

PDH_COUNTERS = {
    "paging": r"\Memory\Pages Input/sec",
    "commit": r"\Memory\% Committed Bytes In Use",
    "disk_idle": r"\PhysicalDisk(*)\% Idle Time",
    "disk_latency": r"\PhysicalDisk(*)\Avg. Disk sec/Transfer",
    "disk_queue": r"\PhysicalDisk(_Total)\Current Disk Queue Length",
    "disk_read": r"\PhysicalDisk(_Total)\Disk Read Bytes/sec",
    "disk_write": r"\PhysicalDisk(_Total)\Disk Write Bytes/sec",
    "run_queue": r"\System\Processor Queue Length",
    "perf": r"\Processor Information(_Total)\% Processor Performance",
    "perf_limit": r"\Processor Information(_Total)\% Performance Limit",
}
WILDCARD = {"disk_idle", "disk_latency"}


class _Pdh:
    """A small wrapper around one PDH query. Thread-confined to the collector."""

    def __init__(self) -> None:
        import win32pdh

        self.pdh = win32pdh
        self.query = win32pdh.OpenQuery()
        self.handles: dict[str, int] = {}
        self.failed: dict[str, str] = {}
        for key, path in PDH_COUNTERS.items():
            try:
                self.handles[key] = win32pdh.AddEnglishCounter(self.query, path)
            except Exception as exc:  # counter missing on this machine
                self.failed[key] = f"performance counter missing ({exc.__class__.__name__})"
        try:
            win32pdh.CollectQueryData(self.query)
        except Exception:
            pass
        self.collections = 1

    def read(self) -> dict[str, object]:
        pdh = self.pdh
        out: dict[str, object] = {}
        try:
            pdh.CollectQueryData(self.query)
            self.collections += 1
        except Exception as exc:
            log.debug("PDH collect failed: %s", exc)
            return out
        fmt = pdh.PDH_FMT_DOUBLE | 0x00008000  # PDH_FMT_NOCAP100
        for key, h in self.handles.items():
            try:
                if key in WILDCARD:
                    out[key] = pdh.GetFormattedCounterArray(h, fmt)
                else:
                    out[key] = pdh.GetFormattedCounterValue(h, fmt)[1]
            except Exception:
                out[key] = None
        return out


class WindowsPlatform(Platform):
    name = "windows"

    def __init__(self) -> None:
        self._buf = ctypes.create_string_buffer(1 << 19)
        self._pdh: _Pdh | None = None
        self._pdh_thread: int | None = None
        self._lock = threading.Lock()
        self._exe_cache: dict[tuple[int, int], str | None] = {}
        self._file_cache: dict[str, FileInfo] = {}
        self._volumes_cache: tuple[float, list[str]] = (0.0, [])
        self._temp_checked_at = 0.0
        self._temp_reason = "needs admin or LibreHardwareMonitor"
        self._temp_value: float | None = None
        self._ncpu = psutil.cpu_count() or 1
        self._self_pid = os.getpid()
        self._is_admin: bool | None = None
        psutil.cpu_percent(interval=None, percpu=True)

    # ------------------------------------------------------------------ basics
    def cpu_count(self) -> int:
        return self._ncpu

    def boot_time(self) -> float:
        return psutil.boot_time()

    def self_pid(self) -> int:
        return self._self_pid

    def is_admin(self) -> bool:
        if self._is_admin is None:
            try:
                self._is_admin = bool(shell32.IsUserAnAdmin())
            except Exception:
                self._is_admin = False
        return self._is_admin

    # --------------------------------------------------------------- processes
    def read_processes(self) -> list[ProcRaw]:
        with self._lock:
            size = ctypes.c_ulong(0)
            while True:
                status = ntdll.NtQuerySystemInformation(
                    SYSTEM_PROCESS_INFORMATION_CLASS, self._buf, len(self._buf), ctypes.byref(size)
                )
                if (status & 0xFFFFFFFF) == STATUS_INFO_LENGTH_MISMATCH:
                    self._buf = ctypes.create_string_buffer(size.value + (1 << 16))
                    continue
                if status < 0:
                    raise PlatformError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08x}")
                break
            out: list[ProcRaw] = []
            offset = 0
            buf = self._buf
            while True:
                spi = SYSTEM_PROCESS_INFORMATION.from_buffer(buf, offset)
                pid = int(spi.UniqueProcessId or 0)
                if spi.ImageName.Buffer and spi.ImageName.Length:
                    name = ctypes.wstring_at(spi.ImageName.Buffer, spi.ImageName.Length // 2)
                else:
                    name = "System Idle Process" if pid == 0 else "System"
                ct = spi.CreateTime / 1e7 - EPOCH_DIFF if spi.CreateTime > 0 else 0.0
                out.append(
                    ProcRaw(
                        pid=pid,
                        ppid=int(spi.InheritedFromUniqueProcessId or 0),
                        create_time=ct,
                        name=name,
                        cpu_time=(spi.UserTime + spi.KernelTime) / 1e7,
                        private_bytes=int(spi.PrivatePageCount),
                        ws_private=int(spi.WorkingSetPrivateSize),
                        io_bytes=int(spi.ReadTransferCount + spi.WriteTransferCount),
                        hard_faults=int(spi.HardFaultCount),
                        threads=int(spi.NumberOfThreads),
                    )
                )
                if spi.NextEntryOffset == 0:
                    break
                offset += spi.NextEntryOffset
            return out

    def _proc(self, pid: int, create_time: float) -> psutil.Process:
        try:
            p = psutil.Process(pid)
            if create_time and abs(p.create_time() - create_time) > 1.0:
                raise ProcessGone(f"PID {pid} now belongs to a different process.")
            return p
        except psutil.NoSuchProcess as exc:
            raise ProcessGone(f"Process {pid} has already exited.") from exc
        except psutil.AccessDenied as exc:
            raise AccessDenied(f"Windows won't let me inspect process {pid}.") from exc

    def exe_path(self, pid: int, create_time: float) -> str | None:
        key = (pid, int(create_time))
        if key in self._exe_cache:
            return self._exe_cache[key]
        try:
            path = self._proc(pid, create_time).exe() or None
        except (ProcessGone, AccessDenied, psutil.Error, OSError):
            path = None
        if len(self._exe_cache) > 4096:
            self._exe_cache.clear()
        self._exe_cache[key] = path
        return path

    def cmdline(self, pid: int, create_time: float) -> list[str] | None:
        try:
            return self._proc(pid, create_time).cmdline()
        except (ProcessGone, AccessDenied, psutil.Error, OSError):
            return None

    def process_exists(self, pid: int, create_time: float) -> bool:
        try:
            self._proc(pid, create_time)
            return True
        except (ProcessGone, AccessDenied):
            return False

    def file_info(self, path: str | None) -> FileInfo:
        if not path:
            return FileInfo(None)
        cached = self._file_cache.get(path)
        if cached is not None:
            return cached
        in_sys, risky = classify_path(path)
        desc = company = None
        try:
            import win32api

            trans = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")
            if trans:
                lang, cp = trans[0]
                base = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\"
                desc = win32api.GetFileVersionInfo(path, base + "FileDescription") or None
                company = win32api.GetFileVersionInfo(path, base + "CompanyName") or None
        except Exception:
            pass
        info = FileInfo(path, desc, company, _verify_signature(path), in_sys, risky)
        self._file_cache[path] = info
        return info

    # ------------------------------------------------------------------ system
    def read_system(self) -> SystemRaw:
        tid = threading.get_ident()
        if self._pdh is None or self._pdh_thread != tid:
            # PDH queries are used from one thread; recreate if the caller changed.
            self._pdh = _Pdh()
            self._pdh_thread = tid
        raw = SystemRaw()
        unavailable: dict[str, str] = {}

        per_core = psutil.cpu_percent(interval=None, percpu=True)
        raw.cpu_per_core = per_core
        raw.cpu_total = sum(per_core) / len(per_core) if per_core else None
        vm = psutil.virtual_memory()
        raw.ram_total, raw.ram_available = vm.total, vm.available

        counters = self._pdh.read()
        for key, reason in self._pdh.failed.items():
            unavailable[key] = reason

        def num(key: str) -> float | None:
            v = counters.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        raw.paging_rate = num("paging")
        raw.commit_pct = num("commit")
        idle = counters.get("disk_idle")
        if isinstance(idle, dict):
            disks = {k: v for k, v in idle.items() if k != "_Total"}
            if disks:
                raw.disk_busy_pct = max(0.0, min(100.0, 100.0 - min(disks.values())))
        lat = counters.get("disk_latency")
        if isinstance(lat, dict):
            disks = {k: v for k, v in lat.items() if k != "_Total"}
            if disks:
                raw.disk_latency_ms = max(disks.values()) * 1000.0
        raw.disk_queue = num("disk_queue")
        raw.disk_read_bps = num("disk_read")
        raw.disk_write_bps = num("disk_write")
        raw.run_queue = num("run_queue")
        raw.cpu_perf_pct = num("perf")
        raw.perf_limit_pct = num("perf_limit")
        for key, field_name in (("paging", "paging_rate"), ("disk_idle", "disk_busy_pct"), ("perf_limit", "perf_limit_pct")):
            if getattr(raw, field_name) is None and key not in unavailable:
                unavailable[field_name] = "warming up" if self._pdh.collections < 3 else "counter returned no data"

        try:
            stats = psutil.cpu_stats()
            raw.ctx_switches, raw.interrupts, raw.syscalls = stats.ctx_switches, stats.interrupts, stats.syscalls
        except Exception:
            unavailable["ctx_switches"] = "not readable"

        raw.battery = self._battery()
        if raw.battery is None:
            unavailable["discharge"] = "no battery (desktop or not reported)"

        raw.temp_c = self._temperature()
        if raw.temp_c is None:
            unavailable["temp"] = self._temp_reason

        raw.idle_seconds = _idle_seconds()
        fg_pid, fullscreen_rect = _foreground()
        raw.foreground_pid = fg_pid
        state = _notification_state()
        raw.fullscreen = state in (2, 3, 4) or fullscreen_rect
        raw.fullscreen_d3d = state == 3
        raw.dnd = state == 6
        raw.locked = state == 1 or fg_pid in (None, 0)
        raw.volumes = self._volumes()
        raw.unavailable = unavailable
        return raw

    def describe_foreground(self, raw: SystemRaw, procs: list[ProcRaw]) -> None:
        """Fill in foreground exe name/path from the process snapshot (no extra syscalls)."""
        if not raw.foreground_pid:
            return
        for p in procs:
            if p.pid == raw.foreground_pid:
                raw.foreground_exe = p.name
                raw.foreground_path = self.exe_path(p.pid, p.create_time)
                if p.name.lower() in ("lockapp.exe", "logonui.exe"):
                    raw.locked = True
                return

    def _battery(self) -> Battery | None:
        st = SYSTEM_BATTERY_STATE()
        try:
            rc = powrprof.CallNtPowerInformation(5, None, 0, ctypes.byref(st), ctypes.sizeof(st))
        except Exception:
            rc = -1
        if rc != 0 or not st.BatteryPresent:
            return None
        pct = None
        try:
            sb = psutil.sensors_battery()
            if sb is not None:
                pct = float(sb.percent)
        except Exception:
            pass
        if pct is None and st.MaxCapacity:
            pct = 100.0 * st.RemainingCapacity / st.MaxCapacity
        rate = ctypes.c_long(st.Rate).value  # mW, negative while discharging
        discharge = (-rate / 1000.0) if rate < 0 else 0.0
        return Battery(percent=pct or 0.0, on_ac=bool(st.AcOnLine), discharge_w=discharge, present=True)

    def _temperature(self) -> float | None:
        now = time.monotonic()
        if now - self._temp_checked_at < 30.0:
            return self._temp_value
        self._temp_checked_at = now
        self._temp_value = None
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                wmi = win32com.client.GetObject(r"winmgmts:\\.\root\LibreHardwareMonitor")
                rows = wmi.ExecQuery("SELECT Name, Value, SensorType FROM Sensor WHERE SensorType='Temperature'")
                temps = [float(r.Value) for r in rows if "cpu" in str(r.Name).lower() or "package" in str(r.Name).lower()]
                if temps:
                    self._temp_value = max(temps)
                    return self._temp_value
            except Exception:
                pass
            if self.is_admin():
                try:
                    wmi = win32com.client.GetObject(r"winmgmts:\\.\root\wmi")
                    rows = wmi.ExecQuery("SELECT CurrentTemperature FROM MSAcpi_ThermalZoneTemperature")
                    temps = [r.CurrentTemperature / 10.0 - 273.15 for r in rows]
                    temps = [t for t in temps if 10 < t < 125]
                    if temps:
                        self._temp_value = max(temps)
                        return self._temp_value
                except Exception:
                    pass
        except Exception:
            pass
        self._temp_reason = (
            "no sensor reported by Windows" if self.is_admin() else "needs admin or LibreHardwareMonitor"
        )
        self._temp_checked_at = now + 570.0  # don't retry for ~10 minutes
        return None

    def _volumes(self) -> list[Volume]:
        now = time.monotonic()
        stamp, mounts = self._volumes_cache
        if now - stamp > 60.0 or not mounts:
            mounts = []
            try:
                for part in psutil.disk_partitions(all=False):
                    opts = part.opts.lower()
                    if "fixed" in opts and "cdrom" not in opts and "remote" not in opts:
                        mounts.append(part.mountpoint)
            except Exception:
                mounts = []
            self._volumes_cache = (now, mounts)
        out = []
        for m in mounts:
            try:
                u = psutil.disk_usage(m)
                out.append(Volume(m, u.total, u.free, True))
            except Exception:
                continue
        return out

    # ----------------------------------------------------------------- actions
    def post_close(self, pid: int) -> int:
        hwnds: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                hwnds.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
        for hwnd in hwnds:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return len(hwnds)

    def terminate(self, pid: int) -> None:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess as exc:
            raise ProcessGone(str(exc)) from exc
        except psutil.AccessDenied as exc:
            raise AccessDenied("Windows won't let me end that one (it may need admin).") from exc

    def set_low_priority(self, pid: int) -> None:
        try:
            psutil.Process(pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except psutil.NoSuchProcess as exc:
            raise ProcessGone(str(exc)) from exc
        except psutil.AccessDenied as exc:
            raise AccessDenied("Windows won't let me change that one's priority.") from exc

    def open_location(self, path: str) -> None:
        if not path or not os.path.exists(path):
            raise PlatformError("I can't find that file any more.")
        subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])

    def open_startup_settings(self) -> None:
        os.startfile("ms-settings:startupapps")  # type: ignore[attr-defined]

    # ------------------------------------------------------------- environment
    def reduce_motion(self) -> bool:
        enabled = wintypes.BOOL(True)
        if user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):  # SPI_GETCLIENTAREAANIMATION
            return not bool(enabled.value)
        return False

    def high_contrast(self) -> bool:
        hc = HIGHCONTRASTW()
        hc.cbSize = ctypes.sizeof(HIGHCONTRASTW)
        if user32.SystemParametersInfoW(0x0042, hc.cbSize, ctypes.byref(hc), 0):  # SPI_GETHIGHCONTRAST
            return bool(hc.dwFlags & 0x1)
        return False

    def power_mode(self) -> str | None:
        guid = GUID()
        try:
            if powrprof.PowerGetEffectiveOverlayScheme(ctypes.byref(guid)) != 0:
                return None
        except Exception:
            return None
        key = str(guid)
        return POWER_MODES.get(key, f"custom:{key}")

    def startup_items(self) -> list[StartupItem]:
        import winreg

        items: list[StartupItem] = []
        approved: dict[str, bool] = {}
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
            ) as k:
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    if isinstance(data, bytes) and data:
                        approved[name.lower()] = data[0] in (0x02, 0x06)
                    i += 1
        except OSError:
            pass
        for hive, label in ((winreg.HKEY_CURRENT_USER, "Run (you)"), (winreg.HKEY_LOCAL_MACHINE, "Run (all users)")):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                    i = 0
                    while True:
                        try:
                            name, data, _ = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        items.append(StartupItem(name, _exe_from_command(str(data)), label, approved.get(name.lower(), True)))
                        i += 1
            except OSError:
                pass
        for folder, label in (
            (os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"), "Startup folder (you)"),
            (os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs\StartUp"), "Startup folder (all users)"),
        ):
            try:
                for entry in os.scandir(folder):
                    if entry.is_file() and not entry.name.lower().endswith(".ini"):
                        items.append(StartupItem(os.path.splitext(entry.name)[0], entry.name, label, True))
            except OSError:
                pass
        items.extend(_logon_tasks())
        return items

    def disk_health(self) -> tuple[list[DiskHealth], str | None]:
        disks: list[DiskHealth] = []
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            wmi = win32com.client.GetObject(r"winmgmts:\\.\root\Microsoft\Windows\Storage")
            for d in wmi.ExecQuery("SELECT FriendlyName, MediaType, HealthStatus FROM MSFT_PhysicalDisk"):
                status = {0: "healthy", 1: "warning", 2: "unhealthy"}.get(int(d.HealthStatus or 5), "unknown")
                media = {3: "HDD", 4: "SSD", 5: "SCM"}.get(int(d.MediaType or 0), "disk")
                disks.append(DiskHealth(str(d.FriendlyName), media, status, "Windows Storage"))
        except Exception as exc:
            return [], f"Windows didn't report disk health ({exc.__class__.__name__})."
        if not disks:
            return [], "Windows didn't report any physical disks."
        return disks, None

    def battery_health(self) -> BatteryHealth | None:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            wmi = win32com.client.GetObject(r"winmgmts:\\.\root\wmi")

            def first(query: str, attr: str) -> int | None:
                try:
                    for row in wmi.ExecQuery(query):
                        v = getattr(row, attr)
                        if v:
                            return int(v)
                except Exception:
                    return None
                return None

            design = first("SELECT DesignedCapacity FROM BatteryStaticData", "DesignedCapacity")
            full = first("SELECT FullChargedCapacity FROM BatteryFullChargedCapacity", "FullChargedCapacity")
            cycles = first("SELECT CycleCount FROM BatteryCycleCount", "CycleCount")
        except Exception:
            return None
        if design is None and full is None:
            return None
        return BatteryHealth(design, full, cycles)


# ---------------------------------------------------------------------- helpers
def _idle_seconds() -> float:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    ticks = kernel32.GetTickCount() & 0xFFFFFFFF
    return ((ticks - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def _foreground() -> tuple[int | None, bool]:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    fullscreen = False
    try:
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        style = user32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF  # GWL_STYLE
        # A maximized window with a title bar is not "full screen" (borderless games are).
        borderless = not (style & 0x00C00000) and not user32.IsZoomed(hwnd)
        if borderless and cls.value not in ("Progman", "WorkerW", "Shell_TrayWnd"):
            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                m = mi.rcMonitor
                fullscreen = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    except Exception:
        fullscreen = False
    return pid.value, fullscreen


def _notification_state() -> int:
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value
    except Exception:
        pass
    return 0


def _verify_signature(path: str) -> bool | None:
    try:
        wintrust = ctypes.WinDLL("wintrust")
        fi = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), path, None, None)
        data = WINTRUST_DATA()
        data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
        data.dwUIChoice = 2            # WTD_UI_NONE
        data.fdwRevocationChecks = 0   # WTD_REVOKE_NONE
        data.dwUnionChoice = 1         # WTD_CHOICE_FILE
        data.pFile = ctypes.pointer(fi)
        data.dwStateAction = 1         # WTD_STATEACTION_VERIFY
        data.dwProvFlags = 0x1000 | 0x10  # cache-only URL retrieval + no revocation check: no network (SEC-1)
        guid = WINTRUST_ACTION_GENERIC_VERIFY_V2
        res = wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))
        data.dwStateAction = 2         # WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))
        return (res & 0xFFFFFFFF) == 0
    except Exception:
        return None


def _exe_from_command(command: str) -> str | None:
    command = command.strip()
    if not command:
        return None
    if command.startswith('"'):
        end = command.find('"', 1)
        path = command[1:end] if end > 0 else command[1:]
    else:
        path = command.split(" ")[0]
    return os.path.basename(path) or None


def _logon_tasks() -> list[StartupItem]:
    items: list[StartupItem] = []
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        svc = win32com.client.Dispatch("Schedule.Service")
        svc.Connect()

        def walk(folder, depth=0):
            if depth > 6:
                return
            try:
                tasks = folder.GetTasks(1)  # include hidden
            except Exception:
                tasks = []
            for task in tasks:
                try:
                    if folder.Path.startswith("\\Microsoft"):
                        continue
                    triggers = task.Definition.Triggers
                    if any(t.Type == 9 for t in triggers):  # TASK_TRIGGER_LOGON
                        exe = None
                        for action in task.Definition.Actions:
                            if getattr(action, "Type", None) == 0:
                                exe = os.path.basename(str(action.Path).strip('"'))
                                break
                        items.append(StartupItem(task.Name, exe, "Scheduled task at sign-in", bool(task.Enabled)))
                except Exception:
                    continue
            try:
                for sub in folder.GetFolders(0):
                    walk(sub, depth + 1)
            except Exception:
                pass

        walk(svc.GetFolder("\\"))
    except Exception:
        return items
    return items
