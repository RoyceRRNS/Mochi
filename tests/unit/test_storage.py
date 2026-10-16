"""SQLite storage (STO-1..8, SEC-3, LIF-4)."""

import csv
import json
import sqlite3
import time

import pytest

from mochi.alerts import AlertManager
from mochi.config import Config
from mochi.feedback import Suppression
from mochi.model import GB, Volume
from mochi.storage import MIGRATIONS, SCHEMA_VERSION, TABLES, Storage, connect, migrate

from ..conftest import make_app, make_proc, make_sample, make_sys
from .test_alerts_feedback import an_episode


@pytest.fixture(params=[False, True], ids=["inline", "threaded"])
def store(request, tmp_path):
    s = Storage(tmp_path / "m.db", commit_interval=0.05, threaded=request.param)
    yield s
    s.close()


def sample_at(ts, **kw):
    s = make_sample(ts=ts, sys=make_sys(**kw), procs=[make_proc(10, "chrome.exe", 5, 300), make_proc(11, "a.exe", 1, 10)])
    s.metrics["cpu"] = kw.get("cpu_total", 5.0)
    return s


class TestStorage:
    def test_wal_mode_and_schema(self, store):
        assert store.scalar("PRAGMA journal_mode") == "wal"
        assert store.version == SCHEMA_VERSION == len(MIGRATIONS)
        names = {r[0] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(TABLES) <= names

    def test_samples_and_processes_roundtrip(self, store):
        for i in range(5):
            store.save_sample(sample_at(1000 + i * 5, cpu_total=10 + i), {"cpu": (8.0, 2.0)}, "slot+activity")
        assert store.flush()
        rows = store.series("cpu", 0, 2000)
        assert [r[1] for r in rows] == [10, 11, 12, 13, 14] and rows[0][2:] == (8.0, 2.0)
        assert store.scalar("SELECT COUNT(*) FROM proc_samples") == 10
        with pytest.raises(ValueError):
            store.series("not_a_metric", 0, 1)

    def test_only_exe_names_stored_never_titles_or_command_lines(self, store):
        s = sample_at(1000)
        s.sys.foreground_exe = "chrome.exe"
        s.sys.foreground_path = r"C:\Users\me\secret\chrome.exe"
        store.save_sample(s, {}, None)
        store.save_apps(1000, {"python.exe:memory_hog.py": make_app("python.exe:memory_hog.py", 100,
                                                                    display="python.exe (memory_hog.py)")})
        store.flush()
        dump = "\n".join(str(r) for t in TABLES for r in store.query(f"SELECT * FROM {t}"))
        assert "secret" not in dump and "--token" not in dump
        assert "chrome.exe" in dump

    def test_alert_upsert_and_ids(self, store):
        mgr, ep, c = an_episode()
        store.upsert_alert(ep)
        ep.response = "intentional"
        store.upsert_alert(ep)
        store.flush()
        rows = store.alerts_between(0, 1e12)
        assert len(rows) == 1 and rows[0]["response"] == "intentional" and rows[0]["rule"] == "runaway"
        assert store.max_alert_id() == ep.id

    def test_suppressions_meta_events_baselines(self, store):
        store.save_suppression(Suppression(1, "a.exe", "cpu", "coding", 1.0, 2.0, True, "label"))
        store.set_meta("k", "v")
        store.add_event(5.0, "feed", {"bytes": 10})
        store.save_baselines([("s1|x", "cpu", json.dumps({"n": 1, "mean": 2, "var": 0, "days": []}))])
        store.save_baseline_snapshot(1.0, [("s1|x", "cpu", 2.0, 0.5, 40)])
        store.flush()
        assert store.load_suppressions()[0][1] == "a.exe"
        assert store.get_meta("k") == "v" and store.get_meta("missing", "d") == "d"
        assert store.events("feed") == [(5.0, {"bytes": 10})] and store.events("feed", since=6.0) == []
        assert store.load_baselines()[0][0] == "s1|x"

    def test_usage_seconds(self, store):
        for i in range(12):
            s = sample_at(1000 + i * 5)
            s.slot, s.activity = 10, "coding"
            store.save_sample(s, {}, None)
        store.flush()
        assert store.usage_seconds(0) == {(10, "coding"): 60.0}

    def test_retention_and_downsampling(self, store):
        day = 86400
        now = 100 * day
        for i in range(120):  # 10 minutes, 20 days ago
            store.save_sample(sample_at(now - 20 * day + i * 5, cpu_total=10.0), {"cpu": (9.0, 1.0)}, None)
        for i in range(120):  # 10 minutes, 1 day ago
            store.save_sample(sample_at(now - day + i * 5, cpu_total=20.0), {"cpu": (9.0, 1.0)}, None)
        store.save_volumes(now - 20 * day, [Volume("C:\\", GB, GB)])
        stats = store.maintain(now, raw_days=14, minute_days=90)
        assert stats["minutes"] == 20 and stats["deleted_samples"] == 120 and stats["deleted_volumes"] == 1
        assert store.scalar("SELECT COUNT(*) FROM samples") == 120
        assert store.scalar("SELECT COUNT(*) FROM agg_minute") == 20
        assert store.scalar("SELECT COUNT(*) FROM agg_hour") == 2
        old = store.series("cpu", now - 21 * day, now - 19 * day, "minute")
        assert old and old[0][1] == pytest.approx(10.0)
        again = store.maintain(now, raw_days=14, minute_days=90)
        assert again["minutes"] == 0  # idempotent
        store.maintain(now + 200 * day, raw_days=14, minute_days=90)
        assert store.scalar("SELECT COUNT(*) FROM agg_minute") == 0
        assert store.scalar("SELECT COUNT(*) FROM agg_hour") == 2  # hourly is kept forever

    def test_export_and_delete_all(self, store, tmp_path):
        store.save_sample(sample_at(1000), {}, None)
        store.add_event(1.0, "feed", {})
        out = store.export(tmp_path / "exports")
        assert (out / "samples.csv").exists() and json.loads((out / "summary.json").read_text())["tables"]["samples"] == 1
        with (out / "samples.csv").open() as fh:
            assert next(csv.reader(fh))[0] == "ts"
        store.delete_all()
        assert all(store.scalar(f"SELECT COUNT(*) FROM {t}") == 0 for t in TABLES)
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)

    def test_integrity_and_size(self, store):
        store.save_sample(sample_at(1000), {}, None)
        store.flush()
        assert store.integrity_ok() and store.size_mb() > 0


