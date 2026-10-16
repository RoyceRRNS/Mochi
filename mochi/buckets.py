"""Contextual buckets: 48 time slots x 7 activities, plus fallback levels (CTX-1, CTX-6)."""

from __future__ import annotations

from datetime import datetime, tzinfo

# Fallback order when a bucket isn't trusted yet: slot+activity -> activity -> global.
LEVELS = ("slot+activity", "activity", "global")


def local_dt(ts: float, tz: tzinfo | None = None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)


def time_slot(ts: float, tz: tzinfo | None = None) -> int:
    """Local hour of day, with weekend hours numbered 24..47."""
    dt = local_dt(ts, tz)
    return dt.hour + (24 if dt.weekday() >= 5 else 0)


def day_ordinal(ts: float, tz: tzinfo | None = None) -> int:
    return local_dt(ts, tz).date().toordinal()


def slot_label(slot: int) -> str:
    day = "weekend" if slot >= 24 else "weekday"
    hour = slot % 24
    return f"{day} {hour:02d}:00-{(hour + 1) % 24:02d}:00"


def bucket_chain(slot: int, activity: str) -> list[tuple[str, str]]:
    """[(level name, bucket key)] from most to least specific."""
    return [
        ("slot+activity", f"s{slot}|{activity}"),
        ("activity", f"*|{activity}"),
        ("global", "*|*"),
    ]
