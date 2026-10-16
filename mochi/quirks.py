"""The fun parts, each backed by a real metric (QRK-1..8) [stretch].

Weather report, OS fact of the day, Feed Mochi, gentle roasts, achievements, calm
streaks, Mochi's diary and personal records.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from .model import GB, MB, App, Sample, fmt_bytes

SKY = {0: "Mostly sunny", 1: "Partly cloudy", 2: "Scattered showers", 3: "Stormy"}
SKY_EMOJI_FREE = SKY  # text only; the UI draws its own icons


def day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day).timestamp()


def human_count(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f} billion"
    if n >= 1e6:
        return f"{n / 1e6:.1f} million"
    if n >= 1e3:
        return f"{n / 1e3:.0f} thousand"
    return f"{n:.0f}"


def data_days(storage, now: float, lookback_days: int = 14) -> list[float]:
    """Start-of-day timestamps of days with at least one hour of data."""
    since = day_start(now) - lookback_days * 86400
    rows = storage.query(
        "SELECT CAST((ts - ?) / 86400 AS INTEGER) AS d, COUNT(*) FROM samples WHERE ts >= ? GROUP BY d", (since, since))
    out = {since + int(d) * 86400: n * 5 for d, n in rows}
    rows = storage.query(
        "SELECT CAST((ts - ?) / 86400 AS INTEGER) AS d, COUNT(*) FROM agg_minute WHERE ts >= ? GROUP BY d", (since, since))
    for d, n in rows:
        key = since + int(d) * 86400
        out[key] = max(out.get(key, 0), n * 60)
    return sorted(k for k, secs in out.items() if secs >= 3600)


# ------------------------------------------------------------------- weather
def weather(storage, now: float, min_days: int = 3) -> str | None:
    """One line each morning from yesterday's alerts and evening memory history (QRK-2)."""
    days = [d for d in data_days(storage, now) if d < day_start(now)]
    if len(days) < min_days:
        return None
    today = day_start(now)
    y0, y1 = today - 86400, today
    worst = storage.scalar("SELECT MAX(max_level) FROM alerts WHERE opened_ts >= ? AND opened_ts < ?", (y0, y1), 0)
    sky = SKY.get(int(worst or 0), "Mostly sunny")
    tops: Counter = Counter()
    names: dict[str, str] = {}
    considered = 0
    for d in days[-14:]:
        rows = storage.query(
            "SELECT app, display, AVG(mem_mb) AS m FROM app_mem WHERE ts >= ? AND ts < ? GROUP BY app ORDER BY m DESC LIMIT 1",
            (d + 17 * 3600, d + 22 * 3600))
        if rows:
            considered += 1
            tops[rows[0][0]] += 1
            names[rows[0][0]] = rows[0][1]
    if not considered:
        return f"{sky} today."
    app, count = tops.most_common(1)[0]
    pct = round(100 * count / considered / 10) * 10
    return f"{sky}, with a {pct}% chance of {names[app]} by evening."


# ----------------------------------------------------------------- OS fact
def os_fact(storage, now: float) -> dict | None:
    """Today's context switches and interrupts from the kernel's own counters (QRK-3).

    The counters are cumulative since boot; a drop means a reboot, so we start a new
    segment instead of subtracting.
    """
    rows = storage.query("SELECT ctx, irq, syscalls FROM samples WHERE ts >= ? AND ctx IS NOT NULL ORDER BY ts",
                         (day_start(now),))
    if len(rows) < 2:
        return None
    totals = [0, 0, 0]
    prev = rows[0]
    for row in rows[1:]:
        for i in range(3):
            a, b = prev[i], row[i]
            if a is not None and b is not None and b >= a:
                totals[i] += b - a
        prev = row
    ctx, irq, sysc = totals
    text = (f"Today your CPU switched between tasks {human_count(ctx)} times and handled "
            f"{human_count(irq)} interrupts.")
    return {"ctx_switches": ctx, "interrupts": irq, "syscalls": sysc, "text": text}


