"""Quirks (QRK-1..8), the colour contract (DS-1, DS-2) and privacy (SEC-1, SEC-3)."""

import ast
import json
from pathlib import Path

import pytest

from mochi.model import GB, MB, Battery
from mochi.quirks import (BootTracker, FeedTracker, calm_streak, data_days, day_start, diary, evaluate_achievements,
                          human_count, os_fact, records, roast, snack_for, weather)
from mochi.storage import Storage
from mochi.ui.theme import DARK, LIGHT, STROKE_PAIRS, TEXT_PAIRS, contrast

from ..conftest import make_app, make_sample, make_sys

ROOT = Path(__file__).resolve().parents[2]
NOW = day_start(1_792_000_000.0) + 10 * 3600  # 10:00 on some day


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "q.db", threaded=False)
    yield s
    s.close()


def add_day(store, day_ts, hours=2, start_hour=9, ctx0=1_000_000, activity="coding", ram=5.0):
    rows = []
    for i in range(int(hours * 720)):
        ts = day_ts + start_hour * 3600 + i * 5
        rows.append((ts, 10, activity, 5.0, ram, ctx0 + i * 1000, 500 + i * 10, 9000 + i))
    store.executemany("INSERT INTO samples(ts, slot, activity, cpu, ram, ctx, irq, syscalls) VALUES (?,?,?,?,?,?,?,?)", rows)


def add_alert(store, ts, level=2, rule="memory_hog", culprit="Chrome", response=None, aid=None):
    aid = aid or int(ts)
    store.execute("INSERT INTO alerts(id, rule, target, level, max_level, opened_ts, culprit_display, response, metrics)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", (aid, rule, culprit, level, level, ts, culprit, response, "ram"))


class TestDataDaysWeatherFacts:
    def test_data_days_need_an_hour(self, store):
        today = day_start(NOW)
        add_day(store, today - 86400, hours=2)
        add_day(store, today - 2 * 86400, hours=0.5)
        assert data_days(store, NOW) == [today - 86400]

    def test_weather_hidden_until_three_days(self, store):
        today = day_start(NOW)
        for d in (1, 2):
            add_day(store, today - d * 86400)
        assert weather(store, NOW) is None

    def test_weather_sky_and_chance(self, store):
        today = day_start(NOW)
        for d in (1, 2, 3, 4):
            add_day(store, today - d * 86400)
            for h in (18, 20):
                store.execute("INSERT INTO app_mem VALUES (?,?,?,?,?,?,?)",
                              (today - d * 86400 + h * 3600, "chrome.exe", "Chrome", 3000 if d != 4 else 100, None, 9, 1))
                store.execute("INSERT INTO app_mem VALUES (?,?,?,?,?,?,?)",
                              (today - d * 86400 + h * 3600, "code.exe", "VS Code", 1000, None, 3, 1))
        add_alert(store, today - 86400 + 15 * 3600, level=2)
        w = weather(store, NOW)
        assert w == "Scattered showers, with a 80% chance of Chrome by evening." or w.startswith("Scattered showers, with a 70%")

    def test_os_fact_handles_reboot(self, store):
        today = day_start(NOW)
        store.executemany("INSERT INTO samples(ts, ctx, irq, syscalls) VALUES (?,?,?,?)", [
            (today + 100, 1000, 10, 1), (today + 105, 3000, 30, 2), (today + 110, 50, 5, 1), (today + 115, 1050, 15, 2)])
        f = os_fact(store, NOW)
        assert f["ctx_switches"] == 3000 and f["interrupts"] == 30
        assert "switched between tasks 3 thousand times" in f["text"]
        assert os_fact(store, NOW + 86400) is None

    def test_human_count(self):
        assert human_count(4_200_000) == "4.2 million" and human_count(2.5e9) == "2.5 billion"
        assert human_count(12) == "12" and human_count(5300) == "5 thousand"


