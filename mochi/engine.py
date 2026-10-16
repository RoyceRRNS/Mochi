"""The engine: collector -> queue -> processing, plus persistence and services.

Threads (all daemon, all supervised, LIF-7):
- collector: samples every 5 s on fixed ticks into a bounded queue (producer)
- processing: detection, diagnosis, alerts, moods, storage writes (consumer)
- supervisor: restarts a dead worker and keeps a heartbeat
- storage writer: lives inside ``Storage``
- startup watcher: optional, for app launch timing

The same ``process_sample`` is used by the processing thread and by the replay harness,
so simulated end-to-end tests exercise exactly the production path.
"""

from __future__ import annotations

import copy
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import config as config_mod
from .actions import ActionResult, ActionRunner
from .activity import ActivityMapper, ActivitySmoother
from .alerts import AlertEvent, AlertManager, Episode, QuietState
from .autostart import Autostart
from .baseline import BaselineStore
from .benchmarks import BenchmarkRunner, BenchmarkScheduler, BenchResult, HealthScore, cleanup_leftovers
from .buckets import day_ordinal, time_slot
from .checkup import CheckupReport, CheckupRunner
from .collector import Collector
from .config import Config
from .detector import Detector, DetectorResult
from .diagnosis import Action, DiagContext, DiagnosisEngine
from .feedback import FeedbackManager, Suppression
from .folderscan import FolderScanner, ScanResult, scan as folder_scan
from .model import GB, MB, App, Battery, Level, Sample
from .moods import LABELS, SPEECH, Mood, MoodEngine, MoodInputs, conditions
from .paths import Paths
from .platform.base import Platform
from .quirks import (BootTracker, FeedTracker, calm_streak, diary, evaluate_achievements, os_fact, records, roast,
                     weather)
from .startup_tracker import Launch, StartupWatcher, several_apps_slow, slow_apps
from .storage import Storage
from .trends import DiskTrend, LeakTracker, RunawayTracker

log = logging.getLogger(__name__)


class SystemClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class EngineEvent:
    kind: str
    data: Any = None


@dataclass
class Notice:
    id: str
    ts: float
    title: str
    text: str
    action: str | None = None   # run_benchmark | run_checkup | open_settings
    dismissed: bool = False


@dataclass
class SampleView:
    ts: float
    metrics: dict[str, float | None]
    z: dict[str, float | None]
    bands: dict[str, tuple[float, float] | None]
    bucket_levels: dict[str, str | None]
    activity: str
    slot: int
    mood: Mood
    learning: bool
    learning_day: int
    progress: float
    cpu: float | None
    ram_used_gb: float
    ram_total_gb: float
    ram_used_pct: float
    commit_pct: float | None
    disk_busy: float | None
    perf_limit: float | None
    primary_volume: tuple[str, float, float] | None   # (mount, free GB, free %)
    volumes: list[tuple[str, float, float, float]]   # (mount, free GB, total GB, free %)
    battery: Battery | None
    apps: list[App]
    unavailable: dict[str, str]
    self_cpu: float
    self_mem_mb: float
    late_ms: float
    drops: int
    badge: int
    roast: str | None
    user_idle: bool
    fullscreen: bool
    snoozed_until: float
    demo: bool
    sensitivity: str

    @property
    def mood_label(self) -> tuple[str, str]:
        return LABELS[self.mood]

    @property
    def speech(self) -> str:
        return SPEECH[self.mood]


