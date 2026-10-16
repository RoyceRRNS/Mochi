"""Baselines (DET-1..3, DET-13, CTX-5, CTX-6, FB-2) and detection (DET-2..12, LIF-5)."""

import math
import random

import pytest

from mochi.baseline import BaselineStore, Stat
from mochi.config import Config
from mochi.detector import Detector, SustainTracker, level_floor_ok, min_std_for
from mochi.model import GB, Level, Volume

from ..conftest import make_sample, make_sys

DAY0 = 739_900


def reference_ewma(xs, alpha):
    """Independent textbook implementation used to check Stat.update."""
    mean, var, n = 0.0, 0.0, 0
    for x in xs:
        a = max(alpha, 1.0 / (n + 1))
        d = x - mean
        mean += a * d
        var = (1 - a) * (var + a * d * d)
        n += 1
    return mean, var


# ------------------------------------------------------------------- Stat
class TestStat:
    def test_matches_reference_to_1e9(self):
        rng = random.Random(7)
        xs = [rng.gauss(10, 2) for _ in range(500)]
        st = Stat()
        for x in xs:
            st.update(x, 0.01, DAY0)
        mean, var = reference_ewma(xs, 0.01)
        assert st.mean == pytest.approx(mean, abs=1e-9) and st.var == pytest.approx(var, abs=1e-9)

    def test_seeded_from_first_samples_not_zero(self):
        st = Stat()
        st.update(50.0, 0.01, DAY0)
        assert st.mean == 50.0 and st.var == 0.0
        st.update(52.0, 0.01, DAY0)
        assert st.mean == pytest.approx(51.0) and st.std == pytest.approx(1.0)

    def test_trusted_needs_samples_and_two_days(self):
        st = Stat()
        for _ in range(100):
            st.update(1.0, 0.01, DAY0)
        assert not st.trusted(30, 2)  # many samples but a single day
        st.update(1.0, 0.01, DAY0 + 1)
        assert st.trusted(30, 2)
        assert not Stat(n=10, days=[1, 2]).trusted(30, 2)

    def test_day_history_is_bounded(self):
        st = Stat()
        for d in range(20):
            st.update(1.0, 0.01, DAY0 + d)
        assert len(st.days) == 8 and st.days[-1] == DAY0 + 19

    def test_json_roundtrip(self):
        st = Stat(5, 1.5, 0.25, [1, 2], [3])
        assert Stat.from_json(st.to_json()) == st


# --------------------------------------------------------------- the store
def fill(store, slot, act, metric, value, days, n=40, ts0=1_000_000.0):
    t = ts0
    for d in days:
        for _ in range(n):
            store.queue(t, slot, act, metric, value, d)
            t += 5
    store.commit_all()
    return t


