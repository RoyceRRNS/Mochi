"""The alert lifecycle: one episode per (cause, culprit) (ALR-1..8, NOT-4).

Fixes critique problem 4: the old pseudocode re-notified every 5 minutes and produced
three alerts for one memory hog. Here an episode opens once, escalates when it gets
worse, closes after 2 quiet minutes, and stays quiet during a 30-minute cooldown.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .config import Config
from .detector import Signal
from .diagnosis import DiagContext, Diagnosis, DiagnosisEngine
from .model import Level


@dataclass
class Episode:
    id: int
    key: tuple[str, str]
    diagnosis: Diagnosis
    level: Level
    max_level: Level
    opened_ts: float
    updated_ts: float
    last_active_mono: float
    last_active_ts: float
    activity: str
    bucket_level: str | None
    z: float | None
    metrics: set[str] = field(default_factory=set)
    closed_ts: float | None = None
    response: str | None = None          # intentional | not_a_problem | action | dismissed
    response_ts: float | None = None
    notified: dict[int, str] = field(default_factory=dict)  # level -> how (popup/badge/held/inbox/suppressed)
    dismissed_ts: float | None = None
    dismissed_level: Level = Level.NONE
    suppressed: bool = False
    exclusion_id: int | None = None
    culprit_seen_ts: float | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_ts is None

    @property
    def rule(self) -> str:
        return self.diagnosis.rule


@dataclass
class AlertEvent:
    kind: str          # opened | escalated | reopened | updated | closed | summary
    episode: Episode | None
    notify: str = "none"   # popup | badge | held | inbox | suppressed | none
    held: list[Episode] = field(default_factory=list)


@dataclass
class QuietState:
    snoozed_until: float = 0.0
    fullscreen: bool = False
    dnd: bool = False

    def snoozed(self, ts: float) -> bool:
        return ts < self.snoozed_until

    def shh(self) -> bool:
        return self.fullscreen or self.dnd


class AlertManager:
    def __init__(self, cfg: Config, diagnoser: DiagnosisEngine | None = None,
                 is_suppressed: Callable[[Episode, float], bool] | None = None,
                 persist: Callable[[Episode], None] | None = None) -> None:
        self.cfg = cfg
        self.diagnoser = diagnoser or DiagnosisEngine()
        self.is_suppressed = is_suppressed or (lambda ep, ts: False)
        self.persist = persist or (lambda ep: None)
        self.open: dict[tuple[str, str], Episode] = {}
        self.recent: deque[Episode] = deque(maxlen=200)
        self._closed_by_key: dict[tuple[str, str], Episode] = {}
        self._ids = itertools.count(1)
        self._diag_cache: dict[tuple, tuple[float, Diagnosis]] = {}
        self.held: list[Episode] = []
        self.popups_today: tuple[int, int] = (0, 0)   # (day ordinal, count)
        self.badge_count = 0

    def set_next_id(self, n: int) -> None:
        self._ids = itertools.count(n)

    # ------------------------------------------------------------------ helpers
    def _diagnose(self, sig: Signal, ctx: DiagContext, now_mono: float) -> Diagnosis:
        cached = self._diag_cache.get(sig.key)
        if cached and now_mono - cached[0] < 60.0:
            return cached[1]
        d = self.diagnoser.diagnose(sig, ctx)
        self._diag_cache[sig.key] = (now_mono, d)
        return d

    def _popup_budget_ok(self, ts: float) -> bool:
        day = datetime.fromtimestamp(ts).date().toordinal()
        d, n = self.popups_today
        if d != day:
            self.popups_today = (day, 0)
            n = 0
        return n < self.cfg.daily_popup_cap

    def _count_popup(self, ts: float) -> None:
        day = datetime.fromtimestamp(ts).date().toordinal()
        d, n = self.popups_today
        self.popups_today = (day, (n if d == day else 0) + 1)

    def _decide(self, ep: Episode, level: Level, ts: float, quiet: QuietState) -> str:
        if self.is_suppressed(ep, ts) and level < Level.UH_OH:
            ep.suppressed = True
            return "suppressed"
        if level == Level.PSST:
            return "badge"
        if level == Level.UH_OH:
            if ep.dismissed_ts is not None and level <= ep.dismissed_level and ts - ep.dismissed_ts < self.cfg.uh_oh_repeat_s:
                return "inbox"
            if quiet.snoozed(ts) and not self.cfg.uh_oh_breaks_snooze:
                return "held"
            return "popup"
        # Heads up
        if quiet.snoozed(ts) or quiet.shh():
            return "held"
        if not self._popup_budget_ok(ts):
            return "inbox"
        return "popup"

    def _notify(self, ep: Episode, level: Level, ts: float, quiet: QuietState) -> str:
        how = self._decide(ep, level, ts, quiet)
        ep.notified[int(level)] = how
        if how == "popup" and level == Level.HEADS_UP:
            self._count_popup(ts)
        if how == "held" and ep not in self.held:
            self.held.append(ep)
        if how in ("badge", "inbox", "held"):
            self.badge_count += 1
        return how

    # --------------------------------------------------------------------- main
    def update(self, signals: list[Signal], ctx: DiagContext, ts: float, mono: float,
               quiet: QuietState | None = None) -> list[AlertEvent]:
        quiet = quiet or QuietState()
        events: list[AlertEvent] = []
        grouped: dict[tuple[str, str], tuple[Level, Diagnosis, list[Signal]]] = {}
        for sig in signals:
            d = self._diagnose(sig, ctx, mono)
            k = d.key
            if k in grouped:
                lvl, d0, sigs = grouped[k]
                sigs.append(sig)
                best = d if d.rank < d0.rank else d0   # the most specific explanation wins
                grouped[k] = (max(lvl, sig.level), best, sigs)
            else:
                grouped[k] = (sig.level, d, [sig])

        for key, (level, diag, sigs) in grouped.items():
            ep = self.open.get(key)
            z = max((s.z for s in sigs if s.z is not None), default=None)
            bucket_level = next((s.bucket_level for s in sigs if s.bucket_level), None)
            if ep is not None:
                ep.metrics.update(s.metric for s in sigs)
                ep.last_active_mono, ep.last_active_ts = mono, ts
                ep.diagnosis = diag
                ep.updated_ts = ts
                if level > ep.level:
                    ep.level = level
                    if level > ep.max_level:
                        ep.max_level = level
                        how = self._notify(ep, level, ts, quiet)
                        events.append(AlertEvent("escalated", ep, how))
                    self.persist(ep)
                continue
            recent = self._closed_by_key.get(key)
            if recent is not None and ts - (recent.closed_ts or 0) < self.cfg.cooldown_s:
                # Cooldown (ALR-4): reopen quietly unless it got worse.
                ep = recent
                ep.closed_ts = None
                ep.level = level
                ep.last_active_mono, ep.last_active_ts, ep.updated_ts = mono, ts, ts
                ep.diagnosis = diag
                ep.metrics.update(s.metric for s in sigs)
                self.open[key] = ep
                del self._closed_by_key[key]
                if level > ep.max_level:
                    ep.max_level = level
                    how = self._notify(ep, level, ts, quiet)
                    events.append(AlertEvent("escalated", ep, how))
                else:
                    events.append(AlertEvent("reopened", ep, "none"))
                self.persist(ep)
                continue
            ep = Episode(next(self._ids), key, diag, level, level, ts, ts, mono, ts, ctx.activity, bucket_level, z,
                         {s.metric for s in sigs})
            self.open[key] = ep
            how = self._notify(ep, level, ts, quiet)
            events.append(AlertEvent("opened", ep, how))
            self.persist(ep)

        # Close episodes that have been quiet long enough (ALR-3).
        for key in [k for k, ep in self.open.items() if k not in grouped and mono - ep.last_active_mono >= self.cfg.close_after_s]:
            ep = self.open.pop(key)
            ep.closed_ts = ts
            self._closed_by_key[key] = ep
            self.recent.appendleft(ep)
            self.persist(ep)
            events.append(AlertEvent("closed", ep))
        # Forget cooldowns that have expired.
        for key in [k for k, ep in self._closed_by_key.items() if ts - (ep.closed_ts or 0) > self.cfg.cooldown_s]:
            del self._closed_by_key[key]

        # Deliver held alerts once Shh/snooze ends (ALR-7).
        if self.held and not quiet.shh() and not quiet.snoozed(ts):
            held, self.held = self.held, []
            events.append(AlertEvent("summary", None, "popup", held))
        return events

    # ----------------------------------------------------------------- queries
    def active(self) -> list[Episode]:
        return sorted(self.open.values(), key=lambda e: (-int(e.level), -e.opened_ts))

    def all_recent(self, n: int = 50) -> list[Episode]:
        eps = list(self.open.values()) + [e for e in self.recent if e.closed_ts is not None]
        seen: dict[int, Episode] = {}
        for e in eps:
            seen.setdefault(e.id, e)
        return sorted(seen.values(), key=lambda e: -e.opened_ts)[:n]

    def find(self, episode_id: int) -> Episode | None:
        for e in list(self.open.values()) + list(self.recent):
            if e.id == episode_id:
                return e
        return None

    def dismiss(self, episode_id: int, ts: float) -> Episode | None:
        ep = self.find(episode_id)
        if ep is not None:
            ep.dismissed_ts = ts
            ep.dismissed_level = ep.level
            if ep.response is None:
                ep.response, ep.response_ts = "dismissed", ts
            self.persist(ep)
        return ep

    def clear_badge(self) -> None:
        self.badge_count = 0
