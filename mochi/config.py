"""Every tunable in one place (LIF-9).

The config file is JSON. A bad value never crashes Mochi: it falls back to the default
and a human-readable warning is recorded so Settings can show it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .model import Level

# (psst, heads up, uh-oh) z thresholds per sensitivity step (DET-11).
SENSITIVITY_STEPS: list[tuple[str, tuple[float, float, float]]] = [
    ("Chill", (2.5, 3.5, 4.5)),
    ("Relaxed", (2.25, 3.25, 4.25)),
    ("Balanced", (2.0, 3.0, 4.0)),
    ("Alert", (1.75, 2.75, 3.75)),
    ("Watchful", (1.5, 2.5, 3.5)),
]

THEMES = ("system", "light", "dark")
REDUCE_MOTION = ("system", "on", "off")

DEFAULT_LEAK_IGNORE = ["system", "registry", "memory compression", "secure system", "msmpeng.exe"]
DEFAULT_GROW_BY_DESIGN = [
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "code.exe", "devenv.exe", "idea64.exe", "pycharm64.exe", "sqlservr.exe",
]

# Demo profile (DEMO-2): shorter waits so the 5-minute demo script fits.
DEMO_OVERRIDES: dict[str, Any] = {
    "psst_s": 20.0,
    "heads_up_s": 45.0,
    "uh_oh_s": 60.0,
    "close_after_s": 30.0,
    "runaway_s": 45.0,
    "disk_io_s": 30.0,
    "leak_fast_window_min": 3.0,
    "leak_fast_growth_mb": 150.0,
    "trend_eval_s": 60.0,
    "cooldown_s": 120.0,
}


@dataclass
class Config:
    # --- collection -------------------------------------------------------
    sample_interval_s: float = 5.0
    top_n: int = 15
    app_snapshot_s: float = 60.0
    queue_size: int = 120
    battery_interval_s: float = 5.0   # PERF-5: can be raised to 10 on battery

    # --- baselines --------------------------------------------------------
    alpha: float = 0.01
    min_samples: int = 30
    min_days: int = 2
    seed_guard_samples: int = 10
    guard_z: float = 2.0
    commit_delay_s: float = 1200.0
    new_normal_days: int = 3
    cold_start_days: float = 3.0

    # --- detection --------------------------------------------------------
    sensitivity: int = 2
    sustain_frac: float = 0.8
    psst_s: float = 120.0
    heads_up_s: float = 300.0
    uh_oh_s: float = 300.0
    gap_s: float = 15.0
    resume_grace_s: float = 180.0
    login_grace_s: float = 300.0
    trend_eval_s: float = 300.0

    # --- absolute limits --------------------------------------------------
    disk_full_pct: float = 5.0
    disk_full_gb: float = 5.0
    disk_low_pct: float = 10.0
    disk_low_gb: float = 20.0
    tjmax_c: float = 100.0
    temp_margin_c: float = 5.0
    ram_critical_avail_pct: float = 5.0

    # --- diagnosis rules --------------------------------------------------
    runaway_s: float = 300.0
    runaway_core_pct: float = 90.0
    runaway_total_pct: float = 50.0
    disk_io_s: float = 120.0
    disk_busy_pct: float = 90.0
    disk_latency_ms: float = 50.0
    paging_k: float = 3.0
    paging_abs: float = 500.0
    leak_fast_window_min: float = 15.0
    leak_fast_growth_mb: float = 500.0
    leak_slow_window_min: float = 60.0
    leak_rate_mb_h: float = 100.0
    leak_r2: float = 0.8
    leak_max_drop: float = 0.10
    leak_ignore: list[str] = field(default_factory=lambda: list(DEFAULT_LEAK_IGNORE))
    grow_by_design: list[str] = field(default_factory=lambda: list(DEFAULT_GROW_BY_DESIGN))
    grow_by_design_factor: float = 3.0
    idle_minutes: float = 10.0
    throttle_limit_pct: float = 85.0

    # --- alerts -----------------------------------------------------------
    close_after_s: float = 120.0
    cooldown_s: float = 1800.0
    daily_popup_cap: int = 5
    uh_oh_breaks_snooze: bool = True
    uh_oh_repeat_s: float = 86400.0
    suppression_days: float = 30.0
    heads_up_popup_s: float = 12.0

    # --- mood -------------------------------------------------------------
    mood_window_s: float = 15.0
    mood_dwell_s: float = 30.0
    proud_s: float = 600.0

    # --- benchmarks and check-up -----------------------------------------
    bench_reps: int = 3
    bench_disk_mb: int = 256
    bench_daily_days: int = 14
    bench_ref_runs: int = 10
    bench_min_free_gb: float = 2.0
    health_suggest_below: float = 80.0
    checkup_decline_days: float = 7.0
    startup_settle_pct: float = 5.0

    # --- retention --------------------------------------------------------
    raw_days: float = 14.0
    minute_days: float = 90.0

    # --- user interface ---------------------------------------------------
    theme: str = "system"
    roasts: bool = False
    autostart: bool = False
    reduce_motion: str = "system"
    demo_mode: bool = False
    onboarded: bool = False
    nerd_stats: bool = False
    start_hidden: bool = False

    # --------------------------------------------------------------------
    def z_thresholds(self) -> dict[Level, float]:
        _, (p, h, u) = SENSITIVITY_STEPS[self.sensitivity]
        return {Level.PSST: p, Level.HEADS_UP: h, Level.UH_OH: u}

    def sustain_seconds(self) -> dict[Level, float]:
        return {Level.PSST: self.psst_s, Level.HEADS_UP: self.heads_up_s, Level.UH_OH: self.uh_oh_s}

    @property
    def sensitivity_name(self) -> str:
        return SENSITIVITY_STEPS[self.sensitivity][0]

    def effective(self) -> "Config":
        """The values the engine should use right now (demo profile applied)."""
        if not self.demo_mode:
            return self
        return replace(self, **DEMO_OVERRIDES)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Allowed ranges for numeric settings. Anything outside falls back to the default.
RANGES: dict[str, tuple[float, float]] = {
    "sample_interval_s": (1, 60), "top_n": (1, 100), "app_snapshot_s": (10, 600), "queue_size": (10, 10000),
    "battery_interval_s": (1, 60),
    "alpha": (0.0001, 0.5), "min_samples": (5, 100000), "min_days": (1, 30), "seed_guard_samples": (1, 1000),
    "guard_z": (0.5, 10), "commit_delay_s": (0, 86400), "new_normal_days": (1, 60), "cold_start_days": (0, 60),
    "sensitivity": (0, len(SENSITIVITY_STEPS) - 1), "sustain_frac": (0.5, 1.0),
    "psst_s": (5, 3600), "heads_up_s": (5, 3600), "uh_oh_s": (5, 3600), "gap_s": (6, 600),
    "resume_grace_s": (0, 3600), "login_grace_s": (0, 3600), "trend_eval_s": (10, 3600),
    "disk_full_pct": (0.5, 50), "disk_full_gb": (0.5, 500), "disk_low_pct": (1, 60), "disk_low_gb": (1, 2000),
    "tjmax_c": (60, 125), "temp_margin_c": (0, 30), "ram_critical_avail_pct": (0.5, 50),
    "runaway_s": (10, 7200), "runaway_core_pct": (10, 1000), "runaway_total_pct": (5, 100),
    "disk_io_s": (10, 3600), "disk_busy_pct": (10, 100), "disk_latency_ms": (1, 10000), "paging_k": (1, 100),
    "paging_abs": (1, 1e6), "leak_fast_window_min": (1, 600), "leak_fast_growth_mb": (10, 1e6),
    "leak_slow_window_min": (5, 1440), "leak_rate_mb_h": (1, 1e6), "leak_r2": (0, 1), "leak_max_drop": (0, 1),
    "grow_by_design_factor": (1, 100), "idle_minutes": (1, 240), "throttle_limit_pct": (10, 100),
    "close_after_s": (5, 3600), "cooldown_s": (0, 86400 * 7), "daily_popup_cap": (0, 1000),
    "uh_oh_repeat_s": (60, 86400 * 30), "suppression_days": (1, 365), "heads_up_popup_s": (2, 600),
    "mood_window_s": (5, 300), "mood_dwell_s": (0, 600), "proud_s": (10, 86400),
    "bench_reps": (1, 10), "bench_disk_mb": (16, 4096), "bench_daily_days": (1, 90), "bench_ref_runs": (1, 100),
    "bench_min_free_gb": (0.1, 100), "health_suggest_below": (1, 100), "checkup_decline_days": (0, 90),
    "startup_settle_pct": (0.5, 100),
    "raw_days": (1, 365), "minute_days": (1, 3650),
}
CHOICES: dict[str, tuple[str, ...]] = {"theme": THEMES, "reduce_motion": REDUCE_MOTION}


@dataclass
class LoadResult:
    config: Config
    warnings: list[str]


def _coerce(name: str, value: Any, default: Any) -> tuple[bool, Any, str | None]:
    """Return (ok, coerced_value, warning)."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return True, value, None
        return False, default, f"'{name}' should be true or false; using {default}."
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            return False, default, f"'{name}' should be a whole number; using {default}."
        value = int(value)
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, default, f"'{name}' should be a number; using {default}."
        value = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str):
            return False, default, f"'{name}' should be text; using '{default}'."
        if name in CHOICES and value not in CHOICES[name]:
            return False, default, f"'{name}' must be one of {', '.join(CHOICES[name])}; using '{default}'."
        return True, value, None
    elif isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return False, default, f"'{name}' should be a list of names; using the default list."
        return True, [v.lower() for v in value], None
    if name in RANGES:
        lo, hi = RANGES[name]
        if not (lo <= value <= hi):
            return False, default, f"'{name}' = {value} is outside {lo}..{hi}; using {default}."
    return True, value, None


def from_dict(data: Any) -> LoadResult:
    cfg = Config()
    warnings: list[str] = []
    if not isinstance(data, dict):
        return LoadResult(cfg, ["The settings file isn't a JSON object; using defaults."])
    known = {f.name: f for f in fields(Config)}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"Unknown setting '{key}' was ignored.")
            continue
        ok, coerced, warn = _coerce(key, value, getattr(cfg, key))
        if warn:
            warnings.append(warn)
        if ok:
            setattr(cfg, key, coerced)
    return LoadResult(cfg, warnings)


def load(path: Path) -> LoadResult:
    if not path.exists():
        return LoadResult(Config(), [])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return LoadResult(Config(), [f"Couldn't read settings ({exc.__class__.__name__}); using defaults."])
    return from_dict(data)


def save(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
