"""Mochi's mood: a live, glanceable summary of system health (MOOD-1..3).

- Precedence when several moods apply: Dizzy > Sweaty > Grumpy > Puffy > Zoomies >
  Proud > Sleepy > Happy.
- Moods react to the last ~15 s of readings, independently of the 5-minute alert rule,
  so Mochi puffs up within 30 s of a memory hog starting.
- A mood lasts at least 30 s, so it never flickers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum


class Mood(str, Enum):
    HAPPY = "happy"
    SLEEPY = "sleepy"
    PUFFY = "puffy"
    SWEATY = "sweaty"
    DIZZY = "dizzy"
    GRUMPY = "grumpy"
    ZOOMIES = "zoomies"
    PROUD = "proud"


PRECEDENCE = [Mood.DIZZY, Mood.SWEATY, Mood.GRUMPY, Mood.PUFFY, Mood.ZOOMIES, Mood.PROUD, Mood.SLEEPY, Mood.HAPPY]

# Plain-language label for every playful element (MOOD-3, design principle 5).
LABELS: dict[Mood, tuple[str, str]] = {
    Mood.HAPPY: ("Happy", "Everything is within your normal."),
    Mood.SLEEPY: ("Sleepy", "You're away and your PC is resting."),
    Mood.PUFFY: ("Puffy", "Memory use is well above your normal."),
    Mood.SWEATY: ("Sweaty", "The CPU is hot or slowing itself down."),
    Mood.DIZZY: ("Dizzy", "Your PC is swapping memory to disk heavily."),
    Mood.GRUMPY: ("Grumpy", "A drive is nearly full."),
    Mood.ZOOMIES: ("Zoomies", "I'm running a quick speed test."),
    Mood.PROUD: ("Proud", "Things got better. Nice work!"),
}

SPEECH: dict[Mood, str] = {
    Mood.HAPPY: "I'm feeling great! Everything looks normal.",
    Mood.SLEEPY: "Zzz... nothing much happening.",
    Mood.PUFFY: "I'm feeling a bit puffy. Memory is fuller than usual.",
    Mood.SWEATY: "Phew, it's getting warm in here.",
    Mood.DIZZY: "Whoa, I'm dizzy. Memory keeps getting shuffled to disk.",
    Mood.GRUMPY: "Hmph. I'm running out of room on a drive.",
    Mood.ZOOMIES: "Zoom zoom! Speed test in progress.",
    Mood.PROUD: "Yay! That helped a lot.",
}


@dataclass
class MoodInputs:
    ram_z: float | None = None
    ram_used_pct: float = 0.0
    paging_z: float | None = None
    paging_rate: float | None = None
    ram_available_pct: float = 100.0
    temp_c: float | None = None
    tjmax_c: float = 100.0
    throttled: bool = False
    disk_nearly_full: bool = False
    benchmark_running: bool = False
    proud: bool = False
    user_idle: bool = False
    cpu: float | None = None
    memory_pressure_open: bool = False
    throttling_open: bool = False
    storage_open: bool = False


def conditions(i: MoodInputs) -> dict[Mood, bool]:
    thrashing = i.memory_pressure_open or (
        (i.paging_z is not None and i.paging_z > 3.0 or (i.paging_rate or 0) >= 1000.0) and i.ram_available_pct < 15.0
    )
    hot = i.temp_c is not None and i.temp_c >= i.tjmax_c - 15.0
    return {
        Mood.DIZZY: thrashing,
        Mood.SWEATY: hot or i.throttled or i.throttling_open,
        Mood.GRUMPY: i.disk_nearly_full or i.storage_open,
        Mood.PUFFY: (i.ram_z is not None and i.ram_z > 2.0) or i.ram_used_pct >= 90.0,
        Mood.ZOOMIES: i.benchmark_running,
        Mood.PROUD: i.proud,
        Mood.SLEEPY: i.user_idle and (i.cpu or 0.0) < 15.0,
        Mood.HAPPY: True,
    }


class MoodEngine:
    def __init__(self, window_s: float = 15.0, dwell_s: float = 30.0) -> None:
        self.window_s = window_s
        self.dwell_s = dwell_s
        self.current = Mood.HAPPY
        self.since: float | None = None
        self._hist: deque[tuple[float, dict[Mood, bool]]] = deque()

    def update(self, mono: float, conds: dict[Mood, bool]) -> Mood:
        self._hist.append((mono, conds))
        while self._hist and self._hist[0][0] < mono - self.window_s:
            self._hist.popleft()
        if self.since is None:
            self.since = mono
        # A mood "holds" when it was true in most of the recent window.
        n = len(self._hist)
        wanted = Mood.HAPPY
        for mood in PRECEDENCE:
            hits = sum(1 for _, c in self._hist if c.get(mood))
            if hits * 2 > n or (mood in (Mood.ZOOMIES, Mood.PROUD) and conds.get(mood)):
                wanted = mood
                break
        if wanted != self.current and mono - self.since >= self.dwell_s:
            self.current = wanted
            self.since = mono
        return self.current

    @staticmethod
    def label(mood: Mood) -> tuple[str, str]:
        return LABELS[mood]
