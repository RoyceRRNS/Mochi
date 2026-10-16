"""Where Mochi keeps its files (STO-1, STO-9).

Data lives in ``%LOCALAPPDATA%\\Mochi\\<profile>`` so it never lands in the repo.
A separate profile (``--profile dev``) keeps development runs away from the
always-on instance's database.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def default_base() -> Path:
    override = os.environ.get("MOCHI_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Mochi"
    return Path.home() / ".local" / "share" / "Mochi"


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def for_profile(cls, profile: str | None = None, base: Path | None = None) -> "Paths":
        profile = profile or os.environ.get("MOCHI_PROFILE") or "default"
        if not PROFILE_RE.match(profile):
            raise ValueError(f"Profile names may use letters, digits, '-' and '_' only (got {profile!r}).")
        return cls((base or default_base()) / profile)

    @property
    def db(self) -> Path:
        return self.root / "mochi.db"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def activity_map(self) -> Path:
        return self.root / "activity_map.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def lock(self) -> Path:
        return self.root / "mochi.lock"

    def ensure(self) -> "Paths":
        for p in (self.root, self.logs, self.exports, self.tmp):
            p.mkdir(parents=True, exist_ok=True)
        return self