class TestFeedAndRoasts:
    def test_feed_only_after_a_tip_within_ten_minutes(self):
        ft = FeedTracker(600)
        ft.note_tip("hog.exe", "hog.exe", 0.0, 2 * 1024 ** 3)
        assert ft.observe(60.0, {"hog.exe": make_app("hog.exe", 2500)}) == []
        ev = ft.observe(120.0, {})
        assert len(ev) == 1 and ev[0].snack == "a whole dango" and "2.4 GB" in ev[0].text
        ft.note_tip("x.exe", "x.exe", 0.0, 100)
        assert ft.observe(700.0, {}) == []  # closed too late: no snack
        assert ft.observe(800.0, {}) == []  # no tip, no snack

    def test_snacks(self):
        assert snack_for(3 * 1024 ** 3) == "a whole dango"
        assert snack_for(600 * MB) == "a mochi bite"
        assert snack_for(10) == "a rice crumb"

    def test_roasts_off_by_default_and_only_measurable(self):
        s = make_sample(sys=make_sys(ram_available=1 * GB), apps={"chrome.exe": make_app("chrome.exe", 4000, procs=63)})
        assert roast(s, enabled=False) is None
        text = roast(s, enabled=True)
        assert text == "chrome.exe is running 63 processes. Are you collecting them?"
        assert "tab" not in text.lower()
        calm = make_sample(sys=make_sys(ram_available=12 * GB))
        assert roast(calm, True) is None
        assert "days since your last restart" in roast(calm, True, uptime_s=9 * 86400)


class TestAchievementsStreaksDiaryRecords:
    def test_calm_streak_skips_days_without_data(self, store):
        today = day_start(NOW)
        add_day(store, today)
        add_day(store, today - 86400)
        # day -2 has no data: neither counts nor breaks
        add_day(store, today - 3 * 86400)
        add_day(store, today - 4 * 86400)
        add_alert(store, today - 4 * 86400 + 9.5 * 3600, level=3)
        assert calm_streak(store, NOW) == 3

    def test_tab_tamer_needs_five_active_days(self, store):
        today = day_start(NOW)
        for d in range(4):
            add_day(store, today - d * 86400)
        a = {x.key: x for x in evaluate_achievements(store, NOW, True, False)}
        assert a["tab_tamer"].unlocked_ts is None and "4/5 active days" in a["tab_tamer"].progress
        add_day(store, today - 4 * 86400)
        a = {x.key: x for x in evaluate_achievements(store, NOW, True, False)}
        assert a["tab_tamer"].unlocked_ts == NOW
        assert ("tab_tamer",) in store.query("SELECT key FROM achievements")

    def test_memory_alert_blocks_tab_tamer_and_hidden_achievements(self, store):
        today = day_start(NOW)
        for d in range(6):
            add_day(store, today - d * 86400)
        add_alert(store, today - 86400 + 9.2 * 3600, rule="memory_hog")
        a = {x.key: x for x in evaluate_achievements(store, NOW, throttle_metric_available=False, boot_measured=False)}
        assert a["tab_tamer"].unlocked_ts is None
        assert a["cool_cucumber"].hidden and a["speed_demon"].hidden

    def test_clean_sweep_speed_demon_well_fed(self, store):
        for i in range(5):
            store.add_event(i, "disk_freed", {"gb": 2.5})
            store.add_event(i, "feed", {"bytes": 1})
        store.add_event(1, "boot", {"duration_s": 40})
        store.add_event(2, "boot", {"duration_s": 30})
        a = {x.key: x for x in evaluate_achievements(store, NOW, True, True)}
        assert a["clean_sweep"].unlocked_ts and a["well_fed"].unlocked_ts and a["speed_demon"].unlocked_ts

    def test_diary_entries(self, store):
        today = day_start(NOW)
        add_day(store, today)
        add_day(store, today - 86400)
        add_alert(store, today + 9.5 * 3600, rule="memory_hog", culprit="Premiere", aid=1)
        add_alert(store, today + 9.7 * 3600, rule="memory_hog", culprit="Chrome", aid=2, response="intentional")
        store.add_event(today + 9.8 * 3600, "feed", {"bytes": 2 * 1024 ** 3})
        entries = diary(store, NOW)
        assert entries and entries[0].title == "This week so far"
        text = entries[0].text
        assert "was rough" in text and "very puffy" in text and "on purpose" in text and "snack" in text

    def test_diary_calm_week(self, store):
        add_day(store, day_start(NOW))
        assert "calm week" in diary(store, NOW)[0].text
        assert diary(store, NOW, weeks=1)

    def test_records(self, store):
        assert records(store) == {}
        store.add_event(1, "boot", {"duration_s": 42})
        store.add_event(1, "uptime", {"seconds": 3 * 86400})
        add_day(store, day_start(NOW), activity="idle", ram=3.2)
        store.execute("INSERT INTO launches(app, ts, duration_s, cold, settled) VALUES ('code.exe', 1, 2.1, 0, 1)")
        store.execute("INSERT INTO benchmarks(ts, score, valid) VALUES (1, 97, 1)")
        r = records(store)
        assert r["Fastest boot"] == "42 s" and r["Longest uptime (since last restart)"] == "3.0 days"
        assert r["Lowest idle memory"] == "3.2 GB" and r["Fastest app launch"] == "code.exe in 2.1 s"
        assert r["Best Health Score"] == "97"

    def test_boot_tracker(self):
        bt = BootTracker(1000.0, 1060.0, already_recorded=False)
        assert bt.observe(1070, 80) is None and bt.observe(1080, 10) is None
        assert bt.observe(1090, 10) == pytest.approx(80.0)
        assert not bt.active
        assert not BootTracker(1000.0, 1500.0, False).active          # Mochi started too late
        assert not BootTracker(1000.0, 1060.0, True).active           # already measured this boot
        late = BootTracker(1000.0, 1010.0, False)
        assert late.observe(2000, 50) is None and not late.active      # gives up after 15 minutes


