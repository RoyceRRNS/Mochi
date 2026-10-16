"""The dashboard window and all six screens, driven through the real controller."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QPushButton

from mochi.model import GB, Level, Volume
from mochi.moods import Mood
from mochi.platform.fake import FakeProc
from mochi.ui.main_window import NAV
from mochi.ui.theme import DARK, LIGHT, THEME

from .conftest import pump

pytestmark = pytest.mark.ui


def buttons_with_text(widget, text):
    return [b for b in widget.findChildren(QPushButton) if b.text().strip() == text]


def add_hog(sim):
    sim.platform.add(FakeProc("python.exe", 7000, 1000, cpu_core=30.0, private_mb=9500, create_time=sim.clock.ts,
                              cmdline=["python", "memory_hog.py"]))


# ------------------------------------------------------------------ window
class TestWindow:
    def test_navigation_by_click_and_shortcut(self, ctrl, qtbot):
        w = ctrl.window
        for key, text, _ in NAV:
            qtbot.mouseClick(w.nav_buttons[key], Qt.LeftButton)
            assert w.current == key and w.stack.currentWidget() is w.pages[key]
            assert w.h_title.text() == w.pages[key].title
        for i, (key, _t, _i) in enumerate(NAV):
            w.navigate("home", animate=False)
            qtbot.keySequence(w, QKeySequence(f"Ctrl+{i + 1}"))
            assert w.current == key

    def test_nav_buttons_are_accessible(self, ctrl):
        for key, text, _ in NAV:
            b = ctrl.window.nav_buttons[key]
            assert b.accessibleName() == text and f"Ctrl+" in b.toolTip()
        # Qt's exclusive-group pattern: the selected item is in the Tab order, arrows move within the group,
        # and Ctrl+1..6 jump straight to a screen.
        checked = [b for b in ctrl.window.nav_buttons.values() if b.isChecked()]
        assert len(checked) == 1 and checked[0].focusPolicy() & Qt.TabFocus

    def test_inbox_shortcut_clears_badge(self, ctrl, sim, qtbot):
        sim.engine.alerts.badge_count = 4
        ctrl.window.navigate("inbox")
        assert ctrl.window.current == "home" and sim.engine.alerts.badge_count == 0

    def test_close_hides_to_tray(self, ctrl, qtbot):
        w = ctrl.window
        w.show()
        w.close()
        assert not w.isVisible() and not w.quitting and w.hint_shown

    def test_nerd_stats_toggle_persists(self, ctrl, sim):
        ctrl.window.nerd.setChecked(True)
        assert ctrl.window.nerd_on and sim.engine.base_cfg.nerd_stats
        ctrl.window.on_sample(sim.engine.latest)
        assert "Mochi uses" in ctrl.window.side_status.text()

    def test_banners(self, ctrl, sim, qtbot):
        w = ctrl.window
        w.show()
        sim.engine.update_config(demo_mode=True)
        pump(qtbot, sim, 5)
        assert w.demo_banner.isVisible()
        sim.engine.snooze(3600)
        pump(qtbot, sim, 5)
        assert w.snooze_banner.isVisible() and "snoozed" in w.snooze_banner.text.text()
        w.on_status({"stale": True, "last_sample_age": 45.0})
        assert w.stale_banner.isVisible() and "45 s" in w.stale_banner.text.text()
        w.on_status({"stale": False})
        assert not w.stale_banner.isVisible()

    def test_toast(self, ctrl):
        ctrl.window.show()
        ctrl.window.toast("Hello!")
        assert ctrl.window.toast_label.isVisible() and ctrl.window.toast_label.history[-1] == "Hello!"

    def test_theme_switch_rebuilds_with_dark_palette(self, ctrl, sim, qapp):
        sim.engine.update_config(theme="dark")
        ctrl.window.config_changed({"theme": "dark"})
        assert THEME.palette is DARK and DARK.background in qapp.styleSheet()
        assert ctrl.window.pages["home"].mochi is not None
        sim.engine.update_config(theme="light")
        ctrl.window.config_changed({"theme": "light"})
        assert THEME.palette is LIGHT

    def test_high_contrast_and_reduce_motion_follow_windows(self, ctrl, sim, qapp):
        sim.platform.contrast = True
        sim.platform.motion_reduced = True
        ctrl.apply_theme()
        assert THEME.high_contrast and THEME.reduce_motion
        assert "background" not in qapp.styleSheet()  # the system's high-contrast colours win
        assert not ctrl.window.pages["home"].mochi.animating
        sim.platform.contrast = False
        sim.platform.motion_reduced = False
        sim.engine.update_config(reduce_motion="on")
        ctrl.apply_theme()
        assert THEME.reduce_motion
        sim.engine.update_config(reduce_motion="system")
        ctrl.apply_theme()
        assert not THEME.reduce_motion and not THEME.high_contrast

    def test_sample_handling_is_fast(self, ctrl, sim):
        """UI-7: keeping the window up to date never blocks the GUI thread for long."""
        v = sim.engine.latest
        t0 = time.perf_counter()
        for _ in range(20):
            ctrl.window.on_sample(v)
        assert (time.perf_counter() - t0) / 20 < 0.1


# -------------------------------------------------------------------- home
class TestHome:
    def test_gauges_mood_and_learning(self, ctrl, sim):
        home = ctrl.window.pages["home"]
        ctrl.window.on_sample(sim.engine.latest)
        assert home.g_cpu.value_text.endswith("%") and "GB" in home.g_ram.value_text and "GB" in home.g_disk.value_text
        assert home.mood_chip.text() == "Happy"
        assert "Compared with your normal" in home.activity_label.text()

    def test_alert_appears_in_inbox_and_feedback_works(self, ctrl, sim, qtbot):
        home = ctrl.window.pages["home"]
        add_hog(sim)
        pump(qtbot, sim, 150)
        home.refresh_inbox()
        rows = [home.inbox_list.itemAt(i).widget() for i in range(home.inbox_list.count())]
        rows = [r for r in rows if hasattr(r, "ep")]
        assert rows, "the hog should be in the inbox"
        row = rows[0]
        assert "memory_hog.py" in row.ep.diagnosis.cause + row.ep.diagnosis.tip
        actions = [a.text() for a in row.more_menu.actions()]
        assert "Not a problem" in actions and "Dismiss" in actions
        qtbot.mouseClick(buttons_with_text(row, "I meant to do that")[0], Qt.LeftButton)
        assert any(s.app == "python.exe:memory_hog.py" for s in sim.engine.suppressions())
        assert "meant to be busy" in ctrl.window.toast_label.history[-1]

    def test_poke_shows_status_bubble(self, ctrl, sim, qtbot):
        home = ctrl.window.pages["home"]
        home.on_view(sim.engine.latest)
        home._poke()
        text = home.bubble.text()
        assert "CPU" in text and "Memory" in text and "Disk C:" in text

    def test_notice_row_actions(self, ctrl, sim, qtbot):
        sim.engine._notice("bench_overdue", sim.clock.ts, "Mind if I run a quick speed test?", "text", "run_benchmark")
        home = ctrl.window.pages["home"]
        home.refresh_inbox()
        btn = buttons_with_text(home, "Not now")
        assert btn
        qtbot.mouseClick(btn[0], Qt.LeftButton)
        assert sim.engine.active_notices() == []

    def test_roast_card_only_when_enabled(self, ctrl, sim, qtbot):
        home = ctrl.window.pages["home"]
        sim.engine.update_config(roasts=True)
        sim.platform.set(ram_used_gb=14.5)
        pump(qtbot, sim, 10)
        ctrl.window.show()
        assert home.roast_card.isVisibleTo(home) and "dumpling" in home.roast_text.text()


# ---------------------------------------------------------------- timeline
class TestTimeline:
    def test_draws_band_line_and_markers(self, ctrl, sim, qtbot):
        add_hog(sim)
        pump(qtbot, sim, 150)
        page = ctrl.window.pages["timeline"]
        ctrl.window.navigate("timeline", animate=False)
        page.set_metric("ram")
        data = page.last_data
        assert data["rows"] and data["markers"], "the memory alert should be marked on the memory timeline"
        assert len(page._items) >= 3 and "readings" in page.summary.text()

    def test_unavailable_metric_explains_why(self, ctrl):
        page = ctrl.window.pages["timeline"]
        ctrl.window.navigate("timeline", animate=False)
        page.set_metric("temp")
        assert page.unavailable.isVisibleTo(page) and "needs admin" in page.unavailable.text()

    def test_ranges_and_nerd_line(self, ctrl, sim):
        page = ctrl.window.pages["timeline"]
        ctrl.window.navigate("timeline", animate=False)
        for secs in (86400, 7 * 86400, 3600):
            page.set_range(secs)
            assert page.last_data is not None
        ctrl.window.nerd.setChecked(True)
        page._nerd(sim.engine.latest)
        assert "z =" in page.nerd_label.text() or "z: not trusted" in page.nerd_label.text()


# --------------------------------------------------------------- detective
class TestDetective:
    def test_lineup_prime_suspect_and_end(self, ctrl, sim, qtbot):
        add_hog(sim)
        pump(qtbot, sim, 20)
        ctrl.window.navigate("detective", animate=False)
        page = ctrl.window.pages["detective"]
        cards = [page.cards.itemAt(i).widget() for i in range(page.cards.count())]
        assert cards and cards[0].row["app"] == "python.exe:memory_hog.py" and hasattr(cards[0], "stamp")
        assert "Prime suspect: python.exe (memory_hog.py)" in page.summary.text()
        sim.platform.close_exits.add(7000)
        qtbot.mouseClick(buttons_with_text(cards[0], "End…")[0], Qt.LeftButton)
        qtbot.waitUntil(lambda: 7000 not in sim.platform.procs, timeout=5000)
        qtbot.waitUntil(lambda: "closed itself nicely" in " ".join(ctrl.window.toast_label.history), timeout=5000)

    def test_system_apps_have_no_end_button(self, ctrl, sim, qtbot):
        sim.platform.add(FakeProc("svchost.exe", 7100, 700, cpu_core=1, private_mb=20000, create_time=sim.clock.ts))
        pump(qtbot, sim, 10)
        ctrl.window.navigate("detective", animate=False)
        page = ctrl.window.pages["detective"]
        card = next(page.cards.itemAt(i).widget() for i in range(page.cards.count())
                    if page.cards.itemAt(i).widget().row["app"] == "svchost.exe")
        assert not buttons_with_text(card, "End…")


# ---------------------------------------------------------------- check-up
class TestCheckup:
    def test_speed_test_button(self, ctrl, sim, qtbot):
        ctrl.window.navigate("checkup", animate=False)
        page = ctrl.window.pages["checkup"]
        qtbot.mouseClick(page.bench_btn, Qt.LeftButton)
        qtbot.waitUntil(lambda: not page.running, timeout=60000)
        assert sim.engine.health_summary()["runs"] == 1
        assert "Speed test done" in ctrl.window.toast_label.history[-1]
        assert "more speed test" in page.score_note.text()

    def test_full_checkup_shows_report_card(self, ctrl, sim, qtbot, monkeypatch, tmp_path):
        from mochi.platform.base import DiskHealth

        sim.platform.disks = ([DiskHealth("NVMe", "SSD", "healthy", "W")], None)
        monkeypatch.setattr(sim.engine, "_load_test", lambda stop: ([99.0], []))
        sim.engine.last_scan = None
        monkeypatch.setattr("mochi.engine.folder_scan", lambda root, budget_s=20.0: __import__(
            "mochi.folderscan", fromlist=["scan"]).scan(str(tmp_path)))
        ctrl.window.navigate("checkup", animate=False)
        page = ctrl.window.pages["checkup"]
        page.start()
        qtbot.waitUntil(lambda: not page.running, timeout=90000)
        qtbot.waitUntil(lambda: page.report_card.isVisibleTo(page), timeout=5000)
        assert page.items.count() >= 6 and "Report card" in page.report_title.text()
        assert sim.engine.checkup_history()

    def test_folder_scan(self, ctrl, qtbot, tmp_path):
        (tmp_path / "big").mkdir()
        (tmp_path / "big" / "x.bin").write_bytes(b"x" * 200_000)
        page = ctrl.window.pages["checkup"]
        page.root_box.addItem("test", str(tmp_path))
        page.root_box.setCurrentIndex(page.root_box.count() - 1)
        page.start_scan(None)
        qtbot.waitUntil(lambda: page.scan_btn.isEnabled(), timeout=10000)
        assert page.folder_list.count() >= 1 and "files" in page.scan_status.text()

    def test_rebaseline(self, ctrl, sim):
        page = ctrl.window.pages["checkup"]
        page.rebaseline()
        assert sim.engine.storage.get_meta("health_rebaseline_ts")


# ------------------------------------------------------------- diary, settings
class TestDiaryAndSettings:
    def test_diary_page(self, ctrl):
        ctrl.window.navigate("diary", animate=False)
        page = ctrl.window.pages["diary"]
        assert page.entries.count() >= 1 and page.badges.count() >= 4

    def test_sensitivity_demo_roasts(self, ctrl, sim):
        ctrl.window.navigate("settings", animate=False)
        page = ctrl.window.pages["settings"]
        page.slider.setValue(4)
        assert sim.engine.cfg.sensitivity == 4 and "Watchful" in page.sens_text.text()
        page.demo.setChecked(True)
        assert sim.engine.base_cfg.demo_mode and sim.engine.cfg.heads_up_s < 300
        page.roasts.setChecked(True)
        assert sim.engine.base_cfg.roasts

    def test_mapping_editor(self, ctrl, sim, qtbot):
        ctrl.window.navigate("settings", animate=False)
        page = ctrl.window.pages["settings"]
        page.new_exe.setText("blender")
        page.new_cls.setCurrentIndex(page.new_cls.findData("media"))
        qtbot.mouseClick(buttons_with_text(page, "Add")[0], Qt.LeftButton)
        assert sim.engine.mapper.mapping["blender.exe"] == "media"
        page.new_exe.setText("bad name.exe")
        page._add_mapping()
        assert "Type an app's exe name" in ctrl.window.toast_label.history[-1]
        rows = page.table.rowCount()
        page.table.cellWidget(0, 2).click()
        assert page.table.rowCount() == rows - 1

    def test_quiet_rules_undo(self, ctrl, sim, qtbot):
        add_hog(sim)
        pump(qtbot, sim, 150)
        ep = sim.engine.inbox()[0]
        sim.engine.respond(ep.id, "intentional")
        ctrl.window.navigate("settings", animate=False)
        page = ctrl.window.pages["settings"]
        undo = buttons_with_text(page, "Undo")
        assert undo
        qtbot.mouseClick(undo[0], Qt.LeftButton)
        assert sim.engine.suppressions() == []

    def test_export_and_delete(self, ctrl, sim, qtbot):
        ctrl.window.navigate("settings", animate=False)
        page = ctrl.window.pages["settings"]
        page._export()
        qtbot.waitUntil(lambda: any("Exported to" in t for t in ctrl.window.toast_label.history), timeout=10000)
        page._delete()  # confirm is patched to "yes"
        assert sim.engine.storage.scalar("SELECT COUNT(*) FROM samples") == 0
        assert "deleted" in ctrl.window.toast_label.history[-1]

    def test_snooze_buttons(self, ctrl, sim):
        page = ctrl.window.pages["settings"]
        page._snooze(3600)
        assert "snoozed until" in page.snooze_text.text()
        page._snooze(0)
        assert page.snooze_text.text() == "alerts are on"

    def test_config_warnings_shown(self, ctrl, sim):
        sim.engine.config_warnings = ["'alpha' should be a number; using 0.01."]
        page = ctrl.window.pages["settings"]
        page.on_show()
        assert page.warn_box.count() == 1
