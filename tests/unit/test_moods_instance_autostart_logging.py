"""Moods (MOOD-1..3), single instance (LIF-2), autostart (LIF-1) and logging (LIF-8)."""

import logging
import subprocess
import sys
import textwrap
import time

import pytest

from mochi.autostart import RUN_KEY, Autostart, MemoryRegistry, launch_command
from mochi.instance import AlreadyRunning, InstanceLock
from mochi.logging_setup import BACKUPS, MAX_BYTES, setup
from mochi.moods import LABELS, PRECEDENCE, SPEECH, Mood, MoodEngine, MoodInputs, conditions


# -------------------------------------------------------------------- moods
class TestMoods:
    def test_precedence_order(self):
        assert PRECEDENCE == [Mood.DIZZY, Mood.SWEATY, Mood.GRUMPY, Mood.PUFFY, Mood.ZOOMIES, Mood.PROUD, Mood.SLEEPY,
                              Mood.HAPPY]

    def test_every_mood_has_plain_language_label_and_speech(self):
        for m in Mood:
            name, desc = LABELS[m]
            assert name and desc[-1] in ".!" and SPEECH[m]

    @pytest.mark.parametrize("inputs,expected", [
        (MoodInputs(), Mood.HAPPY),
        (MoodInputs(ram_z=2.5), Mood.PUFFY),
        (MoodInputs(ram_used_pct=92), Mood.PUFFY),
        (MoodInputs(paging_rate=1500, ram_available_pct=8), Mood.DIZZY),
        (MoodInputs(memory_pressure_open=True), Mood.DIZZY),
        (MoodInputs(temp_c=92, tjmax_c=100), Mood.SWEATY),
        (MoodInputs(throttled=True), Mood.SWEATY),
        (MoodInputs(disk_nearly_full=True), Mood.GRUMPY),
        (MoodInputs(benchmark_running=True), Mood.ZOOMIES),
        (MoodInputs(proud=True), Mood.PROUD),
        (MoodInputs(user_idle=True, cpu=3), Mood.SLEEPY),
        (MoodInputs(user_idle=True, cpu=40), Mood.HAPPY),
        (MoodInputs(ram_z=3, disk_nearly_full=True, paging_rate=2000, ram_available_pct=5), Mood.DIZZY),
    ])
    def test_conditions(self, inputs, expected):
        engine = MoodEngine(window_s=15, dwell_s=0)
        mood = None
        for i in range(4):
            mood = engine.update(i * 5.0, conditions(inputs))
        assert mood == expected

    def test_reacts_within_30_seconds(self):
        e = MoodEngine(15, 30)
        for i in range(12):
            e.update(i * 5.0, conditions(MoodInputs()))
        t0 = 60.0
        for i in range(1, 7):
            if e.update(t0 + i * 5.0, conditions(MoodInputs(ram_z=3))) == Mood.PUFFY:
                break
        assert e.current == Mood.PUFFY and i * 5 <= 30

    def test_no_flicker_minimum_dwell(self):
        e = MoodEngine(15, 30)
        for i in range(12):
            e.update(i * 5.0, conditions(MoodInputs()))
        changes = 0
        last = e.current
        for i in range(12, 60):
            flip = MoodInputs(ram_z=3) if (i // 2) % 2 else MoodInputs()
            m = e.update(i * 5.0, conditions(flip))
            if m != last:
                changes += 1
                last = m
        assert changes <= (60 - 12) * 5 / 30 + 1

    def test_label_helper(self):
        assert MoodEngine.label(Mood.GRUMPY)[0] == "Grumpy"


# ---------------------------------------------------------------- instance
class TestInstanceLock:
    def test_second_lock_fails_then_succeeds_after_release(self, tmp_path):
        a = InstanceLock(tmp_path / "mochi.lock").acquire()
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path / "mochi.lock").acquire()
        a.release()
        assert not a.held
        with InstanceLock(tmp_path / "mochi.lock") as b:
            assert b.held
        a.release()  # releasing twice is harmless

    def test_lock_held_by_another_process(self, tmp_path):
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parents[2])!r})
            from mochi.instance import InstanceLock
            lock = InstanceLock(__import__('pathlib').Path({str(tmp_path / 'p.lock')!r})).acquire()
            print('locked', flush=True)
            time.sleep(30)
        """)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            assert p.stdout.readline().strip() == "locked"
            with pytest.raises(AlreadyRunning):
                InstanceLock(tmp_path / "p.lock").acquire()
        finally:
            p.kill()
            p.wait()
        # The OS frees the lock when the holder dies: no stale lock. The venv's python.exe is a
        # launcher whose child interpreter holds the lock and exits a moment after the kill.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                InstanceLock(tmp_path / "p.lock").acquire().release()
                break
            except AlreadyRunning:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)


# --------------------------------------------------------------- autostart
class TestAutostart:
    def test_enable_disable_with_fake_registry(self):
        reg = MemoryRegistry()
        a = Autostart(reg)
        assert not a.is_enabled()
        cmd = a.enable()
        assert a.is_enabled() and reg.get(RUN_KEY, "Mochi") == cmd and "--start-hidden" in cmd
        a.disable()
        assert not a.is_enabled()
        a.disable()  # idempotent

    def test_profile_name_and_command(self):
        reg = MemoryRegistry()
        a = Autostart(reg, profile="dev")
        a.apply(True)
        assert reg.get(RUN_KEY, "Mochi (dev)") and "--profile dev" in launch_command("dev")
        a.apply(False)
        assert not a.is_enabled()
        assert "-m mochi" in launch_command()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
    def test_real_registry_with_scratch_key(self):
        from mochi.autostart import WinRegistry
        import winreg

        key = r"Software\MochiTests\Run"
        a = Autostart(WinRegistry(), key=key)
        try:
            a.enable()
            assert a.is_enabled()
            a.disable()
            assert not a.is_enabled()
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\MochiTests")
            except OSError:
                pass


# ----------------------------------------------------------------- logging
def test_rotating_log_is_capped(tmp_path):
    h = setup(tmp_path / "logs")
    try:
        assert h.maxBytes == MAX_BYTES and h.backupCount == BACKUPS
        assert MAX_BYTES * (BACKUPS + 1) <= 10 * 1024 * 1024
        logging.getLogger("mochi.test").info("hello")
        h.flush()
        assert "hello" in (tmp_path / "logs" / "mochi.log").read_text(encoding="utf-8")
        setup(tmp_path / "logs")  # re-setup replaces, never duplicates
        assert sum(1 for x in logging.getLogger("mochi").handlers if x.__class__.__name__ == "RotatingFileHandler") == 1
    finally:
        for x in list(logging.getLogger("mochi").handlers):
            logging.getLogger("mochi").removeHandler(x)
            x.close()
