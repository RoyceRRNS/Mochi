"""Damaged-database recovery (LIF-4) and safe shrinking after "Delete all my data"."""

import json
import os
import sqlite3

import pytest

from mochi.config import Config
from mochi.engine import Engine
from mochi.platform.fake import FakeClock, FakePlatform
from mochi.recovery import is_healthy, recover_in_place, salvage
from mochi.storage import Storage


def make_db(path, baselines=3000, samples=500):
    s = Storage(path, threaded=False)
    s.save_baselines([(f"s{i}|coding", "cpu", json.dumps({"n": i, "mean": 1.0, "var": 0.1, "days": [1, 2]}))
                      for i in range(baselines)])
    s.executemany("INSERT INTO samples(ts, cpu) VALUES (?, ?)", [(float(i), 5.0) for i in range(samples)])
    s.set_meta("first_sample_ts", "123.0")
    s.close()


def damage_table(path, table):
    """Overwrite one leaf page of ``table`` with garbage (like a bad disk sector)."""
    con = sqlite3.connect(path)
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    root = con.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    con.close()
    with open(path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        page = fh.read(page_size)
        assert page[0] == 0x05, "the table should span several pages (interior root)"
        child = int.from_bytes(page[8:12], "big")   # right-most child pointer of the interior page
        fh.seek((child - 1) * page_size)
        fh.write(os.urandom(page_size))


class TestSalvage:
    def test_damaged_table_keeps_every_readable_row(self, tmp_path):
        db = tmp_path / "m.db"
        make_db(db)
        damage_table(db, "baselines")
        ok, detail = is_healthy(db)
        assert not ok and detail
        rep = salvage(db, tmp_path / "fresh.db")
        assert rep.copied["samples"] == 500
        assert 0 < rep.copied["baselines"] < 3000          # only the broken page was lost
        assert "baselines" in rep.damaged
        assert is_healthy(tmp_path / "fresh.db")[0]

    def test_recover_in_place_keeps_the_original(self, tmp_path):
        db = tmp_path / "m.db"
        make_db(db)
        damage_table(db, "baselines")
        rep = recover_in_place(db)
        assert rep is not None and is_healthy(db)[0]
        kept = [p.name for p in tmp_path.iterdir() if ".damaged-" in p.name]
        assert any(n.startswith("m.db.damaged-") for n in kept)
        con = sqlite3.connect(db)
        assert con.execute("SELECT value FROM meta WHERE key='first_sample_ts'").fetchone()[0] == "123.0"
        con.close()

    def test_healthy_and_missing_files_are_left_alone(self, tmp_path):
        db = tmp_path / "m.db"
        assert recover_in_place(db) is None
        make_db(db, baselines=10, samples=10)
        before = db.stat().st_mtime_ns
        assert recover_in_place(db) is None and db.stat().st_mtime_ns == before

    def test_unreadable_file_still_gives_a_working_database(self, tmp_path):
        db = tmp_path / "m.db"
        db.write_bytes(b"this is not a database" * 500)
        s = Storage(db, threaded=False)
        assert s.recovered is not None and s.recovered.rows == 0
        s.set_meta("k", "v")
        assert s.get_meta("k") == "v" and s.integrity_ok()
        s.close()


class TestStartup:
    def test_storage_salvages_at_startup(self, tmp_path):
        db = tmp_path / "m.db"
        make_db(db)
        damage_table(db, "baselines")
        s = Storage(db, threaded=True)
        assert s.recovered is not None and "baselines" in s.recovered.damaged
        assert s.scalar("SELECT COUNT(*) FROM samples") == 500 and s.integrity_ok()
        s.close()

    def test_engine_tells_the_person(self, paths):
        make_db(paths.db)
        damage_table(paths.db, "baselines")
        eng = Engine(FakePlatform(FakeClock()), paths, Config(), threaded=False, start_threads=False,
                     startup_watcher=False)
        notice = next(n for n in eng.active_notices() if n.id == "db_recovered")
        assert "damaged" in notice.text and "baselines" in notice.text
        eng.stop()

    def test_no_notice_when_healthy(self, paths):
        make_db(paths.db, baselines=5, samples=5)
        eng = Engine(FakePlatform(FakeClock()), paths, Config(), threaded=False, start_threads=False,
                     startup_watcher=False)
        assert not any(n.id == "db_recovered" for n in eng.active_notices())
        eng.stop()


@pytest.mark.parametrize("threaded", [False, True])
def test_delete_all_shrinks_safely_while_writing(tmp_path, threaded):
    db = tmp_path / "m.db"
    make_db(db, baselines=4000, samples=4000)
    s = Storage(db, threaded=threaded, commit_interval=0.05)
    big = db.stat().st_size
    s.set_meta("during", "write")          # a write queued right before the delete
    s.delete_all()
    s.save_baselines([("s1|x", "cpu", json.dumps({"n": 1, "mean": 1, "var": 0, "days": []}))])
    s.flush()
    assert s.integrity_ok()
    assert s.scalar("SELECT COUNT(*) FROM samples") == 0 and s.scalar("SELECT COUNT(*) FROM baselines") == 1
    s.close()
    assert db.stat().st_size < big and is_healthy(db)[0]
    assert not s.vacuum()                  # closed: nothing happens
