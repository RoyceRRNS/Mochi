"""One-click actions, done safely (ACT-1, ACT-2).

Ending a process:
- System and protected processes are never offered (killing csrss.exe crashes Windows).
- The (pid, create_time) pair is checked again right before acting, so a reused PID is
  never killed by mistake (the classic time-of-check/time-of-use race).
- The app is asked to close its windows first (so it can save), then ended after 5 s.

"Let it run slower" lowers the process priority instead (an OS scheduling concept and a
gentler option than ending it).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .grouping import SELF_KEY, SYSTEM_NAMES
from .platform.base import AccessDenied, Platform, PlatformError, ProcessGone

PROTECTED = SYSTEM_NAMES | {"system idle process", "idle", "mochi.exe"}


@dataclass
class ActionResult:
    ok: bool
    message: str
    freed_bytes: int = 0


def is_protected(name: str, pid: int, self_pid: int, app_key: str | None = None) -> bool:
    return pid in (0, 4) or pid == self_pid or name.lower() in PROTECTED or app_key == SELF_KEY


class ActionRunner:
    def __init__(self, platform: Platform, sleep: Callable[[float], None] = time.sleep,
                 grace_s: float = 5.0) -> None:
        self.platform = platform
        self.sleep = sleep
        self.grace_s = grace_s

    def confirm_text(self, display: str, pid: int) -> str:
        return (f"End {display} (process {pid})?\n\nIf it has unsaved work, that work may be lost. "
                f"I'll ask it to close nicely first.")

    def end_process(self, pid: int, create_time: float, name: str, app_key: str | None = None) -> ActionResult:
        if is_protected(name, pid, self.platform.self_pid(), app_key):
            return ActionResult(False, f"{name} is part of Windows (or Mochi), so I won't end it.")
        if not self.platform.process_exists(pid, create_time):
            return ActionResult(False, f"{name} has already closed (or its process ID now belongs to something else).")
        try:
            asked = self.platform.post_close(pid)
        except PlatformError:
            asked = 0
        if asked:
            waited = 0.0
            while waited < self.grace_s:
                self.sleep(0.25)
                waited += 0.25
                if not self.platform.process_exists(pid, create_time):
                    return ActionResult(True, f"{name} closed itself nicely.")
        if not self.platform.process_exists(pid, create_time):
            return ActionResult(True, f"{name} has closed.")
        try:
            self.platform.terminate(pid)
        except ProcessGone:
            return ActionResult(True, f"{name} has closed.")
        except AccessDenied as exc:
            return ActionResult(False, f"I couldn't end {name}: {exc}")
        return ActionResult(True, f"I ended {name}.")

    def lower_priority(self, pid: int, create_time: float, name: str) -> ActionResult:
        if pid in (0, 4) or pid == self.platform.self_pid():
            return ActionResult(False, f"I won't change {name}'s priority.")
        if not self.platform.process_exists(pid, create_time):
            return ActionResult(False, f"{name} has already closed.")
        try:
            self.platform.set_low_priority(pid)
        except ProcessGone:
            return ActionResult(False, f"{name} has already closed.")
        except AccessDenied as exc:
            return ActionResult(False, f"I couldn't slow {name} down: {exc}")
        return ActionResult(True, f"{name} will now let other apps go first.")

    def open_location(self, path: str | None) -> ActionResult:
        if not path:
            return ActionResult(False, "I don't know where that program lives.")
        try:
            self.platform.open_location(path)
        except PlatformError as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, "Opened its folder.")

    def open_startup_settings(self) -> ActionResult:
        try:
            self.platform.open_startup_settings()
        except PlatformError as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, "Opened startup apps settings.")
