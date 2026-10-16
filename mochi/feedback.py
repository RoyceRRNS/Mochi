"""User feedback: "I meant to do that", "Not a problem" and "new normal" (FB-1..4, DET-13).

"I meant to do that" does three things:
1. Replies in Mochi's voice, naming the app (FB-1).
2. Removes the whole episode from the baseline. Because baseline updates are committed
   with a delay, dropping the pending updates inside the episode gives exactly the
   baseline that would exist if the episode never happened (FB-2).
3. Creates a suppression scoped to (app, metric, activity) that expires after 30 days.
   Uh-oh still gets through, and the user can undo it in Settings (FB-3).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable

from .alerts import Episode
from .baseline import BaselineStore
from .config import Config


@dataclass
class Suppression:
    id: int
    app: str
    metric: str
    activity: str
    created_ts: float
    expires_ts: float
    active: bool = True
    label: str = ""

    def matches(self, app: str, metric: str, activity: str, ts: float) -> bool:
        return (self.active and ts < self.expires_ts and self.app in (app, "*")
                and self.metric in (metric, "*") and self.activity in (activity, "*"))


class FeedbackManager:
    def __init__(self, cfg: Config, baselines: BaselineStore,
                 persist: Callable[[Suppression], None] | None = None) -> None:
        self.cfg = cfg
        self.baselines = baselines
        self.persist = persist or (lambda s: None)
        self.suppressions: dict[int, Suppression] = {}
        self._ids = itertools.count(1)

    def load(self, items: list[Suppression]) -> None:
        for s in items:
            self.suppressions[s.id] = s
        if self.suppressions:
            self._ids = itertools.count(max(self.suppressions) + 1)

    # ----------------------------------------------------------------- actions
    def intentional(self, ep: Episode, ts: float) -> str:
        culprit = ep.diagnosis.culprit
        app = culprit.app if culprit else "*"
        name = culprit.display if culprit else "that"
        ep.response, ep.response_ts = "intentional", ts
        # The streak began before the episode opened: exclude from its start.
        start = ep.opened_ts - max(self.cfg.sustain_seconds().values())
        ep.exclusion_id = self.baselines.exclude(start, ep.closed_ts)
        for metric in sorted(ep.metrics or {ep.diagnosis.metric}):
            existing = next((s for s in self.suppressions.values()
                             if s.active and s.app == app and s.metric == metric and s.activity == ep.activity), None)
            if existing:
                existing.expires_ts = ts + self.cfg.suppression_days * 86400
                self.persist(existing)
                continue
            s = Suppression(next(self._ids), app, metric, ep.activity, ts, ts + self.cfg.suppression_days * 86400,
                            True, f"{name} · {metric} · {ep.activity}")
            self.suppressions[s.id] = s
            self.persist(s)
        if culprit:
            return f"Got it, {name} is meant to be busy. I'll stay quiet about it."
        return "Got it, that was on purpose. I'll stay quiet about it."

    def episode_closed(self, ep: Episode) -> None:
        if ep.exclusion_id is not None and ep.closed_ts is not None:
            self.baselines.close_exclusion(ep.exclusion_id, ep.closed_ts)

    def not_a_problem(self, ep: Episode, ts: float) -> str:
        ep.response, ep.response_ts = "not_a_problem", ts
        return "Thanks, I'll note that this one wasn't a problem."

    def new_normal(self, ep: Episode, slot: int, ts: float) -> str:
        for metric in ep.metrics or {ep.diagnosis.metric}:
            if metric in ("disk_free",):
                continue
            self.baselines.reset_chain(slot, ep.activity, metric)
        ep.response, ep.response_ts = "new_normal", ts
        return "Okay! I'll learn this as your new normal."

    # ------------------------------------------------------------------ queries
    def is_suppressed(self, ep: Episode, ts: float) -> bool:
        culprit = ep.diagnosis.culprit
        app = culprit.app if culprit else "*"
        metrics = ep.metrics or {ep.diagnosis.metric}
        return any(s.matches(app, m, ep.activity, ts) for s in self.suppressions.values() for m in metrics)

    def active(self, ts: float) -> list[Suppression]:
        return sorted((s for s in self.suppressions.values() if s.active and ts < s.expires_ts),
                      key=lambda s: -s.created_ts)

    def remove(self, sid: int) -> bool:
        s = self.suppressions.get(sid)
        if s is None or not s.active:
            return False
        s.active = False
        self.persist(s)
        return True