class TestBaselineStore:
    def test_fallback_chain(self):
        store = BaselineStore(Config())
        fill(store, 10, "coding", "cpu", 20.0, [DAY0, DAY0 + 1])
        st, level, key = store.lookup(10, "coding", "cpu")
        assert level == "slot+activity" and key == "s10|coding" and st.mean == pytest.approx(20.0)
        st, level, key = store.lookup(11, "coding", "cpu")       # same activity, other hour
        assert level == "activity" and key == "*|coding"
        st, level, key = store.lookup(3, "gaming", "cpu")        # nothing specific: global
        assert level == "global"
        assert store.lookup(3, "gaming", "ram") == (None, None, None)

    def test_delayed_commit(self):
        cfg = Config(commit_delay_s=1200)
        store = BaselineStore(cfg)
        store.queue(1000.0, 10, "coding", "cpu", 5.0, DAY0)
        assert store.commit_due(1000.0 + 600) == 0 and store.get("s10|coding", "cpu") is None
        assert store.commit_due(1000.0 + 1200) == 1 and store.get("s10|coding", "cpu").n == 1

    def test_exclusion_equals_never_seeing_the_episode(self):
        """FB-2: marking an episode intentional gives exactly the baseline without it."""
        cfg = Config()
        a, b = BaselineStore(cfg), BaselineStore(cfg)
        normal = [(1000 + i * 5, 10.0 + (i % 3)) for i in range(100)]
        episode = [(1500 + i * 5, 60.0) for i in range(100)]
        for t, v in normal:
            a.queue(t, 10, "coding", "cpu", v, DAY0)
            b.queue(t, 10, "coding", "cpu", v, DAY0)
        for t, v in episode:
            a.queue(t, 10, "coding", "cpu", v, DAY0)
        a.exclude(1500, 1500 + 99 * 5)
        a.commit_all()
        b.commit_all()
        assert a.get("s10|coding", "cpu") == b.get("s10|coding", "cpu")

    def test_open_exclusion_blocks_new_updates_until_closed(self):
        store = BaselineStore(Config())
        eid = store.exclude(1000.0, None)
        store.queue(2000.0, 10, "x", "cpu", 99.0, DAY0)
        assert not store.pending
        store.close_exclusion(eid, 2500.0)
        store.queue(3000.0, 10, "x", "cpu", 1.0, DAY0)
        assert len(store.pending) == 1
        store.prune_exclusions(1e9)
        assert not store.exclusions

    def test_new_normal_after_n_days(self):
        store = BaselineStore(Config(new_normal_days=3))
        fill(store, 10, "coding", "cpu", 20.0, [DAY0, DAY0 + 1])
        assert not store.note_guarded("s10|coding", "cpu", DAY0 + 2)
        assert not store.note_guarded("s10|coding", "cpu", DAY0 + 2)  # same day doesn't count twice
        assert not store.note_guarded("s10|coding", "cpu", DAY0 + 3)
        assert store.note_guarded("s10|coding", "cpu", DAY0 + 4)
        assert store.get("s10|coding", "cpu").n == 0
        assert ("relearn", "s10|coding", "cpu") in store.events
        assert not store.note_guarded("nope", "cpu", DAY0)

    def test_manual_reset_keeps_global(self):
        store = BaselineStore(Config())
        fill(store, 10, "coding", "cpu", 20.0, [DAY0, DAY0 + 1])
        store.reset_chain(10, "coding", "cpu")
        assert store.get("s10|coding", "cpu").n == 0 and store.get("*|coding", "cpu").n == 0
        assert store.get("*|*", "cpu").n == 80

    def test_learning_progress(self):
        store = BaselineStore(Config())
        fill(store, 10, "coding", "cpu", 20.0, [DAY0, DAY0 + 1])
        usage = {(10, "coding"): 3600.0, (11, "browsing"): 3600.0, (3, "idle"): 99999.0}
        assert store.learning_progress(usage) == pytest.approx(0.5)  # idle time isn't counted
        assert store.learning_progress({}) == 0.0

    def test_persistence_rows(self):
        store = BaselineStore(Config())
        fill(store, 10, "coding", "cpu", 20.0, [DAY0, DAY0 + 1])
        rows = store.rows(only_dirty=True)
        assert len(rows) == 3 and not store.dirty
        other = BaselineStore(Config())
        other.load_rows(rows + [("bad", "cpu", "{not json")])
        assert other.get("s10|coding", "cpu") == store.get("s10|coding", "cpu")
        assert other.get("bad", "cpu") is None


# ----------------------------------------------------------- sustain logic
class TestSustainTracker:
    def durations(self):
        return {Level.PSST: 120.0, Level.HEADS_UP: 300.0, Level.UH_OH: 300.0}

    def test_ten_second_spike_never_alerts(self):
        t = SustainTracker(self.durations(), 0.8, 5)
        out = [t.push(i * 5.0, Level.UH_OH if i in (100, 101) else Level.NONE) for i in range(200)]
        assert max(out) == Level.NONE

    def test_six_minute_plateau_alerts_and_escalates(self):
        t = SustainTracker(self.durations(), 0.8, 5)
        out = [t.push(i * 5.0, Level.HEADS_UP) for i in range(73)]  # 6 minutes
        assert out[23] == Level.NONE           # < 2 minutes
        assert out[24] == Level.PSST           # 2 minutes
        assert out[60] == Level.HEADS_UP       # 5 minutes
        assert out[-1] == Level.HEADS_UP

    def test_plateau_with_dips_still_alerts(self):
        t = SustainTracker(self.durations(), 0.8, 5)
        out = [t.push(i * 5.0, Level.NONE if i % 5 == 4 else Level.HEADS_UP) for i in range(80)]
        assert out[-1] == Level.HEADS_UP       # one dip in five is tolerated

    def test_mostly_below_does_not_alert(self):
        t = SustainTracker(self.durations(), 0.8, 5)
        out = [t.push(i * 5.0, Level.HEADS_UP if i % 2 else Level.NONE) for i in range(80)]
        assert max(out) == Level.NONE

    def test_reset(self):
        t = SustainTracker(self.durations(), 0.8, 5)
        for i in range(30):
            t.push(i * 5.0, Level.PSST)
        t.reset()
        assert t.push(1000.0, Level.PSST) == Level.NONE