# ---------------------------------------------------------------- feed Mochi
SNACKS = [(2 * GB, "a whole dango"), (512 * MB, "a mochi bite"), (0, "a rice crumb")]


def snack_for(freed: int) -> str:
    for limit, name in SNACKS:
        if freed >= limit:
            return name
    return "a rice crumb"


@dataclass
class FeedEvent:
    app: str
    display: str
    freed_bytes: int
    snack: str
    ts: float

    @property
    def text(self) -> str:
        return f"Yum! Closing {self.display} freed {fmt_bytes(self.freed_bytes)}. That's {self.snack}!"


class FeedTracker:
    """Credit a snack only when an app named in a tip exits within 10 minutes of it (QRK-4)."""

    def __init__(self, window_s: float = 600.0) -> None:
        self.window_s = window_s
        self.tips: dict[str, tuple[float, int, str]] = {}

    def note_tip(self, app: str, display: str, ts: float, private_bytes: int) -> None:
        self.tips[app] = (ts, private_bytes, display)

    def observe(self, ts: float, apps: dict[str, App]) -> list[FeedEvent]:
        out = []
        for app, (tip_ts, last_bytes, display) in list(self.tips.items()):
            if ts - tip_ts > self.window_s:
                del self.tips[app]
                continue
            cur = apps.get(app)
            if cur is not None:
                self.tips[app] = (tip_ts, cur.private_bytes, display)
                continue
            del self.tips[app]
            out.append(FeedEvent(app, display, last_bytes, snack_for(last_bytes), ts))
        return out


# -------------------------------------------------------------------- roasts
def roast(sample: Sample, enabled: bool, uptime_s: float | None = None) -> str | None:
    """Off by default; only measurable facts (QRK-5). Chrome's process count is not a tab count."""
    if not enabled:
        return None
    browsers = [a for a in sample.apps.values() if a.exe_name.lower() in ("chrome.exe", "msedge.exe", "firefox.exe")]
    for b in sorted(browsers, key=lambda a: -a.proc_count):
        if b.proc_count >= 40:
            return f"{b.display} is running {b.proc_count} processes. Are you collecting them?"
    if sample.sys.ram_total:
        used = 100 - sample.sys.ram_available_pct
        if used >= 85:
            return f"Your memory is {used:.0f}% full. I'm basically a dumpling now."
    if uptime_s is not None and uptime_s > 7 * 86400:
        return f"It's been {uptime_s / 86400:.0f} days since your last restart. Even I nap sometimes."
    return None


# -------------------------------------------------------------- achievements
@dataclass
class Achievement:
    key: str
    title: str
    description: str
    unlocked_ts: float | None = None
    hidden: bool = False
    progress: str = ""


ACHIEVEMENTS = [
    ("tab_tamer", "Tab Tamer", "No memory alerts for 7 days (with at least 5 active days)."),
    ("cool_cucumber", "Cool Cucumber", "No throttling for 7 days (with at least 5 active days)."),
    ("clean_sweep", "Clean Sweep", "Freed 10 GB of disk space."),
    ("speed_demon", "Speed Demon", "A new fastest boot."),
    ("calm_week", "Calm Week", "Seven calm days in a row (no Uh-oh)."),
    ("well_fed", "Well Fed", "Fed Mochi 5 snacks by closing hogs."),
]


