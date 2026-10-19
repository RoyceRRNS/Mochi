"""SQLite persistence (STO-1..9).

- WAL mode, one writer thread, commits batched about every 30 s. A hard kill loses at
  most one batch and never corrupts the file (LIF-4).
- Schema versioning with forward-only migrations; data is never wiped (STO-3).
- Retention: raw 5-second samples 14 days, 1-minute aggregates 90 days, hourly forever.
- Only exe names and script basenames are stored. No window titles, URLs or command
  lines (SEC-3).
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .model import BASELINE_METRICS, GB, MB, WATCH_METRICS, App, Sample, Volume

log = logging.getLogger(__name__)

# The v1 schema's metrics, frozen: later metrics arrive through migrations, never by editing v1.
V1_METRICS = ("cpu", "ram", "paging", "disk", "temp", "discharge")
V1_SAMPLE_COLS = (
    ["ts", "slot", "activity"] + list(V1_METRICS)
    + ["commit_pct", "disk_lat", "disk_queue", "cpu_perf", "perf_limit", "run_queue", "ram_total", "ram_avail",
       "battery_pct", "on_ac", "idle_s", "fg", "fullscreen", "late_ms", "ctx", "irq", "syscalls", "base_level"]
    + [f"{m}_{s}" for m in V1_METRICS for s in ("mu", "sd")]
)
V1_AGG_COLS = ["n"] + [f"{m}_{s}" for m in V1_METRICS for s in ("avg", "max", "mu", "sd")] + ["idle_frac"]
# v3: GPU and fans
V3_SAMPLE_COLS = list(WATCH_METRICS) + [f"{m}_{s}" for m in WATCH_METRICS for s in ("mu", "sd")] + ["gpu_mem", "gpu_power"]
V3_AGG_COLS = [f"{m}_{s}" for m in WATCH_METRICS for s in ("avg", "max", "mu", "sd")]
SAMPLE_COLS = V1_SAMPLE_COLS + V3_SAMPLE_COLS
AGG_COLS = V1_AGG_COLS + V3_AGG_COLS
assert tuple(V1_METRICS) + tuple(WATCH_METRICS) == BASELINE_METRICS

MIGRATIONS: list[str] = [
    # v1
    f"""
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE samples ({", ".join(c + (" REAL PRIMARY KEY" if c == "ts" else "") for c in V1_SAMPLE_COLS)});
    CREATE TABLE proc_samples (ts REAL, app TEXT, name TEXT, pid INTEGER, cpu_core REAL, cpu_total REAL,
                               mem_mb REAL, ws_mb REAL, io_bps REAL);
    CREATE INDEX proc_samples_ts ON proc_samples(ts);
    CREATE TABLE app_mem (ts REAL, app TEXT, display TEXT, mem_mb REAL, ws_mb REAL, procs INTEGER, cpu_core REAL);
    CREATE INDEX app_mem_ts ON app_mem(ts);
    CREATE TABLE volumes (ts REAL, mount TEXT, total_gb REAL, free_gb REAL);
    CREATE INDEX volumes_ts ON volumes(ts);
    CREATE TABLE agg_minute (ts REAL PRIMARY KEY, {", ".join(V1_AGG_COLS)});
    CREATE TABLE agg_hour (ts REAL PRIMARY KEY, {", ".join(V1_AGG_COLS)});
    CREATE TABLE baselines (bucket TEXT, metric TEXT, data TEXT, PRIMARY KEY (bucket, metric));
    CREATE TABLE baseline_history (ts REAL, bucket TEXT, metric TEXT, mean REAL, sd REAL, n INTEGER);
    CREATE TABLE alerts (id INTEGER PRIMARY KEY, rule TEXT, target TEXT, level INTEGER, max_level INTEGER,
                         opened_ts REAL, updated_ts REAL, closed_ts REAL, metrics TEXT, activity TEXT,
                         bucket_level TEXT, z REAL, title TEXT, cause TEXT, tip TEXT, culprit_app TEXT,
                         culprit_display TEXT, response TEXT, response_ts REAL, notified TEXT, suppressed INTEGER);
    CREATE INDEX alerts_opened ON alerts(opened_ts);
    CREATE TABLE suppressions (id INTEGER PRIMARY KEY, app TEXT, metric TEXT, activity TEXT, created_ts REAL,
                               expires_ts REAL, active INTEGER, label TEXT);
    """,
    # v2: task performance and quirks
    """
    CREATE TABLE launches (id INTEGER PRIMARY KEY, app TEXT, ts REAL, duration_s REAL, cold INTEGER, settled INTEGER);
    CREATE INDEX launches_app ON launches(app, ts);
    CREATE TABLE benchmarks (id INTEGER PRIMARY KEY, ts REAL, cpu_s REAL, mem_s REAL, disk_write_s REAL,
                             disk_read_s REAL, disk_mode TEXT, on_ac INTEGER, power_mode TEXT, background_cpu REAL,
                             temp REAL, score REAL, valid INTEGER, scheduled INTEGER, note TEXT);
    CREATE TABLE checkups (id INTEGER PRIMARY KEY, ts REAL, score REAL, report TEXT);
    CREATE TABLE events (ts REAL, kind TEXT, data TEXT);
    CREATE INDEX events_kind ON events(kind, ts);
    CREATE TABLE achievements (key TEXT PRIMARY KEY, unlocked_ts REAL);
    """,
    # v3: GPU busy, GPU temperature and fan speed (with their usual bands), GPU memory and power
    ";".join([f"ALTER TABLE samples ADD COLUMN {c} REAL" for c in V3_SAMPLE_COLS]
             + [f"ALTER TABLE {t} ADD COLUMN {c} REAL" for t in ("agg_minute", "agg_hour") for c in V3_AGG_COLS]),
]
SCHEMA_VERSION = len(MIGRATIONS)
TABLES = ["samples", "proc_samples", "app_mem", "volumes", "agg_minute", "agg_hour", "baselines", "baseline_history",
          "alerts", "suppressions", "launches", "benchmarks", "checkups", "events", "achievements"]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def migrate(conn: sqlite3.Connection, upto: int | None = None) -> int:
    upto = SCHEMA_VERSION if upto is None else upto
    has_meta = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    version = 0
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else 0
    while version < upto:
        conn.execute("BEGIN")
        try:
            for stmt in MIGRATIONS[version].split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            version += 1
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(version),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return version


class Storage:
    def __init__(self, path: Path | str, commit_interval: float = 30.0, threaded: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from .recovery import recover_in_place

        self.recovered = recover_in_place(self.path)  # a damaged file never stops Mochi starting
        self.commit_interval = commit_interval
        self.threaded = threaded
        self._local = threading.local()
        self._wconn = connect(self.path)
        self.version = migrate(self._wconn)
        self._wlock = threading.RLock()
        self._in_tx = False
        self._last_commit = time.monotonic()
        self.writes = 0
        self.errors = 0
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.closed = False
        if threaded:
            self._wconn.close()
            self._wconn = None  # type: ignore[assignment]
            self._thread = threading.Thread(target=self._writer, name="mochi-storage", daemon=True)
            self._thread.start()

    # ----------------------------------------------------------- write plumbing
    def _apply(self, conn: sqlite3.Connection, op: tuple) -> None:
        kind = op[0]
        if not self._in_tx:
            conn.execute("BEGIN")
            self._in_tx = True
        if kind == "exec":
            conn.execute(op[1], op[2])
        elif kind == "many":
            conn.executemany(op[1], op[2])
        self.writes += 1

    def _commit(self, conn: sqlite3.Connection) -> None:
        if self._in_tx:
            conn.execute("COMMIT")
            self._in_tx = False
        self._last_commit = time.monotonic()

    def _writer(self) -> None:
        conn = connect(self.path)
        while True:
            timeout = max(0.05, self.commit_interval - (time.monotonic() - self._last_commit))
            try:
                op = self._q.get(timeout=timeout)
            except queue.Empty:
                op = None
            try:
                if op is None:
                    self._commit(conn)
                elif op[0] == "flush":
                    self._commit(conn)
                    op[1].set()
                elif op[0] == "vacuum":
                    self._commit(conn)
                    conn.execute("VACUUM")
                    op[1].set()
                elif op[0] == "stop":
                    self._commit(conn)
                    conn.close()
                    op[1].set()
                    return
                else:
                    self._apply(conn, op)
                    if time.monotonic() - self._last_commit >= self.commit_interval:
                        self._commit(conn)
            except Exception as exc:  # never let the writer die (LIF-7)
                self.errors += 1
                log.exception("storage write failed: %s", exc)
                try:
                    if self._in_tx:
                        conn.execute("ROLLBACK")
                except Exception:
                    pass
                self._in_tx = False
                if op is not None and op[0] in ("flush", "stop", "vacuum"):
                    op[1].set()

    def _submit(self, op: tuple) -> None:
        if self.closed:
            log.debug("write after close ignored: %s", op[0])
            return
        if self.threaded:
            self._q.put(op)
        else:
            with self._wlock:
                try:
                    self._apply(self._wconn, op)
                    if time.monotonic() - self._last_commit >= self.commit_interval:
                        self._commit(self._wconn)
                except Exception:
                    self.errors += 1
                    log.exception("storage write failed")
                    if self._in_tx:
                        self._wconn.execute("ROLLBACK")
                        self._in_tx = False

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        self._submit(("exec", sql, tuple(params)))

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if rows:
            self._submit(("many", sql, rows))

    def flush(self, timeout: float = 5.0) -> bool:
        if self.closed:
            return True
        if not self.threaded:
            with self._wlock:
                self._commit(self._wconn)
            return True
        ev = threading.Event()
        self._q.put(("flush", ev))
        return ev.wait(timeout)

    def close(self, timeout: float = 2.0) -> bool:
        if self.closed:
            return True
        ok = True
        if self.threaded and self._thread is not None and self._thread.is_alive():
            ev = threading.Event()
            self._q.put(("stop", ev))
            ok = ev.wait(timeout)
            self._thread.join(timeout)
        elif not self.threaded and self._wconn is not None:
            with self._wlock:
                self._commit(self._wconn)
                self._wconn.close()
                self._wconn = None  # type: ignore[assignment]
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        self.closed = True
        return ok

    # ------------------------------------------------------------------- reads
    def conn(self) -> sqlite3.Connection:
        if not self.threaded and self._wconn is not None:
            return self._wconn  # same connection, so reads see not-yet-committed writes
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = connect(self.path)
        return c

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.conn().execute(sql, tuple(params)).fetchone()
        return row[0] if row and row[0] is not None else default

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key=?", (key,), default)

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ---------------------------------------------------------------- samples
    def save_sample(self, s: Sample, bands: dict[str, tuple[float, float] | None], base_level: str | None,
                    top_n: int = 15) -> None:
        sy = s.sys
        row: list[Any] = [s.ts, s.slot, s.activity] + [s.metrics.get(m) for m in V1_METRICS] + [
            sy.commit_pct, sy.disk_latency_ms, sy.disk_queue, sy.cpu_perf_pct, sy.perf_limit_pct, sy.run_queue,
            sy.ram_total / GB if sy.ram_total else None, sy.ram_available / GB if sy.ram_total else None,
            sy.battery.percent if sy.battery else None, int(sy.battery.on_ac) if sy.battery else None,
            sy.idle_seconds, (sy.foreground_exe or None), int(sy.fullscreen), s.late_ms,
            sy.ctx_switches, sy.interrupts, sy.syscalls, base_level,
        ]
        for m in V1_METRICS:
            b = bands.get(m)
            row += [b[0], b[1]] if b else [None, None]
        row += [s.metrics.get(m) for m in WATCH_METRICS]
        for m in WATCH_METRICS:
            b = bands.get(m)
            row += [b[0], b[1]] if b else [None, None]
        row += [sy.gpu_mem_used / GB if sy.gpu_mem_used is not None else None, sy.gpu_power_w]
        self.execute(f"INSERT OR REPLACE INTO samples({', '.join(SAMPLE_COLS)}) VALUES ({', '.join('?' * len(SAMPLE_COLS))})", row)
        procs = [
            (s.ts, p.app, p.name, p.pid, p.cpu_core, p.cpu_total, (p.private_bytes or 0) / MB,
             (p.ws_private / MB) if p.ws_private is not None else None, p.io_bps)
            for p in s.top_procs(top_n)
        ]
        self.executemany("INSERT INTO proc_samples VALUES (?,?,?,?,?,?,?,?,?)", procs)

    def save_apps(self, ts: float, apps: dict[str, App]) -> None:
        rows = [(ts, a.key, a.display, a.private_bytes / MB, (a.ws_private or 0) / MB if a.ws_private is not None else None,
                 a.proc_count, a.cpu_core) for a in apps.values() if not a.is_self]
        self.executemany("INSERT INTO app_mem VALUES (?,?,?,?,?,?,?)", rows)

    def save_volumes(self, ts: float, volumes: list[Volume]) -> None:
        self.executemany("INSERT INTO volumes VALUES (?,?,?,?)",
                         [(ts, v.mount, v.total / GB, v.free / GB) for v in volumes])

    # --------------------------------------------------------------- baselines
    def save_baselines(self, rows: list[tuple[str, str, str]]) -> None:
        self.executemany("INSERT OR REPLACE INTO baselines(bucket, metric, data) VALUES (?,?,?)", rows)

    def load_baselines(self) -> list[tuple[str, str, str]]:
        return self.query("SELECT bucket, metric, data FROM baselines")

    def save_baseline_snapshot(self, ts: float, rows: list[tuple[str, str, float, float, int]]) -> None:
        self.executemany("INSERT INTO baseline_history VALUES (?,?,?,?,?,?)", [(ts, *r) for r in rows])

    # ------------------------------------------------------------------ alerts
    def upsert_alert(self, ep) -> None:
        d = ep.diagnosis
        c = d.culprit
        self.execute(
            "INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ep.id, ep.rule, ep.key[1], int(ep.level), int(ep.max_level), ep.opened_ts, ep.updated_ts, ep.closed_ts,
             ",".join(sorted(ep.metrics)), ep.activity, ep.bucket_level, ep.z, d.title, d.cause, d.tip,
             c.app if c else None, c.display if c else None, ep.response, ep.response_ts,
             json.dumps({str(k): v for k, v in ep.notified.items()}), int(ep.suppressed)),
        )

    def max_alert_id(self) -> int:
        return int(self.scalar("SELECT MAX(id) FROM alerts", default=0))

    def alerts_between(self, t0: float, t1: float) -> list[dict]:
        cols = ["id", "rule", "target", "level", "max_level", "opened_ts", "updated_ts", "closed_ts", "metrics",
                "activity", "bucket_level", "z", "title", "cause", "tip", "culprit_app", "culprit_display",
                "response", "response_ts", "notified", "suppressed"]
        rows = self.query("SELECT * FROM alerts WHERE opened_ts >= ? AND opened_ts < ? ORDER BY opened_ts", (t0, t1))
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------ suppressions
    def save_suppression(self, s) -> None:
        self.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?,?,?,?,?,?,?)",
                     (s.id, s.app, s.metric, s.activity, s.created_ts, s.expires_ts, int(s.active), s.label))

    def load_suppressions(self) -> list[tuple]:
        return self.query("SELECT id, app, metric, activity, created_ts, expires_ts, active, label FROM suppressions")

    # ----------------------------------------------------------------- events
    def add_event(self, ts: float, kind: str, data: dict | None = None) -> None:
        self.execute("INSERT INTO events VALUES (?,?,?)", (ts, kind, json.dumps(data or {})))

    def events(self, kind: str, since: float = 0.0) -> list[tuple[float, dict]]:
        return [(ts, json.loads(d)) for ts, d in self.query("SELECT ts, data FROM events WHERE kind=? AND ts>=? ORDER BY ts",
                                                            (kind, since))]

    # --------------------------------------------------------------- timeline
    def series(self, metric: str, t0: float, t1: float, resolution: str = "raw") -> list[tuple]:
        if metric not in BASELINE_METRICS:
            raise ValueError(metric)
        if resolution == "raw":
            return self.query(f"SELECT ts, {metric}, {metric}_mu, {metric}_sd FROM samples WHERE ts>=? AND ts<? ORDER BY ts",
                              (t0, t1))
        table = "agg_minute" if resolution == "minute" else "agg_hour"
        return self.query(f"SELECT ts, {metric}_avg, {metric}_mu, {metric}_sd FROM {table} WHERE ts>=? AND ts<? ORDER BY ts",
                          (t0, t1))

    def usage_seconds(self, since: float, interval: float = 5.0) -> dict[tuple[int, str], float]:
        rows = self.query("SELECT slot, activity, COUNT(*) FROM samples WHERE ts>=? GROUP BY slot, activity", (since,))
        return {(int(slot), act): n * interval for slot, act, n in rows}

    # -------------------------------------------------------------- retention
    def maintain(self, now: float, raw_days: float = 14.0, minute_days: float = 90.0) -> dict[str, int]:
        """Aggregate finished minutes/hours, then drop expired raw rows (STO-2)."""
        stats: dict[str, int] = {}
        self.flush()
        conn = self.conn()
        cur_min = int(now // 60) * 60
        last = conn.execute("SELECT MAX(ts) FROM agg_minute").fetchone()[0]
        start = (last + 60) if last is not None else (conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or now)
        start = int(start // 60) * 60
        sel = ["COUNT(*)"]
        for m in V1_METRICS:
            sel += [f"AVG({m})", f"MAX({m})", f"AVG({m}_mu)", f"AVG({m}_sd)"]
        sel.append("AVG(CASE WHEN activity='idle' THEN 1.0 ELSE 0.0 END)")
        for m in WATCH_METRICS:
            sel += [f"AVG({m})", f"MAX({m})", f"AVG({m}_mu)", f"AVG({m}_sd)"]
        rows = conn.execute(
            f"SELECT CAST(ts/60 AS INTEGER)*60 AS m, {', '.join(sel)} FROM samples WHERE ts>=? AND ts<? GROUP BY m",
            (start, cur_min)).fetchall()
        cols = ["ts"] + AGG_COLS
        self.executemany(f"INSERT OR REPLACE INTO agg_minute({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)
        stats["minutes"] = len(rows)
        self.flush()
        cur_hour = int(now // 3600) * 3600
        last_h = conn.execute("SELECT MAX(ts) FROM agg_hour").fetchone()[0]
        start_h = (last_h + 3600) if last_h is not None else (conn.execute("SELECT MIN(ts) FROM agg_minute").fetchone()[0] or now)
        start_h = int(start_h // 3600) * 3600
        hsel = ["SUM(n)"]
        for m in V1_METRICS:
            hsel += [f"AVG({m}_avg)", f"MAX({m}_max)", f"AVG({m}_mu)", f"AVG({m}_sd)"]
        hsel.append("AVG(idle_frac)")
        for m in WATCH_METRICS:
            hsel += [f"AVG({m}_avg)", f"MAX({m}_max)", f"AVG({m}_mu)", f"AVG({m}_sd)"]
        hrows = conn.execute(
            f"SELECT CAST(ts/3600 AS INTEGER)*3600 AS h, {', '.join(hsel)} FROM agg_minute WHERE ts>=? AND ts<? GROUP BY h",
            (start_h, cur_hour)).fetchall()
        self.executemany(f"INSERT OR REPLACE INTO agg_hour({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", hrows)
        stats["hours"] = len(hrows)
        raw_cut = now - raw_days * 86400
        for table in ("samples", "proc_samples", "app_mem", "volumes"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE ts < ?", (raw_cut,)).fetchone()[0]
            if n:
                self.execute(f"DELETE FROM {table} WHERE ts < ?", (raw_cut,))
            stats[f"deleted_{table}"] = n
        min_cut = now - minute_days * 86400
        n = conn.execute("SELECT COUNT(*) FROM agg_minute WHERE ts < ?", (min_cut,)).fetchone()[0]
        if n:
            self.execute("DELETE FROM agg_minute WHERE ts < ?", (min_cut,))
        stats["deleted_agg_minute"] = n
        self.execute("DELETE FROM baseline_history WHERE ts < ?", (now - minute_days * 86400,))
        self.flush()
        return stats

    def size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total / MB

    def integrity_ok(self) -> bool:
        return self.scalar("PRAGMA integrity_check") == "ok"

    # ------------------------------------------------------------ export/delete
    def export(self, folder: Path) -> Path:
        """Write every table to CSV plus a JSON summary (STO-7)."""
        self.flush()
        out = folder / time.strftime("mochi-export-%Y%m%d-%H%M%S")
        out.mkdir(parents=True, exist_ok=True)
        summary: dict[str, int] = {}
        for table in TABLES:
            cur = self.conn().execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            with (out / f"{table}.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                w.writerows(rows)
            summary[table] = len(rows)
        (out / "summary.json").write_text(json.dumps({"tables": summary, "schema_version": self.version}, indent=2),
                                          encoding="utf-8")
        return out

    def delete_all(self) -> None:
        """Remove every row of personal data. Keeps the schema (STO-7)."""
        self.flush()
        for table in TABLES:
            self.execute(f"DELETE FROM {table}")
        self.execute("DELETE FROM meta WHERE key != 'schema_version'")
        self.flush()
        self.vacuum()

    def vacuum(self, timeout: float = 60.0) -> bool:
        """Shrink the file. Runs on the writer's connection, so no other connection writes meanwhile."""
        if self.closed:
            return False
        if self.threaded:
            ev = threading.Event()
            self._q.put(("vacuum", ev))
            return ev.wait(timeout)
        with self._wlock:
            self._commit(self._wconn)
            try:
                self._wconn.execute("VACUUM")
            except sqlite3.Error:
                log.exception("vacuum failed")
                return False
        return True
