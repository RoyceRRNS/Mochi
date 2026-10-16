"""Alert lifecycle (ALR-1..8, NOT-4) and feedback (FB-1..4)."""

import pytest

from mochi.alerts import AlertManager, QuietState
from mochi.baseline import BaselineStore
from mochi.config import Config
from mochi.detector import Signal
from mochi.diagnosis import Diagnosis, DiagnosisEngine, Culprit
from mochi.feedback import FeedbackManager
from mochi.model import Level
from mochi.platform.fake import DEFAULT_START

from ..conftest import make_app
from .test_diagnosis import ctx_for

HOG_APPS = {"hog.exe": make_app("hog.exe", 9000, 10, pid=50)}


class StubDiagnoser(DiagnosisEngine):
    """Maps metric -> (rule, culprit) so the lifecycle can be tested on its own."""

    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = 0

    def diagnose(self, sig, ctx):
        self.calls += 1
        rule, app = self.mapping.get(sig.metric, ("unknown", None))
        c = Culprit(app, app, 50, 1.0, None) if app else None
        return Diagnosis(rule, f"{rule} cause", f"{rule} tip", c, [], sig.metric)


class Clock:
    def __init__(self):
        self.ts = DEFAULT_START
        self.mono = 1000.0

    def tick(self, dt=5.0):
        self.ts += dt
        self.mono += dt


def make_mgr(cfg=None, mapping=None, suppressed=None, persisted=None):
    cfg = cfg or Config()
    persisted = persisted if persisted is not None else []
    mgr = AlertManager(cfg, StubDiagnoser(mapping or {"ram": ("memory_pressure", "hog.exe"),
                                                      "paging": ("memory_pressure", "hog.exe"),
                                                      "disk": ("memory_pressure", "hog.exe"),
                                                      "cpu": ("runaway", "burn.exe")}),
                       is_suppressed=suppressed, persist=persisted.append)
    return mgr


def sig(metric, level=Level.HEADS_UP):
    return Signal(metric, level, "z", 1.0, 4.0)


def drive(mgr, clock, signals, seconds, quiet=None):
    events = []
    ctx = ctx_for({}, HOG_APPS)
    for _ in range(int(seconds / 5)):
        clock.tick()
        events += mgr.update(signals, ctx, clock.ts, clock.mono, quiet)
    return events


