"""Salvage a damaged database (LIF-4).

SQLite in WAL mode survives a crashed or killed process, but a database can still be
damaged by a failing disk, an antivirus tool, or a copy made while it was open. Mochi
must never refuse to start because of that. ``salvage`` copies every row it can still
read into a fresh database with the current schema: whole tables when they read
cleanly, otherwise row by row through rowid ranges, skipping only the broken pages.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class SalvageReport:
    source: str
    target: str
    copied: dict[str, int] = field(default_factory=dict)
    damaged: dict[str, str] = field(default_factory=dict)   # table -> what happened

    @property
    def rows(self) -> int:
        return sum(self.copied.values())


def is_healthy(path: Path) -> tuple[bool, str]:
    """Quick structural check. Returns (ok, detail)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        try:
            res = conn.execute("PRAGMA quick_check(5)").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return False, str(exc)
    detail = "; ".join(r[0] for r in res)
    return detail == "ok", detail


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, report: SalvageReport) -> None:
    dst_cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
    if not dst_cols:
        return
    src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
    cols = [c for c in dst_cols if c in src_cols]
    col_sql = ", ".join(cols)
    insert = f"INSERT OR IGNORE INTO {table}({col_sql}) VALUES ({', '.join('?' * len(cols))})"
    try:
        rows = src.execute(f"SELECT {col_sql} FROM {table}").fetchall()
        dst.executemany(insert, rows)
        report.copied[table] = len(rows)
        return
    except sqlite3.DatabaseError as exc:
        first_error = str(exc)
    # The table is damaged: walk rowid ranges, keeping every row on a readable page.
    try:
        hi = src.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0] or 0
    except sqlite3.DatabaseError:
        hi = 1_000_000
    kept = 0
    step = 256
    lo = 0
    while lo <= hi:
        try:
            rows = src.execute(f"SELECT {col_sql} FROM {table} WHERE rowid > ? AND rowid <= ?", (lo, lo + step)).fetchall()
            dst.executemany(insert, rows)
            kept += len(rows)
        except sqlite3.DatabaseError:
            for rid in range(lo + 1, lo + step + 1):
                try:
                    row = src.execute(f"SELECT {col_sql} FROM {table} WHERE rowid = ?", (rid,)).fetchone()
                except sqlite3.DatabaseError:
                    continue
                if row is not None:
                    dst.execute(insert, row)
                    kept += 1
        lo += step
    report.copied[table] = kept
    report.damaged[table] = f"damaged ({first_error}); kept {kept} readable rows"


def salvage(source: Path, target: Path) -> SalvageReport:
    """Copy every readable row of ``source`` into a brand-new database at ``target``."""
    from .storage import TABLES, connect, migrate

    report = SalvageReport(str(source), str(target))
    if target.exists():
        target.unlink()
    dst = connect(target)
    migrate(dst)
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.DatabaseError as exc:
        report.damaged["*"] = f"couldn't open at all ({exc})"
        dst.close()
        return report
    try:
        dst.execute("BEGIN")
        for table in TABLES:
            try:
                _copy_table(src, dst, table, report)
            except sqlite3.DatabaseError as exc:
                report.damaged[table] = f"unreadable ({exc})"
        try:
            for key, value in src.execute("SELECT key, value FROM meta WHERE key != 'schema_version'"):
                dst.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        except sqlite3.DatabaseError as exc:
            report.damaged["meta"] = f"unreadable ({exc})"
        dst.execute("COMMIT")
    finally:
        src.close()
        dst.close()
    return report


def recover_in_place(db: Path) -> SalvageReport | None:
    """If ``db`` is damaged: keep it aside as ``mochi.db.damaged-<time>``, salvage what can
    be read into a fresh ``db``, and return the report. Healthy -> None."""
    if not db.exists():
        return None
    ok, detail = is_healthy(db)
    if ok:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    keep = db.with_name(f"{db.name}.damaged-{stamp}")
    log.error("database damaged (%s); salvaging into a fresh one, original kept as %s", detail, keep.name)
    tmp = db.with_name(f"{db.name}.salvage")
    report = salvage(db, tmp)
    os.replace(db, keep)
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            os.replace(side, Path(str(keep) + suffix))
    os.replace(tmp, db)
    log.warning("salvaged %d rows; damaged tables: %s", report.rows, report.damaged or "none")
    return report
