"""Start at sign-in without admin (LIF-1).

Uses the per-user ``HKCU\\...\\Run`` key. The registry is behind a tiny interface so tests
can use a fake (or a scratch key) and never touch the real Run key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Mochi"


class Registry(Protocol):
    def get(self, key: str, name: str) -> str | None: ...
    def set(self, key: str, name: str, value: str) -> None: ...
    def delete(self, key: str, name: str) -> None: ...


class WinRegistry:
    def get(self, key: str, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return None

    def set(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass


class MemoryRegistry:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, key: str, name: str) -> str | None:
        return self.values.get((key, name))

    def set(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def delete(self, key: str, name: str) -> None:
        self.values.pop((key, name), None)


def launch_command(profile: str = "default") -> str:
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        cmd = f'"{exe}"'
    else:
        pythonw = exe.with_name("pythonw.exe")
        runner = pythonw if pythonw.exists() else exe
        cmd = f'"{runner}" -m mochi'
    cmd += " --start-hidden"
    if profile != "default":
        cmd += f" --profile {profile}"
    return cmd


class Autostart:
    def __init__(self, registry: Registry | None = None, key: str = RUN_KEY, name: str = VALUE_NAME,
                 profile: str = "default") -> None:
        self.registry = registry or (WinRegistry() if sys.platform == "win32" else MemoryRegistry())
        self.key = key
        self.name = name if profile == "default" else f"{name} ({profile})"
        self.profile = profile

    def is_enabled(self) -> bool:
        return self.registry.get(self.key, self.name) is not None

    def enable(self) -> str:
        cmd = launch_command(self.profile)
        self.registry.set(self.key, self.name, cmd)
        return cmd

    def disable(self) -> None:
        self.registry.delete(self.key, self.name)

    def apply(self, enabled: bool) -> None:
        if enabled:
            self.enable()
        else:
            self.disable()