class TestLifecycle:
    def test_one_notification_per_episode_and_level(self):
        mgr, c = make_mgr(), Clock()
        ev = drive(mgr, c, [sig("ram")], 900)
        opened = [e for e in ev if e.kind == "opened"]
        assert len(opened) == 1 and opened[0].notify == "popup"
        assert len([e for e in ev if e.notify == "popup"]) == 1  # never re-notified while ongoing

    def test_escalation_within_one_episode(self):
        mgr, c = make_mgr(), Clock()
        ev = drive(mgr, c, [sig("ram", Level.PSST)], 60)
        ev += drive(mgr, c, [sig("ram", Level.HEADS_UP)], 60)
        ev += drive(mgr, c, [sig("ram", Level.UH_OH)], 60)
        kinds = [(e.kind, e.notify) for e in ev if e.kind in ("opened", "escalated")]
        assert kinds == [("opened", "badge"), ("escalated", "popup"), ("escalated", "popup")]
        assert len(mgr.open) == 1 and list(mgr.open.values())[0].max_level == Level.UH_OH

    def test_related_metrics_roll_up(self):
        mgr, c = make_mgr(), Clock()
        ev = drive(mgr, c, [sig("ram"), sig("paging"), sig("disk")], 60)
        assert len([e for e in ev if e.kind == "opened"]) == 1
        ep = list(mgr.open.values())[0]
        assert ep.metrics == {"ram", "paging", "disk"}

    def test_closes_after_two_quiet_minutes(self):
        mgr, c = make_mgr(), Clock()
        drive(mgr, c, [sig("ram")], 60)
        ev = drive(mgr, c, [], 115)
        assert not [e for e in ev if e.kind == "closed"]
        ev = drive(mgr, c, [], 10)
        assert [e for e in ev if e.kind == "closed"] and not mgr.open

    def test_cooldown_reopens_quietly_unless_worse(self):
        mgr, c = make_mgr(), Clock()
        drive(mgr, c, [sig("ram")], 30)
        drive(mgr, c, [], 130)
        ev = drive(mgr, c, [sig("ram")], 30)
        assert [e.kind for e in ev if e.kind != "updated"] == ["reopened"] and all(e.notify == "none" for e in ev)
        ev = drive(mgr, c, [sig("ram", Level.UH_OH)], 10)
        assert [(e.kind, e.notify) for e in ev] == [("escalated", "popup")]

    def test_after_cooldown_it_is_a_new_episode(self):
        mgr, c = make_mgr(Config(cooldown_s=600)), Clock()
        drive(mgr, c, [sig("ram")], 30)
        drive(mgr, c, [], 900)
        ev = drive(mgr, c, [sig("ram")], 10)
        assert [e.kind for e in ev] == ["opened"]

    def test_daily_popup_cap(self):
        mapping = {f"m{i}": (f"rule{i}", f"app{i}.exe") for i in range(8)}
        mgr, c = make_mgr(Config(daily_popup_cap=5), mapping), Clock()
        ev = drive(mgr, c, [sig(f"m{i}") for i in range(8)], 5)
        notes = sorted(e.notify for e in ev if e.kind == "opened")
        assert notes.count("popup") == 5 and notes.count("inbox") == 3

    def test_psst_is_badge_only(self):
        mgr, c = make_mgr(), Clock()
        ev = drive(mgr, c, [sig("ram", Level.PSST)], 5)
        assert ev[0].notify == "badge" and mgr.badge_count == 1
        mgr.clear_badge()
        assert mgr.badge_count == 0

    def test_snooze_holds_heads_up_but_uh_oh_gets_through(self):
        mgr, c = make_mgr(), Clock()
        quiet = QuietState(snoozed_until=c.ts + 3600)
        ev = drive(mgr, c, [sig("cpu")], 5, quiet)
        assert ev[0].notify == "held"
        ev = drive(mgr, c, [sig("ram", Level.UH_OH)], 5, quiet)
        assert any(e.notify == "popup" for e in ev)
        mgr2, c2 = make_mgr(Config(uh_oh_breaks_snooze=False)), Clock()
        ev = drive(mgr2, c2, [sig("ram", Level.UH_OH)], 5, QuietState(snoozed_until=c2.ts + 3600))
        assert ev[0].notify == "held"

    def test_shh_mode_holds_then_summarises(self):
        mgr, c = make_mgr(), Clock()
        quiet = QuietState(fullscreen=True)
        ev = drive(mgr, c, [sig("cpu")], 30, quiet)
        assert [e.notify for e in ev if e.kind == "opened"] == ["held"]
        quiet.fullscreen = False
        ev = drive(mgr, c, [sig("cpu")], 5, quiet)
        summary = [e for e in ev if e.kind == "summary"]
        assert summary and summary[0].held and summary[0].held[0].rule == "runaway"
        dnd = QuietState(dnd=True)
        assert dnd.shh()

    def test_dismissed_uh_oh_does_not_nag(self):
        mgr, c = make_mgr(), Clock()
        drive(mgr, c, [sig("ram", Level.UH_OH)], 10)
        ep = list(mgr.open.values())[0]
        mgr.dismiss(ep.id, c.ts)
        assert ep.response == "dismissed"
        assert mgr._decide(ep, Level.UH_OH, c.ts + 60, QuietState()) == "inbox"
        assert mgr._decide(ep, Level.UH_OH, c.ts + 86400 + 1, QuietState()) == "popup"

    def test_suppressed_episodes_stay_quiet_below_uh_oh(self):
        mgr, c = make_mgr(suppressed=lambda ep, ts: True), Clock()
        ev = drive(mgr, c, [sig("cpu")], 5)
        assert ev[0].notify == "suppressed" and ev[0].episode.suppressed
        ev = drive(mgr, c, [sig("cpu", Level.UH_OH)], 5)
        assert ev[0].notify == "popup"

    def test_persistence_and_queries(self):
        saved = []
        mgr, c = make_mgr(persisted=saved), Clock()
        drive(mgr, c, [sig("ram")], 10)
        drive(mgr, c, [], 130)
        assert saved and saved[-1].closed_ts is not None
        ep = saved[-1]
        assert mgr.find(ep.id) is ep and mgr.find(999) is None
        assert mgr.all_recent()[0] is ep and mgr.active() == []
        assert mgr.dismiss(12345, c.ts) is None

    def test_diagnosis_is_cached_for_a_minute(self):
        mgr, c = make_mgr(), Clock()
        drive(mgr, c, [sig("ram")], 50)
        assert mgr.diagnoser.calls == 1
        drive(mgr, c, [sig("ram")], 30)
        assert mgr.diagnoser.calls == 2

    def test_popup_budget_resets_next_day(self):
        mgr = make_mgr(Config(daily_popup_cap=1))
        mgr._count_popup(DEFAULT_START)
        assert not mgr._popup_budget_ok(DEFAULT_START + 10)
        assert mgr._popup_budget_ok(DEFAULT_START + 86400)