def test_migration_from_v1_keeps_data(tmp_path):
    db = tmp_path / "old.db"
    conn = connect(db)
    assert migrate(conn, upto=1) == 1
    conn.execute("INSERT INTO samples(ts, cpu) VALUES (1.0, 42.0)")
    conn.close()
    s = Storage(db, threaded=False)
    assert s.version == SCHEMA_VERSION
    assert s.scalar("SELECT cpu FROM samples WHERE ts=1.0") == 42.0
    assert s.scalar("SELECT COUNT(*) FROM launches") == 0  # v2 table now exists
    s.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import mochi.storage as st

    monkeypatch.setattr(st, "MIGRATIONS", st.MIGRATIONS + ["CREATE TABLE broken (x INTEGER); SELECT * FROM nope"])
    conn = connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.OperationalError):
        st.migrate(conn, upto=len(st.MIGRATIONS))
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(len(MIGRATIONS))
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name='broken'").fetchone()


def test_writer_survives_bad_statement(tmp_path):
    s = Storage(tmp_path / "w.db", commit_interval=0.05, threaded=True)
    s.execute("INSERT INTO no_such_table VALUES (1)")
    s.set_meta("after", "ok")
    assert s.flush()
    assert s.errors == 1
    s.set_meta("after2", "ok")
    s.flush()
    assert s.get_meta("after2") == "ok"
    assert s.close()


def test_batched_commits_visible_to_other_connections(tmp_path):
    s = Storage(tmp_path / "b.db", commit_interval=60.0, threaded=True)
    s.set_meta("x", "1")
    time.sleep(0.2)
    other = sqlite3.connect(tmp_path / "b.db")
    assert other.execute("SELECT value FROM meta WHERE key='x'").fetchone() is None  # not committed yet (batched)
    s.flush()
    assert other.execute("SELECT value FROM meta WHERE key='x'").fetchone()[0] == "1"
    other.close()
    s.close()


def test_writes_after_close_are_ignored(tmp_path):
    for threaded in (False, True):
        s = Storage(tmp_path / f"c{threaded}.db", threaded=threaded)
        assert s.close() and s.closed
        s.set_meta("late", "write")      # no crash, nothing written
        assert s.flush() and s.close()   # closing twice is fine
