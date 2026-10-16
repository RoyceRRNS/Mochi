"""Contextual EWMA baselines (DET-1..3, DET-13, CTX-5, CTX-6, FB-2).

Fixes from the critique:
- Problem 1 (the baseline swallows the anomaly): the detector never queues an update
  while a reading is anomalous (contamination guard), so a plateau stays visible.
- Problem 9 ("I meant to do that" can't be undone): updates are committed with a delay
  (default 20 min). Marking an episode as intentional drops every pending update inside
  it, so the result equals a baseline that never saw the episode.
- Problem 15 (30 samples is thin): a bucket is trusted only after >= 30 samples spread
  across >= 2 different days; otherwise lookups fall back to coarser buckets.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from dataclasses import dataclass, field

from .buckets import bucket_chain
from .config import Config

MAX_DAYS_KEPT = 8


@dataclass
class Stat:
    n: int = 0
    mean: float = 0.0
    var: float = 0.0
    days: list[int] = field(default_factory=list)
    guarded_days: list[int] = field(default_factory=list)

    @property
    def std(self) -> float:
        return math.sqrt(max(0.0, self.var))

    def update(self, x: float, alpha: float, day: int) -> None:
        # Seeded from the first samples: alpha is never smaller than 1/(n+1), so early
        # values give a plain running mean/variance instead of decaying from zero (DET-1).
        a = max(alpha, 1.0 / (self.n + 1))
        d = x - self.mean
        self.mean += a * d
        self.var = (1.0 - a) * (self.var + a * d * d)
        self.n += 1
        if not self.days or self.days[-1] != day:
            if day not in self.days:
                self.days.append(day)
                self.days.sort()
                del self.days[:-MAX_DAYS_KEPT]

    def trusted(self, min_samples: int, min_days: int) -> bool:
        return self.n >= min_samples and len(self.days) >= min_days

    def to_json(self) -> dict:
        return {"n": self.n, "mean": self.mean, "var": self.var, "days": self.days, "guarded_days": self.guarded_days}

    @classmethod
    def from_json(cls, d: dict) -> "Stat":
        return cls(int(d["n"]), float(d["mean"]), float(d["var"]), list(d.get("days", [])), list(d.get("guarded_days", [])))


@dataclass(slots=True)
class Pending:
    ts: float
    slot: int
    activity: str
    metric: str
    value: float
    day: int


class BaselineStore:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stats: dict[tuple[str, str], Stat] = {}
        self.pending: deque[Pending] = deque()
        self.exclusions: dict[int, list[float | None]] = {}
        self._next_excl = 1
        self.lock = threading.RLock()
        self.dirty: set[tuple[str, str]] = set()
        self.events: list[tuple[str, str, str]] = []  # (kind, bucket, metric), drained by the engine

    # --- reading -----------------------------------------------------------
    def get(self, bucket: str, metric: str) -> Stat | None:
        return self.stats.get((bucket, metric))

    def lookup(self, slot: int, activity: str, metric: str) -> tuple[Stat | None, str | None, str | None]:
        """First trusted stat along the fallback chain: (stat, level name, bucket key)."""
        with self.lock:
            for level, key in bucket_chain(slot, activity):
                st = self.stats.get((key, metric))
                if st is not None and st.trusted(self.cfg.min_samples, self.cfg.min_days):
                    return st, level, key
        return None, None, None

    def provisional(self, slot: int, activity: str, metric: str) -> tuple[Stat | None, str | None]:
        """Most specific stat with enough samples to guard against contamination."""
        with self.lock:
            for _level, key in bucket_chain(slot, activity):
                st = self.stats.get((key, metric))
                if st is not None and st.n >= self.cfg.seed_guard_samples:
                    return st, key
        return None, None

    # --- writing -----------------------------------------------------------
    def queue(self, ts: float, slot: int, activity: str, metric: str, value: float, day: int) -> None:
        if self._excluded(ts):
            return
        with self.lock:
            self.pending.append(Pending(ts, slot, activity, metric, value, day))

    def commit_due(self, now_ts: float, delay: float | None = None) -> int:
        delay = self.cfg.commit_delay_s if delay is None else delay
        applied = 0
        with self.lock:
            while self.pending and self.pending[0].ts <= now_ts - delay:
                p = self.pending.popleft()
                if self._excluded(p.ts):
                    continue
                self._apply(p)
                applied += 1
        return applied

    def commit_all(self) -> int:
        return self.commit_due(float("inf"), 0.0)

    def _apply(self, p: Pending) -> None:
        for _level, key in bucket_chain(p.slot, p.activity):
            st = self.stats.get((key, p.metric))
            if st is None:
                st = self.stats[(key, p.metric)] = Stat()
            st.update(p.value, self.cfg.alpha, p.day)
            self.dirty.add((key, p.metric))

    # --- exclusions (FB-2, DET-12) ------------------------------------------
    def exclude(self, start_ts: float, end_ts: float | None = None) -> int:
        with self.lock:
            eid = self._next_excl
            self._next_excl += 1
            self.exclusions[eid] = [start_ts, end_ts]
            self.pending = deque(p for p in self.pending if not self._excluded(p.ts))
            return eid

    def close_exclusion(self, eid: int, end_ts: float) -> None:
        with self.lock:
            if eid in self.exclusions:
                self.exclusions[eid][1] = end_ts
                self.pending = deque(p for p in self.pending if not self._excluded(p.ts))

    def prune_exclusions(self, now_ts: float) -> None:
        """Closed exclusions can be forgotten once nothing pending could fall inside them."""
        with self.lock:
            horizon = now_ts - self.cfg.commit_delay_s - 60.0
            for eid in [e for e, (s, end) in self.exclusions.items() if end is not None and end < horizon]:
                del self.exclusions[eid]

    def _excluded(self, ts: float) -> bool:
        for start, end in self.exclusions.values():
            if ts >= start and (end is None or ts <= end):
                return True
        return False

    # --- new normal (DET-13) -------------------------------------------------
    def note_guarded(self, bucket: str, metric: str, day: int) -> bool:
        """Record that an anomalous value was kept out. If that keeps happening on
        ``new_normal_days`` different days within a week, accept it as the new normal."""
        with self.lock:
            st = self.stats.get((bucket, metric))
            if st is None:
                return False
            if day not in st.guarded_days:
                st.guarded_days.append(day)
                st.guarded_days = [d for d in st.guarded_days if d > day - 7]
                self.dirty.add((bucket, metric))
            if len(st.guarded_days) >= self.cfg.new_normal_days:
                self._relearn(bucket, metric)
                return True
        return False

    def _relearn(self, bucket: str, metric: str) -> None:
        old = self.stats.get((bucket, metric))
        days = list(old.days) if old else []
        # Keep the day history so the bucket becomes trusted again quickly, but forget values.
        self.stats[(bucket, metric)] = Stat(days=days[-1:])
        self.dirty.add((bucket, metric))
        self.events.append(("relearn", bucket, metric))

    def reset_chain(self, slot: int, activity: str, metric: str) -> None:
        """Manual "this is my new normal" for the specific buckets of one context."""
        with self.lock:
            for level, key in bucket_chain(slot, activity):
                if level == "global":
                    continue
                if (key, metric) in self.stats:
                    self._relearn(key, metric)

    # --- learning progress (DET-9) -------------------------------------------
    def learning_progress(self, usage_seconds: dict[tuple[int, str], float], metric: str = "cpu") -> float:
        """Share of the person's recent active time spent in trusted buckets."""
        total = sum(v for (slot, act), v in usage_seconds.items() if act != "idle")
        if total <= 0:
            return 0.0
        trusted = 0.0
        with self.lock:
            for (slot, act), secs in usage_seconds.items():
                if act == "idle":
                    continue
                st = self.stats.get((f"s{slot}|{act}", metric))
                if st is not None and st.trusted(self.cfg.min_samples, self.cfg.min_days):
                    trusted += secs
        return trusted / total

    # --- persistence -------------------------------------------------------
    def rows(self, only_dirty: bool = False) -> list[tuple[str, str, str]]:
        with self.lock:
            keys = list(self.dirty) if only_dirty else list(self.stats)
            self.dirty.clear()
            return [(b, m, json.dumps(self.stats[(b, m)].to_json())) for b, m in keys if (b, m) in self.stats]

    def load_rows(self, rows: list[tuple[str, str, str]]) -> None:
        with self.lock:
            for bucket, metric, data in rows:
                try:
                    self.stats[(bucket, metric)] = Stat.from_json(json.loads(data))
                except (ValueError, KeyError, TypeError):
                    continue