# --------------------------------------------------------------- feedback
@pytest.fixture
def fb():
    cfg = Config()
    store = BaselineStore(cfg)
    saved = []
    return cfg, store, FeedbackManager(cfg, store, persist=saved.append), saved


def an_episode(mgr=None, metric="cpu"):
    mgr = mgr or make_mgr()
    c = Clock()
    drive(mgr, c, [sig(metric)], 10)
    return mgr, list(mgr.open.values())[0], c


class TestFeedback:
    def test_intentional_reply_names_the_app_and_suppresses(self, fb):
        cfg, store, f, saved = fb
        _, ep, c = an_episode()
        msg = f.intentional(ep, c.ts)
        assert "burn.exe" in msg and ep.response == "intentional"
        assert f.is_suppressed(ep, c.ts + 3600)
        assert not f.is_suppressed(ep, c.ts + 31 * 86400)   # expires after 30 days
        assert saved and saved[0].app == "burn.exe" and saved[0].activity == ep.activity

    def test_suppression_scope(self, fb):
        cfg, store, f, _ = fb
        mgr, ep, c = an_episode()
        f.intentional(ep, c.ts)
        other = make_mgr(mapping={"cpu": ("runaway", "other.exe")})
        _, ep2, _ = an_episode(other)
        assert not f.is_suppressed(ep2, c.ts)  # a different app still alerts

    def test_intentional_excludes_episode_from_baseline(self, fb):
        cfg, store, f, _ = fb
        _, ep, c = an_episode()
        store.queue(ep.opened_ts + 10, 10, "browsing", "cpu", 90.0, 1)
        assert store.pending
        f.intentional(ep, c.ts)
        assert not store.pending and ep.exclusion_id is not None
        ep.closed_ts = c.ts + 100
        f.episode_closed(ep)
        assert store.exclusions[ep.exclusion_id][1] == ep.closed_ts

    def test_repeat_intentional_extends_instead_of_duplicating(self, fb):
        cfg, store, f, _ = fb
        _, ep, c = an_episode()
        f.intentional(ep, c.ts)
        f.intentional(ep, c.ts + 100)
        assert len(f.active(c.ts)) == 1

    def test_not_a_problem_is_a_label_only(self, fb):
        cfg, store, f, _ = fb
        _, ep, c = an_episode()
        assert "wasn't a problem" in f.not_a_problem(ep, c.ts)
        assert ep.response == "not_a_problem" and not f.is_suppressed(ep, c.ts)

    def test_undo_and_load(self, fb):
        cfg, store, f, saved = fb
        _, ep, c = an_episode()
        f.intentional(ep, c.ts)
        sid = f.active(c.ts)[0].id
        assert f.remove(sid) and not f.remove(sid) and not f.remove(999)
        assert not f.is_suppressed(ep, c.ts)
        f2 = FeedbackManager(cfg, store)
        f2.load(saved)
        assert f2._ids.__next__() == max(s.id for s in saved) + 1

    def test_new_normal_resets_the_context(self, fb):
        cfg, store, f, _ = fb
        for d in (1, 2):
            for i in range(40):
                store.queue(i * 5.0 + d * 1000, 10, "browsing", "cpu", 20.0, d)
        store.commit_all()
        _, ep, c = an_episode()
        assert "new normal" in f.new_normal(ep, 10, c.ts)
        assert store.get("s10|browsing", "cpu").n == 0 and ep.response == "new_normal"

    def test_intentional_without_culprit(self, fb):
        cfg, store, f, _ = fb
        mgr = make_mgr(mapping={"cpu": ("unknown", None)})
        _, ep, c = an_episode(mgr)
        assert f.intentional(ep, c.ts) == "Got it, that was on purpose. I'll stay quiet about it."