# -------------------------------------------------------------- detector
def trusted_store(cfg, metric="cpu", mean=10.0, sd=2.0, slot=10, act="browsing"):
    store = BaselineStore(cfg)
    rng = random.Random(3)
    t = 0.0
    for d in (DAY0, DAY0 + 1):
        for _ in range(200):
            store.queue(t, slot, act, metric, rng.gauss(mean, sd), d)
            t += 5
    store.commit_all()
    return store


class Runner:
    def __init__(self, cfg=None, store=None, metric="cpu", **kw):
        self.cfg = cfg or Config()
        self.store = store or trusted_store(self.cfg, metric, **kw)
        self.det = Detector(self.cfg, self.store)
        self.t = 1_000_000.0
        self.mono = 50_000.0

    def step(self, dt=5.0, learning=False, excluded=False, grace=False, **sys_kw):
        self.t += dt
        self.mono += dt
        s = make_sample(ts=self.t, mono=self.mono, sys=make_sys(**sys_kw))
        return self.det.process(s, 10, "browsing", DAY0 + 3, learning=learning, excluded=excluded, login_grace=grace)

    def run(self, seconds, **kw):
        return [self.step(**kw) for _ in range(int(seconds / 5))]


def levels(results, metric="cpu", kind="z"):
    return [max((s.level for s in r.signals if s.metric == metric and s.kind == kind), default=Level.NONE) for r in results]


