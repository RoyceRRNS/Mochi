"""Config (LIF-9, DET-11, DEMO-2), paths (STO-1, STO-9) and model helpers."""

import json

import pytest

from mochi import config as config_mod
from mochi.config import DEMO_OVERRIDES, SENSITIVITY_STEPS, Config
from mochi.model import GB, MB, Level, Sample, Volume, derive_metrics, fmt_bytes, fmt_duration
from mochi.paths import Paths, default_base

from ..conftest import make_proc, make_sys


# ------------------------------------------------------------------ config
class TestConfig:
    def test_defaults_match_acceptance_criteria(self):
        c = Config()
        assert c.sample_interval_s == 5.0
        assert c.min_samples == 30 and c.min_days == 2
        assert c.cold_start_days == 3.0
        assert c.psst_s == 120 and c.heads_up_s == 300
        assert c.close_after_s == 120 and c.cooldown_s == 1800
        assert c.daily_popup_cap == 5 and c.suppression_days == 30
        assert c.roasts is False and c.demo_mode is False

    def test_sensitivity_maps_to_documented_thresholds(self):
        assert Config(sensitivity=0).z_thresholds() == {Level.PSST: 2.5, Level.HEADS_UP: 3.5, Level.UH_OH: 4.5}
        assert Config(sensitivity=2).z_thresholds() == {Level.PSST: 2.0, Level.HEADS_UP: 3.0, Level.UH_OH: 4.0}
        assert Config(sensitivity=4).z_thresholds() == {Level.PSST: 1.5, Level.HEADS_UP: 2.5, Level.UH_OH: 3.5}
        assert [n for n, _ in SENSITIVITY_STEPS] == ["Chill", "Relaxed", "Balanced", "Alert", "Watchful"]

    def test_demo_profile_shortens_waits_only_when_on(self):
        c = Config()
        assert c.effective() is c
        d = Config(demo_mode=True).effective()
        for k, v in DEMO_OVERRIDES.items():
            assert getattr(d, k) == v
        assert d.heads_up_s < Config().heads_up_s

    def test_load_valid_file(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"sensitivity": 4, "alpha": 0.02, "roasts": True, "theme": "dark"}))
        res = config_mod.load(p)
        assert res.warnings == []
        assert res.config.sensitivity == 4 and res.config.alpha == 0.02 and res.config.roasts and res.config.theme == "dark"

    def test_missing_file_gives_defaults(self, tmp_path):
        res = config_mod.load(tmp_path / "nope.json")
        assert res.config == Config() and res.warnings == []

    @pytest.mark.parametrize("data,field", [
        ({"sensitivity": 9}, "sensitivity"),
        ({"alpha": "fast"}, "alpha"),
        ({"roasts": "yes"}, "roasts"),
        ({"theme": "neon"}, "theme"),
        ({"min_samples": 3.5}, "min_samples"),
        ({"leak_ignore": "chrome"}, "leak_ignore"),
        ({"heads_up_s": -5}, "heads_up_s"),
    ])
    def test_bad_values_fall_back_with_warning(self, data, field):
        res = config_mod.from_dict(data)
        assert getattr(res.config, field) == getattr(Config(), field)
        assert len(res.warnings) == 1 and field in res.warnings[0]

    def test_unknown_key_and_non_object(self):
        res = config_mod.from_dict({"bogus": 1})
        assert "bogus" in res.warnings[0]
        res = config_mod.from_dict([1, 2, 3])
        assert res.config == Config() and res.warnings

    def test_corrupt_json_never_crashes(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{not json")
        res = config_mod.load(p)
        assert res.config == Config() and "defaults" in res.warnings[0]

    def test_int_accepted_for_float_and_lists_lowercased(self):
        res = config_mod.from_dict({"alpha": 0, "heads_up_s": 60, "leak_ignore": ["Chrome.EXE"]})
        assert res.config.heads_up_s == 60.0 and isinstance(res.config.heads_up_s, float)
        assert res.config.leak_ignore == ["chrome.exe"]
        assert any("alpha" in w for w in res.warnings)  # 0 is outside the allowed range

    def test_save_roundtrip_atomic(self, tmp_path):
        p = tmp_path / "sub" / "c.json"
        c = Config(sensitivity=1, demo_mode=True)
        config_mod.save(c, p)
        assert not p.with_suffix(".tmp").exists()
        assert config_mod.load(p).config == c


# ------------------------------------------------------------------- paths
class TestPaths:
    def test_profile_layout(self, tmp_path):
        p = Paths.for_profile("dev", tmp_path).ensure()
        assert p.root == tmp_path / "dev"
        for d in (p.logs, p.exports, p.tmp):
            assert d.is_dir()
        assert p.db.name == "mochi.db" and p.config.name == "config.json"

    def test_profiles_are_separate(self, tmp_path):
        assert Paths.for_profile("live", tmp_path).db != Paths.for_profile("dev", tmp_path).db

    @pytest.mark.parametrize("bad", ["../evil", "a b", "x" * 33, "c:\\temp"])
    def test_invalid_profile_rejected(self, bad, tmp_path):
        with pytest.raises(ValueError):
            Paths.for_profile(bad, tmp_path)

    def test_env_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MOCHI_HOME", str(tmp_path / "h"))
        monkeypatch.setenv("MOCHI_PROFILE", "envprof")
        assert default_base() == tmp_path / "h"
        assert Paths.for_profile().root == tmp_path / "h" / "envprof"

    def test_default_base_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MOCHI_HOME", raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_base() == tmp_path / "Mochi"


# ------------------------------------------------------------------- model
class TestModel:
    def test_derive_metrics(self):
        s = make_sys(ram_total=16 * GB, ram_available=6 * GB, cpu_total=12.0)
        m = derive_metrics(s)
        assert m["cpu"] == 12.0 and m["ram"] == pytest.approx(10.0)
        assert m["discharge"] is None  # no battery

    def test_discharge_only_on_battery(self):
        from mochi.model import Battery

        assert derive_metrics(make_sys(battery=Battery(50, False, 12.0)))["discharge"] == 12.0
        assert derive_metrics(make_sys(battery=Battery(50, True, 0.0)))["discharge"] is None

    def test_volume_maths(self):
        v = Volume("C:\\", 100 * GB, 8 * GB)
        assert v.free_pct == pytest.approx(8.0) and v.free_gb == pytest.approx(8.0)
        assert Volume("X:", 0, 0).free_pct == 0.0

    def test_top_procs_union_without_duplicates(self):
        procs = [make_proc(i, f"p{i}.exe", cpu_core=i, private_mb=100 - i) for i in range(1, 41)]
        s = Sample(0, 0, make_sys(), procs, {})
        top = s.top_procs(5)
        idents = {p.pid for p in top}
        assert {36, 37, 38, 39, 40} <= idents and {1, 2, 3, 4, 5} <= idents and len(top) == 10

    def test_formatting(self):
        assert fmt_bytes(None) == "unknown"
        assert fmt_bytes(5.8 * GB) == "5.8 GB"
        assert fmt_bytes(300 * MB) == "300 MB"
        assert fmt_bytes(2048) == "2 KB"
        assert fmt_duration(30) == "30 s" and fmt_duration(720) == "12 min"
        assert fmt_duration(3 * 3600) == "3 h" and fmt_duration(3 * 3600 + 600) == "3 h 10 min"
        assert fmt_duration(5 * 86400) == "5 days" and fmt_duration(-4) == "0 s"

    def test_level_titles(self):
        assert Level.PSST.title == "Psst..." and Level.HEADS_UP.title == "Heads up!" and Level.UH_OH.title == "Uh-oh."
        assert Level.NONE.title == ""
