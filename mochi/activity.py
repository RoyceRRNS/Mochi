"""What is the person doing? (CTX-2, CTX-3, CTX-4)

- The foreground app maps to an activity class through an editable JSON file.
- "Idle" has one shared definition used everywhere: no keyboard/mouse input for
  10 minutes AND not full screen AND no media or game in front. Watching a film or
  playing with a gamepad therefore never counts as idle. The lock screen does.
- The class is smoothed: the dominant class over the last 60 s, so alt-tabbing
  doesn't flip buckets every sample.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, deque
from pathlib import Path

from .model import ACTIVITIES, SystemRaw

DEFAULT_MAP: dict[str, list[str]] = {
    "browsing": ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
                 "librewolf.exe", "waterfox.exe"],
    "coding": ["code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "clion64.exe", "rider64.exe",
               "webstorm64.exe", "goland64.exe", "sublime_text.exe", "notepad++.exe", "windowsterminal.exe",
               "wt.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "studio64.exe", "zed.exe", "nvim-qt.exe"],
    "gaming": ["steam.exe", "epicgameslauncher.exe", "minecraft.exe", "javaw.exe", "riotclientservices.exe",
               "battle.net.exe", "gamebar.exe"],
    "media": ["vlc.exe", "spotify.exe", "wmplayer.exe", "microsoft.media.player.exe", "mpc-hc64.exe",
              "potplayermini64.exe", "video.ui.exe", "music.ui.exe", "obs64.exe", "itunes.exe", "netflix.exe"],
    "office": ["winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe", "onenote.exe", "olk.exe", "ms-teams.exe",
               "teams.exe", "notion.exe", "obsidian.exe", "acrobat.exe", "acrord32.exe", "notepad.exe",
               "sumatrapdf.exe", "slack.exe", "zoom.exe", "thunderbird.exe"],
}

GAME_PATH_RE = re.compile(r"\\(steamapps\\common|epic games|xboxgames|riot games|gog galaxy\\games)\\", re.I)


def flatten(mapping: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cls, exes in mapping.items():
        for exe in exes:
            out[exe.lower()] = cls
    return out


class ActivityMapper:
    def __init__(self, mapping: dict[str, str] | None = None, idle_minutes: float = 10.0) -> None:
        self.mapping = dict(mapping) if mapping is not None else flatten(DEFAULT_MAP)
        self.idle_minutes = idle_minutes
        self.warnings: list[str] = []

    # --- file -------------------------------------------------------------
    @classmethod
    def load(cls, path: Path, idle_minutes: float = 10.0) -> "ActivityMapper":
        mapper = cls(idle_minutes=idle_minutes)
        if not path.exists():
            return mapper
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            mapping: dict[str, str] = {}
            for exe, cls_name in data.items():
                if isinstance(exe, str) and cls_name in ACTIVITIES and cls_name != "idle":
                    mapping[exe.lower()] = cls_name
                else:
                    mapper.warnings.append(f"Ignored mapping {exe!r} -> {cls_name!r}.")
            mapper.mapping = mapping
        except (OSError, ValueError) as exc:
            mapper.warnings.append(f"Couldn't read the app mapping ({exc}); using defaults.")
        return mapper

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(self.mapping.items())), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def set(self, exe: str, cls_name: str) -> None:
        if cls_name not in ACTIVITIES or cls_name == "idle":
            raise ValueError(f"Unknown activity {cls_name!r}")
        self.mapping[exe.lower()] = cls_name

    def remove(self, exe: str) -> None:
        self.mapping.pop(exe.lower(), None)

    # --- classification ---------------------------------------------------
    def app_class(self, exe: str | None, path: str | None = None, sysraw: SystemRaw | None = None) -> str | None:
        if exe:
            cls_name = self.mapping.get(exe.lower())
            if cls_name:
                return cls_name
        if path and GAME_PATH_RE.search(path):
            return "gaming"
        if sysraw is not None and sysraw.fullscreen_d3d:
            return "gaming"
        return None

    def is_user_idle(self, sysraw: SystemRaw) -> bool:
        """The single shared idle definition (CTX-4)."""
        if sysraw.locked:
            return True
        if sysraw.idle_seconds < self.idle_minutes * 60:
            return False
        if sysraw.fullscreen:
            return False
        cls_name = self.app_class(sysraw.foreground_exe, sysraw.foreground_path, sysraw)
        return cls_name not in ("media", "gaming")

    def classify(self, sysraw: SystemRaw) -> str:
        if self.is_user_idle(sysraw):
            return "idle"
        return self.app_class(sysraw.foreground_exe, sysraw.foreground_path, sysraw) or "other"


class ActivitySmoother:
    """Dominant class over the last ``window_s`` seconds (ties go to the most recent)."""

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = window_s
        self._hist: deque[tuple[float, str]] = deque()

    def reset(self) -> None:
        self._hist.clear()

    def update(self, mono: float, raw: str) -> str:
        self._hist.append((mono, raw))
        while self._hist and self._hist[0][0] < mono - self.window_s:
            self._hist.popleft()
        counts = Counter(c for _, c in self._hist)
        best = max(counts.values())
        for _, c in reversed(self._hist):
            if counts[c] == best:
                return c
        return raw