def evaluate_achievements(storage, now: float, throttle_metric_available: bool,
                          boot_measured: bool) -> list[Achievement]:
    unlocked = dict(storage.query("SELECT key, unlocked_ts FROM achievements"))
    week = now - 7 * 86400
    active_days = len([d for d in data_days(storage, now, 7) if d >= day_start(week)])
    out: list[Achievement] = []
    for key, title, desc in ACHIEVEMENTS:
        a = Achievement(key, title, desc, unlocked.get(key))
        if key == "tab_tamer":
            n = storage.scalar("SELECT COUNT(*) FROM alerts WHERE opened_ts >= ? AND (rule IN ('memory_pressure','leak') "
                               "OR metrics LIKE '%ram%' OR metrics LIKE '%paging%')", (week,), 0)
            earned = active_days >= 5 and n == 0
            a.progress = f"{active_days}/5 active days, {n} memory alerts this week"
        elif key == "cool_cucumber":
            a.hidden = not throttle_metric_available
            n = storage.scalar("SELECT COUNT(*) FROM alerts WHERE opened_ts >= ? AND rule='throttling'", (week,), 0)
            earned = not a.hidden and active_days >= 5 and n == 0
            a.progress = f"{active_days}/5 active days, {n} throttling alerts this week"
        elif key == "clean_sweep":
            freed = sum(d.get("gb", 0.0) for _, d in storage.events("disk_freed"))
            earned = freed >= 10.0
            a.progress = f"{freed:.1f} / 10 GB freed"
        elif key == "speed_demon":
            a.hidden = not boot_measured
            boots = [d["duration_s"] for _, d in storage.events("boot")]
            earned = len(boots) >= 2 and boots[-1] <= min(boots[:-1])
            a.progress = f"{len(boots)} boots measured"
        elif key == "calm_week":
            streak = calm_streak(storage, now)
            earned = streak >= 7
            a.progress = f"{streak}/7 calm days"
        else:  # well_fed
            feeds = len(storage.events("feed"))
            earned = feeds >= 5
            a.progress = f"{feeds}/5 snacks"
        if earned and a.unlocked_ts is None:
            a.unlocked_ts = now
            storage.execute("INSERT OR IGNORE INTO achievements(key, unlocked_ts) VALUES (?, ?)", (key, now))
        out.append(a)
    return out


# ---------------------------------------------------------------- calm streak
def calm_streak(storage, now: float, lookback_days: int = 60) -> int:
    """Days in a row without an Uh-oh; days with < 1 h of data neither count nor break it (QRK-7)."""
    with_data = set(data_days(storage, now, lookback_days))
    streak = 0
    d = day_start(now)
    for _ in range(lookback_days + 1):
        if d in with_data:
            bad = storage.scalar("SELECT COUNT(*) FROM alerts WHERE max_level >= 3 AND opened_ts >= ? AND opened_ts < ?",
                                 (d, d + 86400), 0)
            if bad:
                break
            streak += 1
        d -= 86400
    return streak


# ---------------------------------------------------------------------- diary
RULE_FEELINGS = {
    "memory_pressure": "very dizzy", "leak": "a bit puffy", "memory_hog": "very puffy", "runaway": "out of breath", "disk_io": "sluggish",
    "throttling": "sweaty", "storage": "grumpy", "idle_cpu": "restless", "battery": "drained", "unknown": "uneasy",
}


@dataclass
class DiaryEntry:
    week_start: float
    title: str
    text: str