class Engine:
    def __init__(self, platform: Platform, paths: Paths, cfg: Config | None = None, storage: Storage | None = None,
                 *, threaded: bool = True, clock=None, tz=None, start_threads: bool = True,
                 startup_watcher: bool | None = None, bench_scale: float = 1.0, bench_disk_mb: int | None = None,
                 autostart: Autostart | None = None, profile: str = "default") -> None:
        self.paths = paths.ensure()
        self.platform = platform
        self.clock = clock or SystemClock()
        self.tz = tz
        self.profile = profile
        if cfg is None:
            loaded = config_mod.load(paths.config)
            cfg, self.config_warnings = loaded.config, loaded.warnings
        else:
            self.config_warnings = []
        self.base_cfg = cfg
        self.cfg = cfg.effective()
        self.threaded = threaded
        self.storage = storage or Storage(paths.db, threaded=threaded)
        self.mapper = ActivityMapper.load(paths.activity_map, self.cfg.idle_minutes)
        self.smoother = ActivitySmoother(60.0)
        self.baselines = BaselineStore(self.cfg)
        self.baselines.load_rows(self.storage.load_baselines())
        self.detector = Detector(self.cfg, self.baselines)
        self.collector = Collector(platform, self.cfg)
        self.leaks = LeakTracker(self.cfg)
        self.disk_trend = DiskTrend(self.cfg)
        self.runaway = RunawayTracker(self.cfg)
        self.feedback = FeedbackManager(self.cfg, self.baselines, persist=self.storage.save_suppression)
        self.feedback.load([Suppression(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7] or "")
                            for r in self.storage.load_suppressions()])
        self.diagnoser = DiagnosisEngine()
        self.alerts = AlertManager(self.cfg, self.diagnoser, is_suppressed=self.feedback.is_suppressed,
                                   persist=self.storage.upsert_alert)
        self.alerts.set_next_id(self.storage.max_alert_id() + 1)
        self.moods = MoodEngine(self.cfg.mood_window_s, self.cfg.mood_dwell_s)
        self.actions = ActionRunner(platform)
        self.feeds = FeedTracker()
        self.history: deque[Sample] = deque(maxlen=150)
        self.views: deque[SampleView] = deque(maxlen=720)
        self.listeners: list[Callable[[EngineEvent], None]] = []
        self.lock = threading.RLock()
        self.quiet = QuietState(snoozed_until=float(self.storage.get_meta("snoozed_until") or 0.0))
        first = self.storage.get_meta("first_sample_ts")
        self.first_ts: float | None = float(first) if first else None
        self.started_ts = self.clock.time()
        boot = platform.boot_time()
        self.boot = BootTracker(boot, self.started_ts, self.storage.get_meta("last_boot_recorded") == str(int(boot)))
        self.queue: queue.Queue[Sample] = queue.Queue(maxsize=self.cfg.queue_size)
        self.drops = 0
        self.errors = 0
        self.benchmark_running = False
        self.checkup_running = False
        self.self_activity_until = 0.0
        self.proud_until = 0.0
        self.trend_signals: list = []
        self.leak_map: dict[str, dict] = {}
        self._last_app_snapshot = -1e18
        self._last_trend_eval = -1e18
        self._last_baseline_save = -1e18
        self._last_hour = -1
        self._last_maintain = 0.0
        self._last_progress = -1e18
        self._last_bench_check = -1e18
        self._last_uptime_day = -1
        self._last_volumes: dict[str, float] = {}
        self.progress = 0.0
        self.latest: SampleView | None = None
        self.current_mood = Mood.HAPPY
        self.notices: dict[str, Notice] = {}
        self.health = HealthScore(self.cfg.bench_ref_runs)
        self.bench = BenchmarkRunner(self.cfg, paths.tmp, scale=bench_scale, disk_mb=bench_disk_mb)
        self.scheduler = BenchmarkScheduler(self.cfg)
        cleanup_leftovers(paths.tmp)
        self.autostart = autostart or Autostart(profile=profile)
        self.scanner = FolderScanner()
        self.last_scan: ScanResult | None = None
        self.last_checkup: CheckupReport | None = None
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self.last_sample_mono: float | None = None
        self.restarts = 0
        self.startup_watcher: StartupWatcher | None = None
        if startup_watcher is None:
            startup_watcher = threaded and platform.name == "windows"
        self._want_startup_watcher = startup_watcher
        if start_threads and threaded:
            self.start()

    # ================================================================ events
    def subscribe(self, fn: Callable[[EngineEvent], None]) -> None:
        self.listeners.append(fn)

    def _emit(self, kind: str, data: Any = None) -> None:
        ev = EngineEvent(kind, data)
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("listener failed for %s", kind)

    # =============================================================== threads
    def start(self) -> None:
        self._stop.clear()
        self._spawn("collector", self._collector_loop)
        self._spawn("processing", self._processing_loop)
        self._spawn("supervisor", self._supervisor_loop)
        if self._want_startup_watcher and self.startup_watcher is None:
            self.startup_watcher = StartupWatcher(self._on_launch, self.platform.self_pid(), self.platform.boot_time(),
                                                  self._launched_since_boot, self.cfg.startup_settle_pct)
            self.startup_watcher.start()

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=f"mochi-{name}", daemon=True)
        self._threads[name] = t
        t.start()

    def stop(self, timeout: float = 2.0) -> float:
        """Stop threads and save everything within ``timeout`` seconds (LIF-3). Safe to call twice."""
        t0 = time.monotonic()
        if self.storage.closed:
            return 0.0
        self._stop.set()
        for t in list(self._threads.values()):
            t.join(max(0.05, timeout / 3))
        if self.startup_watcher:
            self.startup_watcher.stop()
        with self.lock:
            self.baselines.commit_all()
            self.storage.save_baselines(self.baselines.rows())
            self.storage.set_meta("snoozed_until", str(self.quiet.snoozed_until))
        self.storage.close(max(0.2, timeout - (time.monotonic() - t0)))
        return time.monotonic() - t0

    def _collector_loop(self) -> None:
        interval = self.cfg.sample_interval_s
        next_tick = self.clock.monotonic()
        while not self._stop.is_set():
            wait = next_tick - self.clock.monotonic()
            if wait > 0 and self._stop.wait(wait):
                break
            now_m = self.clock.monotonic()
            late_ms = max(0.0, (now_m - next_tick) * 1000.0)
            try:
                sample = self.collector.collect(self.clock.time(), now_m, late_ms)
                try:
                    self.queue.put_nowait(sample)
                except queue.Full:
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        pass
                    self.drops += 1
                    self.queue.put_nowait(sample)
            except Exception:
                self.errors += 1
                log.exception("collector iteration failed")
            next_tick += interval
            if self.clock.monotonic() - next_tick > interval:
                next_tick = self.clock.monotonic() + interval  # fell far behind: don't burst

    def _processing_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_sample(sample)
            except Exception:
                self.errors += 1
                log.exception("processing failed")

    def _supervisor_loop(self) -> None:
        while not self._stop.wait(2.0):
            for name, target in (("collector", self._collector_loop), ("processing", self._processing_loop)):
                t = self._threads.get(name)
                if t is not None and not t.is_alive() and not self._stop.is_set():
                    log.error("%s thread died; restarting", name)
                    self.restarts += 1
                    self._spawn(name, target)
            self._emit("status", self.status())

    # ============================================================ sync tick
    def tick(self) -> SampleView:
        """Collect and process one sample synchronously (replay harness and tests)."""
        sample = self.collector.collect(self.clock.time(), self.clock.monotonic(), 0.0)
        return self.process_sample(sample)

    # =================================================================== core
    def process_sample(self, sample: Sample) -> SampleView:
        events: list[AlertEvent] = []
        mood_changed = False
        feeds = []
        with self.lock:
            cfg = self.cfg
            ts, mono = sample.ts, sample.mono
            self.last_sample_mono = mono
            if self.first_ts is None:
                self.first_ts = ts
                self.storage.set_meta("first_sample_ts", str(ts))
            raw_act = self.mapper.classify(sample.sys)
            act = self.smoother.update(mono, raw_act)
            sample.raw_activity, sample.activity = raw_act, act
            sample.slot = time_slot(ts, self.tz)
            day = day_ordinal(ts, self.tz)
            learning = ts - self.first_ts < cfg.cold_start_days * 86400
            excluded = mono < self.self_activity_until or self.benchmark_running
            login_grace = 0 <= ts - self.platform.boot_time() < cfg.login_grace_s
            det = self.detector.process(sample, sample.slot, act, day, learning=learning, excluded=excluded,
                                        login_grace=login_grace)
            if det.gap:
                self.smoother.reset()
            self.history.append(sample)
            fg_app = None
            if sample.sys.foreground_pid:
                for p in sample.procs:
                    if p.pid == sample.sys.foreground_pid:
                        fg_app = p.app
                        break
            self.runaway.observe(mono, sample.procs, fg_app)

            if ts - self._last_app_snapshot >= cfg.app_snapshot_s:
                self._last_app_snapshot = ts
                self.leaks.observe(ts, sample.apps)
                self.disk_trend.observe(ts, sample.sys.volumes)
                self.storage.save_apps(ts, sample.apps)
                self.storage.save_volumes(ts, sample.sys.volumes)
                for v in sample.sys.volumes:
                    prev = self._last_volumes.get(v.mount)
                    if prev is not None and v.free_gb - prev >= 0.5:
                        self.storage.add_event(ts, "disk_freed", {"mount": v.mount, "gb": v.free_gb - prev})
                    self._last_volumes[v.mount] = v.free_gb
            if mono - self._last_trend_eval >= cfg.trend_eval_s:
                self._last_trend_eval = mono
                self.trend_signals = self.leaks.signals() + self.disk_trend.signals()
                self.leak_map = {s.detail["target"]: s.detail for s in self.trend_signals if s.detail.get("rule") == "leak"}

            quiet_now = excluded or det.grace
            signals = list(det.signals)
            if not quiet_now:
                signals += self.trend_signals
                if not learning:  # learning mode: absolute limits and objective trends only (DET-9)
                    signals += self.runaway.signals(mono, sample.apps)
            user_idle = self.mapper.is_user_idle(sample.sys)
            ctx = DiagContext(sample, list(self.history), det.band, cfg, self.platform, act, user_idle,
                              self.runaway.findings(mono, sample.apps) if not quiet_now else [], self.leak_map)
            self.quiet.fullscreen = sample.sys.fullscreen
            self.quiet.dnd = sample.sys.dnd
            events = self.alerts.update(signals, ctx, ts, mono, self.quiet)
            for ev in events:
                ep = ev.episode
                if ev.kind == "closed" and ep is not None:
                    self.feedback.episode_closed(ep)
                if ev.kind in ("opened", "escalated") and ep is not None and ep.diagnosis.culprit is not None \
                        and ev.notify in ("popup", "badge", "inbox", "held"):
                    c = ep.diagnosis.culprit
                    app = sample.apps.get(c.app)
                    self.feeds.note_tip(c.app, c.display, ts, app.private_bytes if app else 0)
            for fe in self.feeds.observe(ts, sample.apps):
                feeds.append(fe)
                self.storage.add_event(ts, "feed", {"app": fe.app, "display": fe.display, "bytes": fe.freed_bytes,
                                                    "snack": fe.snack})
                self.proud_until = mono + cfg.proud_s

            active_rules = {e.rule for e in self.alerts.active()}
            inputs = MoodInputs(
                ram_z=det.z.get("ram"), ram_used_pct=100.0 - sample.sys.ram_available_pct if sample.sys.ram_total else 0.0,
                paging_z=det.z.get("paging"), paging_rate=sample.sys.paging_rate,
                ram_available_pct=sample.sys.ram_available_pct, temp_c=sample.sys.temp_c, tjmax_c=cfg.tjmax_c,
                throttled=(sample.sys.perf_limit_pct is not None and sample.sys.perf_limit_pct < cfg.throttle_limit_pct
                           and (sample.sys.cpu_total or 0) >= 50),
                disk_nearly_full=any(v.fixed and v.total and v.free_pct < cfg.disk_low_pct and v.free_gb < cfg.disk_low_gb
                                     for v in sample.sys.volumes),
                benchmark_running=self.benchmark_running, proud=mono < self.proud_until, user_idle=user_idle,
                cpu=sample.sys.cpu_total, memory_pressure_open="memory_pressure" in active_rules,
                throttling_open="throttling" in active_rules, storage_open="storage" in active_rules)
            mood = self.moods.update(mono, conditions(inputs))
            mood_changed = mood != self.current_mood
            self.current_mood = mood

            self.baselines.commit_due(ts)
            for kind, bucket, metric in self.baselines.events:
                self._notice(f"relearn:{bucket}:{metric}", ts, "I've learned a new normal",
                             f"Your {metric} in {bucket} has been higher for several days, so I now treat it as normal.")
            self.baselines.events.clear()

            base_level = det.bucket_level.get("cpu")
            self.storage.save_sample(sample, det.band, base_level, cfg.top_n)
            if mono - self._last_baseline_save >= 60.0:
                self._last_baseline_save = mono
                self.storage.save_baselines(self.baselines.rows(only_dirty=True))
                self.baselines.prune_exclusions(ts)
            hour = int(ts // 3600)
            if hour != self._last_hour:
                if self._last_hour != -1:
                    self._hourly(ts)
                self._last_hour = hour

            boot_s = self.boot.observe(ts, sample.sys.cpu_total)
            if boot_s is not None:
                self.storage.add_event(ts, "boot", {"duration_s": boot_s, "boot_time": self.boot.boot_time})
                self.storage.set_meta("last_boot_recorded", str(int(self.boot.boot_time)))
            if mono - self._last_progress >= 600.0:
                self._last_progress = mono
                self.progress = self.baselines.learning_progress(self.storage.usage_seconds(ts - 7 * 86400,
                                                                                            cfg.sample_interval_s))
            if self.threaded and mono - self._last_bench_check >= 300.0:
                self._last_bench_check = mono
                self._maybe_schedule_benchmark(sample, user_idle)

            uptime = ts - self.platform.boot_time()
            view = self._view(sample, det, mood, learning, user_idle, roast(sample, cfg.roasts, uptime))
            self.latest = view
            self.views.append(view)
        self._emit("sample", view)
        for ev in events:
            self._emit("alert", ev)
        for fe in feeds:
            self._emit("feed", fe)
        if mood_changed:
            self._emit("mood", mood)
        return view

    def _hourly(self, ts: float) -> None:
        rows = []
        for (bucket, metric), st in list(self.baselines.stats.items()):
            if st.trusted(self.cfg.min_samples, self.cfg.min_days):
                rows.append((bucket, metric, st.mean, st.std, st.n))
        self.storage.save_baseline_snapshot(ts, rows)
        if ts - self._last_maintain >= 3600.0:
            self._last_maintain = ts
            try:
                self.storage.maintain(ts, self.cfg.raw_days, self.cfg.minute_days)
            except Exception:
                log.exception("retention job failed")
        day = day_ordinal(ts, self.tz)
        if day != self._last_uptime_day:
            self._last_uptime_day = day
            self.storage.add_event(ts, "uptime", {"seconds": ts - self.platform.boot_time()})

    def _view(self, s: Sample, det: DetectorResult, mood: Mood, learning: bool, user_idle: bool,
              roast_text: str | None) -> SampleView:
        sy = s.sys
        vols = [(v.mount, v.free_gb, v.total / GB, v.free_pct) for v in sy.volumes if v.fixed and v.total]
        primary = None
        if vols:
            c = next((v for v in vols if v[0].upper().startswith("C")), vols[0])
            primary = (c[0], c[1], c[3])
        apps = sorted((a for a in s.apps.values() if not a.is_self), key=lambda a: a.private_bytes, reverse=True)[:16]
        me = s.apps.get("mochi")
        first = self.first_ts or s.ts
        return SampleView(
            ts=s.ts, metrics=dict(s.metrics), z=dict(det.z), bands=dict(det.band), bucket_levels=dict(det.bucket_level),
            activity=s.activity, slot=s.slot, mood=mood, learning=learning,
            learning_day=int((s.ts - first) // 86400) + 1, progress=self.progress,
            cpu=sy.cpu_total, ram_used_gb=sy.ram_used / GB, ram_total_gb=sy.ram_total / GB,
            ram_used_pct=100.0 - sy.ram_available_pct if sy.ram_total else 0.0, commit_pct=sy.commit_pct,
            disk_busy=sy.disk_busy_pct, perf_limit=sy.perf_limit_pct, primary_volume=primary, volumes=vols,
            battery=copy.copy(sy.battery), apps=[copy.copy(a) for a in apps], unavailable=dict(sy.unavailable),
            self_cpu=me.cpu_total if me else 0.0, self_mem_mb=(me.private_bytes / MB) if me else 0.0,
            late_ms=s.late_ms, drops=self.drops, badge=self.alerts.badge_count, roast=roast_text,
            user_idle=user_idle, fullscreen=sy.fullscreen, snoozed_until=self.quiet.snoozed_until,
            demo=self.base_cfg.demo_mode, sensitivity=self.cfg.sensitivity_name,
        )

    # ============================================================ notices
    def _notice(self, nid: str, ts: float, title: str, text: str, action: str | None = None) -> None:
        if nid in self.notices and not self.notices[nid].dismissed:
            return
        n = Notice(nid, ts, title, text, action)
        self.notices[nid] = n
        self._emit("notice", n)

    def dismiss_notice(self, nid: str, declined_checkup: bool = False) -> None:
        with self.lock:
            n = self.notices.get(nid)
            if n:
                n.dismissed = True
            if declined_checkup:
                self.storage.set_meta("checkup_declined_ts", str(self.clock.time()))

    # ============================================================ user API
    def status(self) -> dict:
        now_m = self.clock.monotonic()
        age = None if self.last_sample_mono is None else now_m - self.last_sample_mono
        return {
            "last_sample_age": age,
            "stale": age is not None and age > 30.0,
            "threads": {n: t.is_alive() for n, t in self._threads.items()},
            "errors": self.errors + self.storage.errors,
            "restarts": self.restarts,
            "drops": self.drops,
            "db_mb": self.storage.size_mb(),
            "learning_progress": self.progress,
            "config_warnings": list(self.config_warnings) + list(self.mapper.warnings),
        }

    def respond(self, episode_id: int, kind: str) -> str:
        """kind: intentional | not_a_problem | new_normal | dismiss"""
        with self.lock:
            ts = self.clock.time()
            ep = self.alerts.find(episode_id)
            if ep is None:
                return "That alert has already gone."
            if kind == "intentional":
                msg = self.feedback.intentional(ep, ts)
            elif kind == "not_a_problem":
                msg = self.feedback.not_a_problem(ep, ts)
            elif kind == "new_normal":
                slot = time_slot(ep.opened_ts, self.tz)
                msg = self.feedback.new_normal(ep, slot, ts)
            elif kind == "dismiss":
                self.alerts.dismiss(episode_id, ts)
                msg = "Okay."
            else:
                raise ValueError(kind)
            self.storage.upsert_alert(ep)
        self._emit("alert", AlertEvent("updated", ep))
        return msg

    def act(self, action: Action, episode_id: int | None = None) -> ActionResult:
        t = action.target
        if action.kind == "end_process":
            res = self.actions.end_process(t.get("pid") or 0, t.get("create_time") or 0.0, t.get("display") or t.get("app", "?"),
                                           t.get("app"))
        elif action.kind == "lower_priority":
            res = self.actions.lower_priority(t.get("pid") or 0, t.get("create_time") or 0.0, t.get("app", "?"))
        elif action.kind == "open_location":
            res = self.actions.open_location(t.get("path"))
        elif action.kind == "startup_settings":
            res = self.actions.open_startup_settings()
        else:
            return ActionResult(True, "")
        if res.ok and episode_id is not None:
            with self.lock:
                ep = self.alerts.find(episode_id)
                if ep is not None and ep.response is None:
                    ep.response, ep.response_ts = "action", self.clock.time()
                    self.storage.upsert_alert(ep)
        return res

    def snooze(self, seconds: float | None) -> float:
        """seconds=None means until tomorrow 08:00. 0 resumes."""
        with self.lock:
            now = self.clock.time()
            if seconds is None:
                d = datetime.fromtimestamp(now) + timedelta(days=1)
                until = datetime(d.year, d.month, d.day, 8, 0).timestamp()
            else:
                until = now + seconds if seconds > 0 else 0.0
            self.quiet.snoozed_until = until
            self.storage.set_meta("snoozed_until", str(until))
        self._emit("status", self.status())
        return until

    def update_config(self, **changes) -> list[str]:
        with self.lock:
            data = self.base_cfg.to_dict()
            data.update(changes)
            res = config_mod.from_dict(data)
            new = res.config
            if "autostart" in changes and changes["autostart"] != self.base_cfg.autostart:
                try:
                    self.autostart.apply(bool(new.autostart))
                except OSError as exc:
                    res.warnings.append(f"Couldn't change start-at-sign-in ({exc}).")
            self.base_cfg = new
            self.cfg = new.effective()
            for part in (self.baselines, self.feedback, self.alerts, self.leaks, self.disk_trend, self.runaway,
                         self.collector, self.bench, self.scheduler):
                part.cfg = self.cfg
            self.detector.configure(self.cfg)
            self.mapper.idle_minutes = self.cfg.idle_minutes
            self.moods.window_s, self.moods.dwell_s = self.cfg.mood_window_s, self.cfg.mood_dwell_s
            config_mod.save(new, self.paths.config)
        self._emit("config", new)
        return res.warnings

    def set_mapping(self, exe: str, activity: str | None) -> None:
        with self.lock:
            if activity is None:
                self.mapper.remove(exe)
            else:
                self.mapper.set(exe, activity)
            self.mapper.save(self.paths.activity_map)

    def suppressions(self) -> list[Suppression]:
        return self.feedback.active(self.clock.time())

    def remove_suppression(self, sid: int) -> bool:
        with self.lock:
            return self.feedback.remove(sid)

    def export_data(self):
        return self.storage.export(self.paths.exports)

    def delete_all_data(self) -> None:
        with self.lock:
            self.storage.delete_all()
            self.baselines.stats.clear()
            self.baselines.pending.clear()
            self.feedback.suppressions.clear()
            self.alerts.open.clear()
            self.alerts.recent.clear()
            self.alerts._closed_by_key.clear()
            self.first_ts = None
            self.views.clear()
        self._emit("status", self.status())

    def set_rebaseline(self) -> None:
        self.storage.set_meta("health_rebaseline_ts", str(self.clock.time()))

    # ------------------------------------------------------------- queries
    def inbox(self, n: int = 40) -> list[Episode]:
        with self.lock:
            return self.alerts.all_recent(n)

    def active_notices(self) -> list[Notice]:
        return [n for n in self.notices.values() if not n.dismissed]

    def timeline(self, metric: str, seconds: float) -> dict:
        now = self.clock.time()
        self.storage.flush()
        res = "raw" if seconds <= 26 * 3600 else "minute"
        rows = self.storage.series(metric, now - seconds, now + 1, res)
        if res == "minute":
            rows += [r for r in self.storage.series(metric, max(now - 3 * 3600, now - seconds), now + 1, "raw")
                     if not rows or r[0] > rows[-1][0] + 60]
        markers = [a for a in self.storage.alerts_between(now - seconds, now + 1)
                   if metric in (a["metrics"] or "").split(",") or (metric == "ram" and a["rule"] in ("leak", "memory_pressure"))]
        return {"rows": rows, "markers": markers, "resolution": res}

    def detective(self) -> list[dict]:
        """The suspect line-up: apps by memory actually in RAM (QRK-1)."""
        with self.lock:
            if not self.history:
                return []
            s = self.history[-1]
            apps = [a for a in s.apps.values() if not a.is_self and a.key not in ("system", "memory compression", "registry")]
        total_ram = s.sys.ram_total or 1
        rows = []
        for a in sorted(apps, key=lambda a: (a.ws_private if a.ws_private is not None else a.private_bytes), reverse=True)[:8]:
            in_ram = a.ws_private if a.ws_private is not None else min(a.private_bytes, total_ram)
            rows.append({"app": a.key, "display": a.display, "in_ram": in_ram, "private": a.private_bytes,
                         "growing_s": self.leaks.growing_for(a.key), "procs": a.proc_count, "pid": a.root_pid,
                         "create_time": a.root_create_time, "exe": self.platform.exe_path(a.root_pid, a.root_create_time),
                         "is_system": a.is_system, "leak": a.key in self.leak_map, "cpu_core": a.cpu_core})
        if rows:
            leakers = [r for r in rows if r["leak"]]
            prime = max(leakers, key=lambda r: self.leak_map[r["app"]]["rate_mb_h"]) if leakers else rows[0]
            prime["prime"] = True
        return rows

    def weather(self) -> str | None:
        return weather(self.storage, self.clock.time())

    def os_fact(self) -> dict | None:
        self.storage.flush()
        return os_fact(self.storage, self.clock.time())

    def achievements(self):
        has_limit = self.latest is not None and self.latest.perf_limit is not None
        boot_measured = bool(self.storage.events("boot")) or self.boot.active
        return evaluate_achievements(self.storage, self.clock.time(), has_limit, boot_measured)

    def calm_streak(self) -> int:
        return calm_streak(self.storage, self.clock.time())

    def diary(self):
        return diary(self.storage, self.clock.time())

    def records(self) -> dict[str, str]:
        return records(self.storage)

    def bench_history(self) -> list[BenchResult]:
        return [BenchResult.from_row(r) for r in self.storage.query("SELECT * FROM benchmarks ORDER BY ts")]

    def health_summary(self) -> dict:
        hist = self.bench_history()
        valid = [r for r in hist if r.valid]
        latest = valid[-1] if valid else None
        since = float(self.storage.get_meta("health_rebaseline_ts") or 0.0)
        ref = self.health.reference(hist, latest.condition(), since) if latest else None
        return {"latest": latest, "history": hist, "reference": ref, "score": latest.score if latest else None,
                "runs": len(valid)}

    # -------------------------------------------------------- benchmarks
    def _maybe_schedule_benchmark(self, sample: Sample, user_idle: bool) -> None:
        hist = self.bench_history()
        first = hist[0].ts if hist else None
        last = hist[-1].ts if hist else None
        on_ac = sample.sys.battery.on_ac if sample.sys.battery else None
        if self.scheduler.due(sample.ts, first, last) and self.scheduler.can_run_now(user_idle, on_ac, sample.sys.fullscreen) \
                and not self.benchmark_running and not self.checkup_running:
            threading.Thread(target=self.run_benchmark, kwargs={"scheduled": True}, name="mochi-bench", daemon=True).start()
        elif self.scheduler.overdue(sample.ts, first or self.first_ts, last):
            self._notice("bench_overdue", sample.ts, "Mind if I run a quick speed test?",
                         "I haven't found a quiet moment for my 15-second speed test. Run it now?", "run_benchmark")

    def run_benchmark(self, scheduled: bool = False) -> BenchResult:
        with self.lock:
            if self.benchmark_running:
                raise RuntimeError("A speed test is already running.")
            self.benchmark_running = True
            latest = self.history[-1] if self.history else None
            idle_at_start = latest.sys.idle_seconds if latest else 0.0
        self._emit("benchmark_started", None)
        try:
            sy = latest.sys if latest else None
            free = min((v.free_gb for v in sy.volumes if v.fixed), default=None) if sy else None
            bg = sy.cpu_total if sy else None
            on_ac = sy.battery.on_ac if sy and sy.battery else None

            def abort() -> str | None:
                cur = self.history[-1] if self.history else None
                if scheduled and cur is not None and cur.sys.idle_seconds + 1 < idle_at_start:
                    return "you came back"
                return None

            res = self.bench.run(scheduled=scheduled, free_gb=free, on_ac=on_ac, power_mode=self.platform.power_mode(),
                                 background_cpu=bg, temp=sy.temp_c if sy else None, abort_check=abort)
            hist = self.bench_history()
            since = float(self.storage.get_meta("health_rebaseline_ts") or 0.0)
            prev_scores = [r.score for r in hist if r.valid and r.score is not None]
            ref = self.health.reference(hist + [res], res.condition(), since)
            res.score = self.health.score(res, ref)
            self.storage.execute("INSERT INTO benchmarks(ts, cpu_s, mem_s, disk_write_s, disk_read_s, disk_mode, on_ac, "
                                 "power_mode, background_cpu, temp, score, valid, scheduled, note) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", res.to_row())
            self.storage.flush()
            if res.score is not None and prev_scores and res.score > prev_scores[-1] + 2:
                self.proud_until = self.clock.monotonic() + self.cfg.proud_s
            scores = prev_scores + ([res.score] if res.score is not None else [])
            declined = self.storage.get_meta("checkup_declined_ts")
            if self.health.should_suggest(scores, self.cfg.health_suggest_below, float(declined) if declined else None,
                                          self.clock.time(), self.cfg.checkup_decline_days):
                self._notice("checkup_suggest", self.clock.time(), "Check-up time?",
                             "I've been feeling a bit slow lately. Want to do a check-up together?", "run_checkup")
            return res
        finally:
            with self.lock:
                self.benchmark_running = False
                self.self_activity_until = self.clock.monotonic() + 30.0
            self._emit("benchmark", self.health_summary())

    # ----------------------------------------------------------- check-up
    def scan_biggest(self, root: str, done: Callable[[ScanResult], None], budget_s: float = 60.0) -> None:
        def finished(res: ScanResult) -> None:
            self.last_scan = res
            done(res)

        self.scanner.start(root, finished, budget_s)

    def _idle_rows(self) -> list[tuple[str, float]]:
        since = self.clock.time() - 7 * 86400
        rows = self.storage.query(
            "SELECT p.app, AVG(p.cpu_core) FROM proc_samples p JOIN samples s ON s.ts = p.ts "
            "WHERE s.activity='idle' AND p.ts >= ? GROUP BY p.app ORDER BY 2 DESC LIMIT 20", (since,))
        n_idle = self.storage.scalar("SELECT COUNT(*) FROM samples WHERE activity='idle' AND ts >= ?", (since,), 0)
        if n_idle < 60:
            return []
        out = []
        for app, avg in rows:
            if app in ("mochi", "system"):
                continue
            seen = self.storage.scalar("SELECT COUNT(*) FROM proc_samples p JOIN samples s ON s.ts=p.ts "
                                       "WHERE s.activity='idle' AND p.app=? AND p.ts>=?", (app, since), 0)
            out.append((app, (avg or 0.0) * seen / n_idle))
        return sorted(out, key=lambda r: -r[1])

    def _load_test(self, stop: threading.Event, seconds: float = 20.0) -> tuple[list[float], list[float]]:
        with self.lock:
            self.self_activity_until = float("inf")
        proc = self.bench.load_test(seconds)
        limits: list[float] = []
        temps: list[float] = []
        t0 = time.monotonic()
        seen = set()
        try:
            while proc.poll() is None and time.monotonic() - t0 < seconds + 10:
                time.sleep(0.5)
                cur = self.history[-1] if self.history else None
                if cur is None or cur.ts in seen:
                    continue
                seen.add(cur.ts)
                if cur.sys.perf_limit_pct is not None and (cur.sys.cpu_total or 0) > 30:
                    limits.append(cur.sys.perf_limit_pct)
                if cur.sys.temp_c is not None:
                    temps.append(cur.sys.temp_c)
                    if cur.sys.temp_c >= self.cfg.tjmax_c - self.cfg.temp_margin_c:
                        break  # too hot: stop early (CHK-4)
                if stop.is_set():
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(5)
            with self.lock:
                self.self_activity_until = self.clock.monotonic() + 30.0
        return limits, temps

    def run_checkup(self, progress: Callable[[str, float], None] | None = None,
                    cancel: threading.Event | None = None, quick_scan_root: str | None = None) -> CheckupReport:
        with self.lock:
            if self.checkup_running:
                raise RuntimeError("A check-up is already running.")
            self.checkup_running = True
        try:
            def volumes():
                return self.history[-1].sys.volumes if self.history else []

            def biggest():
                if self.last_scan is not None and time.time() - (self.last_scan_ts or 0) < 86400:
                    return self.last_scan.folders
                import os

                root = quick_scan_root or os.path.expanduser("~")
                res = folder_scan(root, budget_s=20.0)
                self.last_scan, self.last_scan_ts = res, time.time()
                return res.folders

            def bench():
                r = self.run_benchmark(scheduled=False)
                return (r.__dict__ if r.valid else None), r.score

            runner = CheckupRunner(self.cfg, self.platform, volumes, biggest, self._idle_rows,
                                   self.leaks.findings, lambda: bool(self.history and self.history[-1].sys.battery),
                                   bench, self._load_test)
            report = runner.run(progress, cancel)
            import json

            self.storage.execute("INSERT INTO checkups(ts, score, report) VALUES (?,?,?)",
                                 (report.ts, report.score, json.dumps(report.to_json(), default=str)))
            self.storage.flush()
            self.last_checkup = report
            self._emit("checkup", report)
            return report
        finally:
            with self.lock:
                self.checkup_running = False

    last_scan_ts: float | None = None

    def checkup_history(self) -> list[tuple[float, float | None]]:
        return [(ts, score) for ts, score in self.storage.query("SELECT ts, score FROM checkups ORDER BY ts DESC LIMIT 20")]

    # ------------------------------------------------------- startup timing
    def _launched_since_boot(self, app: str, boot_time: float) -> bool:
        return bool(self.storage.scalar("SELECT COUNT(*) FROM launches WHERE app=? AND ts>=?", (app, boot_time), 0))

    def _on_launch(self, la: Launch) -> None:
        if not la.settled:
            return
        self.storage.execute("INSERT INTO launches(app, ts, duration_s, cold, settled) VALUES (?,?,?,?,?)",
                             (la.app, la.ts, la.duration_s, int(la.cold), int(la.settled)))
        self._emit("launch", la)
        self.storage.flush()
        launches = [Launch(a, a, ts, d, bool(c), bool(s)) for a, ts, d, c, s in
                    self.storage.query("SELECT app, ts, duration_s, cold, settled FROM launches WHERE ts >= ?",
                                       (time.time() - 30 * 86400,))]
        slow = slow_apps(launches, time.time())
        if several_apps_slow(slow):
            names = ", ".join(sorted({s["app"] for s in slow})[:4])
            self._notice("slow_launches", time.time(), "Apps are opening slower",
                         f"Several apps take longer to open than they used to ({names}). Want to do a check-up?",
                         "run_checkup")