class TestDetector:
    def test_plateau_is_not_absorbed_by_the_baseline(self):
        """Critique problem 1: the old pseudocode's z fell below 3 within ~25 s."""
        r = Runner()
        r.run(60, cpu_total=10.0)
        res = r.run(360, cpu_total=10.0 + 4 * 3.0 + 15)  # well above normal for 6 minutes
        assert res[-1].z["cpu"] > 3.0
        assert Level.HEADS_UP in levels(res)
        st, _, _ = r.store.lookup(10, "browsing", "cpu")
        assert st.mean < 11.0  # the anomaly never entered the baseline

    def test_pre_update_values_used_for_z(self):
        r = Runner()
        res = r.step(cpu_total=40.0)
        st, _, _ = r.store.lookup(10, "browsing", "cpu")
        assert res.z["cpu"] == pytest.approx((40.0 - st.mean) / max(st.std, 3.0))

    def test_ten_second_spike_no_alert(self):
        r = Runner()
        r.run(120, cpu_total=10.0)
        res = r.run(10, cpu_total=95.0) + r.run(300, cpu_total=10.0)
        assert max(levels(res)) == Level.NONE

    def test_small_rise_on_tiny_variance_is_ignored(self):
        """z is huge for 2% -> 9% idle CPU, but the rise is below the 15-point minimum."""
        r = Runner(mean=2.0, sd=0.2)
        res = r.run(400, cpu_total=9.0)
        assert res[-1].z["cpu"] > 2.0
        assert max(levels(res)) == Level.NONE

    def test_low_values_never_alert(self):
        r = Runner(mean=50.0, sd=5.0)
        res = r.run(400, cpu_total=1.0)
        assert max(levels(res)) == Level.NONE

    def test_learning_mode_only_absolute_limits(self):
        r = Runner()
        res = r.run(400, cpu_total=60.0, learning=True)
        assert max(levels(res)) == Level.NONE

    def test_absolute_limits_checked_when_trusted(self):
        """Critique problem 2: absolute limits ran only during cold start."""
        r = Runner()
        res = r.step(volumes=[Volume("C:\\", 500 * GB, 4 * GB)])
        sig = [s for s in res.signals if s.metric == "disk_free"]
        assert sig and sig[0].level == Level.UH_OH and sig[0].detail["rule"] == "disk_full"
        res = r.step(volumes=[Volume("C:\\", 160 * GB, 15 * GB)])
        sig = [s for s in res.signals if s.metric == "disk_free"]
        assert sig and sig[0].level == Level.HEADS_UP and sig[0].detail["rule"] == "disk_low"
        res = r.step(volumes=[Volume("C:\\", 2000 * GB, 150 * GB)])  # 7.5% free but 150 GB: fine
        assert not [s for s in res.signals if s.metric == "disk_free"]

    def test_removable_volumes_ignored(self):
        r = Runner()
        res = r.step(volumes=[Volume("E:\\", 32 * GB, 0.1 * GB, fixed=False)])
        assert not [s for s in res.signals if s.metric == "disk_free"]

    def test_temperature_limit_relative_to_tjmax(self):
        r = Runner(Config(tjmax_c=100.0))
        res = r.run(40, temp_c=96.0)
        assert any(s.detail.get("rule") == "temp_high" and s.level == Level.UH_OH for s in res[-1].signals)
        r2 = Runner(Config(tjmax_c=105.0))  # e.g. a chip designed to run hot
        res = r2.run(40, temp_c=96.0)
        assert not any(s.detail.get("rule") == "temp_high" for s in res[-1].signals)

    def test_throttle_absolute_rule(self):
        r = Runner()
        res = r.run(130, cpu_total=80.0, perf_limit_pct=60.0)
        assert any(s.detail.get("rule") == "throttle" for s in res[-1].signals)
        r = Runner()
        res = r.run(130, cpu_total=10.0, perf_limit_pct=60.0)  # idle and limited is normal power saving
        assert not any(s.detail.get("rule") == "throttle" for s in res[-1].signals)

    def test_learning_only_abs_rules(self):
        cfg = Config()
        r = Runner(cfg, store=BaselineStore(cfg))  # nothing trusted
        res = r.run(605, cpu_total=97.0, learning=True)
        assert any(s.detail.get("rule") == "cpu_pegged" for s in res[-1].signals)
        r = Runner(cfg)  # trusted baseline: the conservative limit doesn't apply
        res = r.run(605, cpu_total=97.0)
        assert not any(s.detail.get("rule") == "cpu_pegged" for s in res[-1].signals)

    def test_gap_resets_streaks_and_starts_grace(self):
        r = Runner()
        r.run(100, cpu_total=50.0)
        res = r.step(dt=3600, cpu_total=50.0)     # slept for an hour
        assert res.gap and res.grace
        res = r.run(170, cpu_total=50.0)
        assert max(levels(res)) == Level.NONE      # resume grace (3 min) keeps it quiet
        res = r.run(400, cpu_total=50.0)
        assert max(levels(res)) >= Level.PSST

    def test_clock_going_backwards_is_a_gap(self):
        r = Runner()
        r.step()
        r.t -= 3600  # manual clock change
        assert r.step().gap

    def test_excluded_periods_neither_alert_nor_learn(self):
        r = Runner()
        before = r.store.get("s10|browsing", "cpu").n
        res = r.run(400, cpu_total=90.0, excluded=True)
        r.store.commit_all()
        assert max(levels(res)) == Level.NONE and not r.store.pending
        assert r.store.get("s10|browsing", "cpu").n == before

    def test_missing_metric_is_skipped_not_zero(self):
        r = Runner()
        res = r.step(temp_c=None)
        assert res.z["temp"] is None and res.band["temp"] is None

    def test_sensitivity_change_applies_immediately(self):
        r = Runner()
        r.run(60, cpu_total=10.0)
        val = 10.0 + 2.2 * 3.0 + 15.0  # > 15 points above
        r.det.configure(Config(sensitivity=0))  # chill: psst at z 2.5
        chill = r.run(200, cpu_total=val)
        r.det.configure(Config(sensitivity=4))  # watchful: psst at z 1.5
        watchful = r.run(200, cpu_total=val)
        assert max(levels(watchful)) >= max(levels(chill))

    def test_uh_oh_needs_a_practical_floor(self):
        r = Runner(metric="ram", mean=5.0, sd=0.1)
        # +3 GB on a 16 GB PC: z is huge but only 50% of memory is used -> Psst only
        res = r.run(400, ram_total=16 * GB, ram_available=8 * GB)
        assert max(levels(res, "ram")) == Level.PSST
        r = Runner(metric="ram", mean=5.0, sd=0.1)
        res = r.run(400, ram_total=16 * GB, ram_available=int(1.5 * GB))
        assert max(levels(res, "ram")) == Level.UH_OH

    def test_floor_helpers(self):
        s = make_sample(sys=make_sys(ram_total=32 * GB, ram_available=30 * GB))
        assert level_floor_ok("cpu", Level.PSST, 1.0, s)
        assert not level_floor_ok("cpu", Level.HEADS_UP, 20.0, s) and level_floor_ok("cpu", Level.HEADS_UP, 31.0, s)
        assert not level_floor_ok("ram", Level.HEADS_UP, 2.0, s)
        assert level_floor_ok("unknown", Level.UH_OH, 0.0, s)
        assert min_std_for("ram", s) == pytest.approx(0.96)
        assert min_std_for("cpu", s) == 3.0