def diary(storage, now: float, weeks: int = 4) -> list[DiaryEntry]:
    """Weekly entries in Mochi's voice, built offline from templates (QRK-8)."""
    out: list[DiaryEntry] = []
    today = datetime.fromtimestamp(day_start(now))
    monday = today - timedelta(days=today.weekday())
    for w in range(weeks):
        start = (monday - timedelta(weeks=w)).timestamp()
        end = start + 7 * 86400
        days = [d for d in data_days(storage, min(end, now), 30) if start <= d < end]
        if not days:
            continue
        alerts = storage.alerts_between(start, end)
        feeds = [d for t, d in storage.events("feed", start) if t < end]
        title = ("This week so far" if w == 0 else
                 f"Week of {datetime.fromtimestamp(start).strftime('%d %b').lstrip('0')}")
        lines: list[str] = []
        if not alerts:
            lines.append("What a calm week! Nothing bothered me at all. I mostly napped.")
        else:
            by_day = Counter(datetime.fromtimestamp(a["opened_ts"]).strftime("%A") for a in alerts)
            worst_day, n = by_day.most_common(1)[0]
            day_alerts = [a for a in alerts if datetime.fromtimestamp(a["opened_ts"]).strftime("%A") == worst_day]
            culprits = [a["culprit_display"] for a in day_alerts if a["culprit_display"]]
            feeling = RULE_FEELINGS.get(Counter(a["rule"] for a in day_alerts).most_common(1)[0][0], "uneasy")
            if len(set(culprits)) >= 2:
                c = Counter(culprits).most_common(2)
                lines.append(f"{worst_day} was rough. {c[0][0]} and {c[1][0]} showed up together and I got {feeling}.")
            elif culprits:
                lines.append(f"{worst_day} was rough. {culprits[0]} kept me busy and I got {feeling}.")
            else:
                lines.append(f"{worst_day} was rough and I got {feeling}.")
            calm = len(days) - len(by_day)
            if calm > 0:
                lines.append(f"The other {calm} day{'s' if calm != 1 else ''} were calm.")
            intentional = sum(1 for a in alerts if a["response"] == "intentional")
            if intentional:
                lines.append(f"You told me {intentional} of those were on purpose, so I've learned a little.")
        if feeds:
            freed = sum(f.get("bytes", 0) for f in feeds)
            lines.append(f"I got {len(feeds)} snack{'s' if len(feeds) != 1 else ''} ({fmt_bytes(freed)} freed). Yum!")
        out.append(DiaryEntry(start, title, " ".join(lines)))
    return out


# ------------------------------------------------------------ personal records
def records(storage) -> dict[str, str]:
    out: dict[str, str] = {}
    boots = [d["duration_s"] for _, d in storage.events("boot")]
    if boots:
        out["Fastest boot"] = f"{min(boots):.0f} s"
    ups = [d["seconds"] for _, d in storage.events("uptime")]
    if ups:
        out["Longest uptime (since last restart)"] = f"{max(ups) / 86400:.1f} days"
    low = storage.scalar("SELECT MIN(ram) FROM samples WHERE activity='idle' AND ram IS NOT NULL")
    low_agg = storage.scalar("SELECT MIN(ram_avg) FROM agg_minute WHERE idle_frac > 0.9 AND ram_avg IS NOT NULL")
    vals = [v for v in (low, low_agg) if v is not None]
    if vals:
        out["Lowest idle memory"] = f"{min(vals):.1f} GB"
    fastest = storage.query("SELECT app, MIN(duration_s) FROM launches WHERE settled=1 GROUP BY app ORDER BY 2 LIMIT 1")
    if fastest and fastest[0][1] is not None:
        out["Fastest app launch"] = f"{fastest[0][0]} in {fastest[0][1]:.1f} s"
    scores = [r[0] for r in storage.query("SELECT score FROM benchmarks WHERE score IS NOT NULL")]
    if scores:
        out["Best Health Score"] = f"{max(scores):.0f}"
    return out


class BootTracker:
    """Boot duration = boot time -> first 10 s with CPU < 20% after sign-in (QRK-8).

    Measured only when Mochi starts within 3 minutes of boot (autostart). With Windows
    Fast Startup, "shut down" doesn't reset the boot time, so no new boot is measured
    until a real restart.
    """

    def __init__(self, boot_time: float, started_ts: float, already_recorded: bool) -> None:
        self.boot_time = boot_time
        self.active = not already_recorded and started_ts - boot_time <= 180.0
        self.quiet_since: float | None = None
        self.result: float | None = None

    def observe(self, ts: float, cpu: float | None) -> float | None:
        if not self.active or cpu is None:
            return None
        if cpu < 20.0:
            if self.quiet_since is None:
                self.quiet_since = ts
            if ts - self.quiet_since >= 10.0:
                self.active = False
                self.result = self.quiet_since - self.boot_time
                return self.result
        else:
            self.quiet_since = None
        if ts - self.boot_time > 900:
            self.active = False
        return None


def median_or_none(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None