# --------------------------------------------------------------- colours
class TestTheme:
    @pytest.mark.parametrize("pal", [LIGHT, DARK], ids=["light", "dark"])
    def test_text_pairs_meet_wcag_aa(self, pal):
        for fg, bg in TEXT_PAIRS:
            ratio = contrast(getattr(pal, fg), getattr(pal, bg))
            assert ratio >= 4.5, f"{pal.name}: {fg} on {bg} is {ratio:.2f}:1"

    @pytest.mark.parametrize("pal", [LIGHT, DARK], ids=["light", "dark"])
    def test_strokes_meet_non_text_contrast(self, pal):
        for fg, bg in STROKE_PAIRS:
            ratio = contrast(getattr(pal, fg), getattr(pal, bg))
            assert ratio >= 3.0, f"{pal.name}: {fg} on {bg} is {ratio:.2f}:1"

    def test_original_palette_problems_are_fixed(self):
        assert contrast("#8C7B75", "#FFF8F0") < 4.5 <= contrast(LIGHT.text_muted, LIGHT.background)
        assert contrast("#F3EDE7", "#FF9FB2") < 4.5 <= contrast(DARK.on_fill, DARK.accent)

    def test_contrast_maths(self):
        assert contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
        assert contrast("#777777", "#777777") == pytest.approx(1.0)

    def test_never_pure_black_text(self):
        assert LIGHT.text != "#000000" and DARK.text != "#000000"


# --------------------------------------------------------------- privacy
NETWORK_MODULES = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib",
                   "urllib3", "websocket", "ssl"}


def test_no_network_code_anywhere():
    """SEC-1: nothing in Mochi imports a network library."""
    offenders = []
    for path in (ROOT / "mochi").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in NETWORK_MODULES:
                    offenders.append(f"{path.name}: {n}")
    assert offenders == []


def test_signature_check_is_offline():
    src = (ROOT / "mochi" / "platform" / "windows.py").read_text(encoding="utf-8")
    assert "0x1000 | 0x10" in src  # WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_NONE


def test_window_titles_are_never_read():
    """SEC-3: Mochi never calls GetWindowText, so titles can't be stored."""
    for path in (ROOT / "mochi").rglob("*.py"):
        assert "GetWindowText" not in path.read_text(encoding="utf-8"), path


def test_gitignore_keeps_personal_data_out_of_the_repo():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.db", "*.db-wal", "logs/", "exports/", ".venv/"):
        assert pattern in gi
