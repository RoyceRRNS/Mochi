"""Widgets, the mascot (MOOD-3/4, UI-8), the tray (UI-1) and popups (NOT-1..3)."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from mochi.model import Level
from mochi.moods import LABELS, Mood
from mochi.ui.mascot import MochiWidget, ease_out_back, mochi_pixmap
from mochi.ui.popup import Popup, PopupManager
from mochi.ui.theme import DARK, LIGHT, THEME
from mochi.ui.tray import TRAY_SIZES, TrayController, tray_icon, tray_tooltip
from mochi.ui.widgets import (Avatar, Banner, Chip, Dot, FlowLayout, JellyBar, RiceCakes, RingGauge, SpeechBubble,
                              icon_pixmap, level_chip, sky_pixmap, status_chip)

pytestmark = pytest.mark.ui


def img(widget_or_pixmap) -> QImage:
    pm = widget_or_pixmap.grab() if isinstance(widget_or_pixmap, QWidget) else widget_or_pixmap
    return pm.toImage()


def non_blank(image: QImage) -> bool:
    first = image.pixel(0, 0)
    return any(image.pixel(x, y) != first for x in range(0, image.width(), 3) for y in range(0, image.height(), 3))


# ---------------------------------------------------------------- widgets
class TestWidgets:
    def test_ring_gauge_states_and_accessibility(self, qtbot):
        g = RingGauge("CPU")
        qtbot.addWidget(g)
        g.set(42, "42%", "usual 5-12%", (5, 12), "warning")
        assert g.value == 42 and "CPU: 42%" in g.accessibleDescription() and "usual" in g.toolTip()
        g.set(250, "x", state="critical")
        assert g.value == 100
        g.set(None, "--")
        assert g.value == 0
        g.resize(160, 180)
        assert non_blank(img(g))

    def test_jelly_bar_ricecakes_bubble_banner(self, qtbot):
        bar = JellyBar()
        bar.set_segments([(0.5, "accent", "accent_strong"), (0.2, "info", "info_strong")], "7 of 16 GB")
        bar.resize(300, 16)
        cakes = RiceCakes()
        cakes.set_count(5)
        cakes.resize(300, 30)
        for w in (bar, cakes):
            qtbot.addWidget(w)
            assert non_blank(img(w))
        assert "5 calm days" in cakes.toolTip() and cakes.accessibleName() == "Calm streak: 5 days"
        b = Banner("hello", "warning")
        s = SpeechBubble("hi")
        qtbot.addWidget(b)
        qtbot.addWidget(s)
        b.restyle()
        s.restyle()
        assert THEME.palette.warning in b.styleSheet()

    def test_chips(self, qtbot):
        c = level_chip(3)
        assert c.text() == "Uh-oh" and c.fill == "critical" and c.accessibleName() == "Level: Uh-oh"
        assert status_chip("look").text() == "Worth a look"
        assert Chip("x").fill == "surface_alt"

    @pytest.mark.parametrize("name", ["home", "timeline", "detective", "checkup", "diary", "settings", "bell", "refresh",
                                      "folder", "slow", "close", "stop", "play", "moon", "export", "trash", "sparkle",
                                      "unknown"])
    def test_every_icon_draws(self, name):
        assert non_blank(icon_pixmap(name, "#000000", 20).toImage())

    def test_icons_differ(self):
        assert icon_pixmap("home", "#000000").toImage() != icon_pixmap("diary", "#000000").toImage()

    @pytest.mark.parametrize("sky", ["Mostly sunny", "Partly cloudy", "Scattered showers", "Stormy"])
    def test_weather_drawings(self, sky):
        assert non_blank(sky_pixmap(sky).toImage())

    def test_flow_layout_wraps(self, qtbot):
        host = QWidget()
        lay = FlowLayout(host, spacing=10)
        for _ in range(6):
            w = QWidget()
            w.setFixedSize(100, 40)
            lay.addWidget(w)
        qtbot.addWidget(host)
        assert lay.heightForWidth(1000) == 40
        assert lay.heightForWidth(230) == 3 * 40 + 2 * 10
        assert lay.count() == 6 and lay.itemAt(99) is None and lay.takeAt(99) is None

    def test_dot_and_avatar(self, qtbot):
        d, a = Dot("good_strong"), Avatar("python.exe")
        for w in (d, a):
            qtbot.addWidget(w)
            assert non_blank(img(w))
        assert a.letter == "P"


# ----------------------------------------------------------------- mascot
class TestMascot:
    def test_every_mood_draws_differently_in_both_themes(self):
        images = {}
        for pal in (LIGHT, DARK):
            for m in Mood:
                image = mochi_pixmap(m, 96, pal, face_only=False).toImage()
                assert non_blank(image)
                images[(pal.name, m)] = image
        assert len({(k[0], im.cacheKey()) for k, im in images.items()}) == len(images)
        assert images[("light", Mood.HAPPY)] != images[("light", Mood.DIZZY)]

    def test_face_only_at_tray_sizes(self):
        assert mochi_pixmap(Mood.HAPPY, 16).toImage() != mochi_pixmap(Mood.HAPPY, 16, face_only=False).toImage()
        badge = mochi_pixmap(Mood.HAPPY, 32, badge="psst").toImage()
        assert badge != mochi_pixmap(Mood.HAPPY, 32).toImage()

    def test_mood_change_updates_accessibility(self, qtbot):
        w = MochiWidget(120)
        qtbot.addWidget(w)
        w.set_mood(Mood.GRUMPY)
        assert w.accessibleName() == "Mochi is grumpy" and LABELS[Mood.GRUMPY][1] in w.accessibleDescription()

    def test_animates_only_while_visible(self, qtbot):
        """UI-8: no animation work while the window is hidden."""
        w = MochiWidget(120)
        qtbot.addWidget(w)
        w.show()
        qtbot.waitUntil(lambda: w.animating)
        phase = w.phase
        qtbot.wait(120)
        assert w.phase != phase
        w.hide()
        assert not w.animating

    def test_reduce_motion_stops_the_loop(self, qtbot):
        w = MochiWidget(120)
        qtbot.addWidget(w)
        w.show()
        w.set_reduce_motion(True)
        assert not w.animating
        w.start_squish()
        assert w._squish_start is None  # no squish either

    def test_poke_by_click_and_keyboard(self, qtbot):
        w = MochiWidget(120)
        qtbot.addWidget(w)
        w.show()
        with qtbot.waitSignal(w.poked, timeout=1000):
            qtbot.mouseClick(w, Qt.LeftButton)
        with qtbot.waitSignal(w.poked, timeout=1000):
            qtbot.keyClick(w, Qt.Key_Space)
        qtbot.waitUntil(lambda: w.squish == 1.0, timeout=2000)

    def test_ease_out_back(self):
        assert ease_out_back(0) == pytest.approx(0) and ease_out_back(1) == pytest.approx(1)
        assert max(ease_out_back(t / 100) for t in range(101)) > 1.0  # it overshoots, then settles


# ------------------------------------------------------------------- tray
class TestTray:
    def test_icon_has_all_sizes(self):
        ic = tray_icon(Mood.SWEATY, "psst")
        assert {s.width() for s in ic.availableSizes()} >= set(TRAY_SIZES)

    def test_tooltip(self):
        t = tray_tooltip(Mood.PUFFY, 3, 0.0)
        assert t.startswith("Mochi: Puffy.") and "3 new in your inbox" in t and len(t) <= 127
        assert "snoozed until" in tray_tooltip(Mood.HAPPY, 0, 10_000.0, now=1.0)
        assert len(tray_tooltip(Mood.DIZZY, 99, 10_000.0, now=1.0)) <= 127

    def test_menu_actions(self, qtbot):
        calls = []
        t = TrayController(lambda: calls.append("open"), lambda: calls.append("check"),
                           lambda s: calls.append(("snooze", s)), lambda: calls.append("quit"))
        t.act_open.trigger()
        t.act_check.trigger()
        t.act_snooze_hour.trigger()
        t.act_snooze_tomorrow.trigger()
        t.act_quit.trigger()
        assert calls == ["open", "check", ("snooze", 3600), ("snooze", None), "quit"]
        t.update(Mood.DIZZY, 2, 0.0)
        assert t.badge == "psst" and t.mood == Mood.DIZZY and "Dizzy" in t.tray.toolTip()
        assert not t.act_resume.isEnabled()
        t.hide()


# ----------------------------------------------------------------- popups
def make_popup(level=2, persistent=False, timeout=0.3, primary=None, secondary=None, link=None):
    return Popup("Heads up!", "cause line", "one tip", Mood.PUFFY, level, persistent, primary, secondary, link,
                 timeout_s=timeout, reduce_motion=True)


class TestPopups:
    def test_never_takes_focus(self, qtbot):
        p = make_popup()
        qtbot.addWidget(p)
        assert p.windowFlags() & Qt.WindowDoesNotAcceptFocus
        assert p.testAttribute(Qt.WA_ShowWithoutActivating) and p.focusPolicy() == Qt.NoFocus
        assert all(b.focusPolicy() == Qt.NoFocus for b in p.buttons)

    def test_heads_up_fades_and_uh_oh_persists(self, qtbot):
        p = make_popup(timeout=0.2)
        u = make_popup(level=3, persistent=True, timeout=0.2)
        for i, x in enumerate((p, u)):
            x.move(900, 300 + i * 200)   # away from the (offscreen) cursor, like the real stack
            x.fade_in()
        with qtbot.waitSignal(p.closed, timeout=2000):
            pass
        qtbot.wait(400)
        assert u.isVisible()
        u.dismiss()

    def test_hover_pauses_the_fade(self, qtbot):
        p = make_popup(timeout=0.2)
        p.fade_in()
        p.enterEvent(None)
        qtbot.wait(400)
        assert p.isVisible()
        p.dismiss()

    def test_buttons_run_and_close(self, qtbot):
        calls = []
        p = make_popup(primary=("Show me", lambda: calls.append("primary")),
                       secondary=("I meant to do that", lambda: calls.append("intentional")),
                       link=("Not a problem", lambda: calls.append("nap")))
        p.fade_in()
        with qtbot.waitSignal(p.closed, timeout=1000):
            qtbot.mouseClick(p.buttons[1], Qt.LeftButton)
        assert calls == ["intentional"]
        assert [b.text() for b in p.buttons] == ["Show me", "I meant to do that", "Not a problem"]

    def test_manager_stacks_at_most_three(self, qtbot, sim):
        mgr = PopupManager(sim.engine, lambda k: None, lambda i, k: None, lambda a, i: None)
        mgr.reduce_motion = True
        for i in range(5):
            mgr._add(make_popup(timeout=30))
        assert len(mgr.popups) == 3
        ys = [p.y() for p in mgr.popups]
        assert ys == sorted(ys)  # newest at the bottom
        mgr.close_all()
        assert mgr.popups == []


def test_late_background_result_for_a_closed_window_is_dropped(qtbot, sim):
    from mochi.ui.bridge import EngineBridge
    from mochi.ui.widgets import Banner

    bridge = EngineBridge(sim.engine)
    b = Banner("x")
    b.deleteLater()
    qtbot.wait(10)
    errors = []
    bridge.run_async(lambda: 42, lambda r: b.text.setText(str(r)), lambda e: errors.append(e))
    qtbot.wait(200)
    assert errors == []  # the RuntimeError for the deleted widget was swallowed, not raised in the event loop

    got = []
    bridge.run_async(lambda: 1 / 0, None, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=2000)
    assert isinstance(got[0], ZeroDivisionError)
