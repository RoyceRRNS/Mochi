"""The OS adapter interface. Everything above this layer is platform-neutral."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import PureWindowsPath

from ..model import ProcRaw, SystemRaw


class PlatformError(Exception):
    """Base class for adapter errors that callers handle politely."""


class ProcessGone(PlatformError):
    """The process exited, or its PID now belongs to a different process."""


class AccessDenied(PlatformError):
    """The OS refused (usually: needs admin, or the process is protected)."""


@dataclass(slots=True)
class FileInfo:
    path: str | None
    description: str | None = None
    company: str | None = None
    signed: bool | None = None          # None = couldn't check
    in_system_dir: bool = False
    in_temp_or_downloads: bool = False

    @property
    def unfamiliar(self) -> bool:
        """'Unfamiliar' never means malicious (DIA-12). It means: not a Windows file and
        either unsigned with no publisher, or running from Temp/Downloads."""
        if self.path is None:
            return False
        if self.in_temp_or_downloads:
            return True
        if self.in_system_dir:
            return False
        return self.signed is False and not self.company

    @property
    def publisher(self) -> str:
        return self.company or "unknown publisher"


@dataclass(slots=True)
class StartupItem:
    name: str
    exe: str | None
    source: str
    enabled: bool | None = True


@dataclass(slots=True)
class DiskHealth:
    name: str
    media: str
    status: str           # healthy | warning | unhealthy | unknown
    source: str


@dataclass(slots=True)
class BatteryHealth:
    design_mwh: int | None
    full_mwh: int | None
    cycles: int | None

    @property
    def health_pct(self) -> float | None:
        if not self.design_mwh or not self.full_mwh:
            return None
        return 100.0 * self.full_mwh / self.design_mwh


def classify_path(path: str | None) -> tuple[bool, bool]:
    """Return (in_system_dir, in_temp_or_downloads) for a Windows-style path."""
    if not path:
        return False, False
    p = path.lower().replace("/", "\\")
    sysroot = (os.environ.get("SystemRoot") or r"C:\Windows").lower().rstrip("\\") + "\\"
    in_system = p.startswith(sysroot)
    risky = False
    temp = (os.environ.get("TEMP") or "").lower().rstrip("\\")
    if temp and p.startswith(temp + "\\"):
        risky = True
    parts = [s.lower() for s in PureWindowsPath(p).parts]
    if "downloads" in parts or ("appdata" in parts and "temp" in parts):
        risky = True
    return in_system, risky


class Platform(ABC):
    name = "base"

    # --- readings ---------------------------------------------------------
    @abstractmethod
    def read_system(self) -> SystemRaw: ...

    @abstractmethod
    def read_processes(self) -> list[ProcRaw]: ...

    @abstractmethod
    def cpu_count(self) -> int: ...

    @abstractmethod
    def boot_time(self) -> float: ...

    def self_pid(self) -> int:
        return os.getpid()

    def describe_foreground(self, raw: SystemRaw, procs: list[ProcRaw]) -> None:
        """Fill in foreground exe name/path once the process list is known."""
        return None

    # --- process details (looked up lazily, only for culprits) ------------
    def exe_path(self, pid: int, create_time: float) -> str | None:
        return None

    def cmdline(self, pid: int, create_time: float) -> list[str] | None:
        return None

    def file_info(self, path: str | None) -> FileInfo:
        in_sys, risky = classify_path(path)
        return FileInfo(path, in_system_dir=in_sys, in_temp_or_downloads=risky)

    def process_exists(self, pid: int, create_time: float) -> bool:
        return False

    # --- actions ----------------------------------------------------------
    def post_close(self, pid: int) -> int:
        """Ask a process's windows to close. Returns how many windows were asked."""
        return 0

    def terminate(self, pid: int) -> None:
        raise AccessDenied("Not supported on this platform.")

    def set_low_priority(self, pid: int) -> None:
        raise AccessDenied("Not supported on this platform.")

    def open_location(self, path: str) -> None:
        raise PlatformError("Not supported on this platform.")

    def open_startup_settings(self) -> None:
        raise PlatformError("Not supported on this platform.")

    # --- environment ------------------------------------------------------
    def reduce_motion(self) -> bool:
        return False

    def high_contrast(self) -> bool:
        return False

    def power_mode(self) -> str | None:
        return None

    def startup_items(self) -> list[StartupItem]:
        return []

    def disk_health(self) -> tuple[list[DiskHealth], str | None]:
        return [], "Disk health isn't available on this platform."

    def battery_health(self) -> BatteryHealth | None:
        return None

    def is_admin(self) -> bool:
        return False
