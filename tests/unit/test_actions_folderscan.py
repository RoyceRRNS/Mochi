"""Safe one-click actions (ACT-1, ACT-2) and the biggest-folders scan (ACT-3)."""

import os
import stat as stat_mod
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from mochi.actions import ActionRunner, is_protected
from mochi.folderscan import CLOUD_ONLY, FILE_ATTRIBUTE_REPARSE_POINT, IO_REPARSE_TAG_MOUNT_POINT, FolderScanner, scan
from mochi.platform.fake import FakeClock, FakePlatform, FakeProc


@pytest.fixture
def runner():
    fake = FakePlatform(FakeClock())
    return fake, ActionRunner(fake, sleep=lambda s: None, grace_s=1.0)


class TestEndProcess:
    def test_graceful_close_first(self, runner):
        fake, r = runner
        fake.add(FakeProc("notes.exe", 10, create_time=100.0))
        fake.close_exits.add(10)
        res = r.end_process(10, 100.0, "notes.exe")
        assert res.ok and "closed itself nicely" in res.message
        assert ("post_close", 10) in fake.calls and ("terminate", 10) not in fake.calls

    def test_forced_after_grace(self, runner):
        fake, r = runner
        fake.add(FakeProc("stuck.exe", 11, create_time=100.0))
        res = r.end_process(11, 100.0, "stuck.exe")
        assert res.ok and res.message == "I ended stuck.exe."
        assert fake.calls[-1] == ("terminate", 11)

    def test_pid_reuse_is_refused(self, runner):
        fake, r = runner
        fake.add(FakeProc("new.exe", 12, create_time=500.0))
        res = r.end_process(12, 100.0, "old.exe")
        assert not res.ok and "already closed" in res.message and not fake.calls

    @pytest.mark.parametrize("name,pid", [("csrss.exe", 600), ("System", 4), ("explorer.exe", 700), ("svchost.exe", 800)])
    def test_protected_never_offered(self, runner, name, pid):
        fake, r = runner
        fake.add(FakeProc(name, pid, create_time=1.0))
        res = r.end_process(pid, 1.0, name)
        assert not res.ok and "part of Windows" in res.message and not fake.calls

    def test_never_ends_mochi(self, runner):
        fake, r = runner
        assert not r.end_process(fake.self_pid(), 0.0, "python.exe").ok
        assert is_protected("python.exe", 5, 5) and is_protected("python.exe", 6, 5, "mochi")
        assert not is_protected("chrome.exe", 6, 5)

    def test_access_denied_is_polite(self, runner):
        fake, r = runner
        fake.add(FakeProc("admin.exe", 13, create_time=1.0))
        fake.access_denied.add(13)
        res = r.end_process(13, 1.0, "admin.exe")
        assert not res.ok and "couldn't end admin.exe" in res.message

    def test_confirm_text_warns_about_unsaved_work(self, runner):
        _, r = runner
        t = r.confirm_text("Word", 42)
        assert "process 42" in t and "unsaved work" in t


class TestOtherActions:
    def test_lower_priority(self, runner):
        fake, r = runner
        fake.add(FakeProc("render.exe", 20, create_time=1.0))
        assert r.lower_priority(20, 1.0, "render.exe").ok and ("low_priority", 20) in fake.calls
        assert not r.lower_priority(21, 1.0, "gone.exe").ok
        assert not r.lower_priority(4, 1.0, "System").ok
        fake.access_denied.add(20)
        assert "couldn't slow" in r.lower_priority(20, 1.0, "render.exe").message

    def test_open_location_and_startup(self, runner):
        fake, r = runner
        assert not r.open_location(None).ok
        assert r.open_location(r"C:\x\y.exe").ok and ("open_location", r"C:\x\y.exe") in fake.calls
        assert r.open_startup_settings().ok


# ------------------------------------------------------------- folder scan
def make_tree(root):
    (root / "big").mkdir()
    (root / "big" / "a.bin").write_bytes(b"x" * 300_000)
    (root / "big" / "deep").mkdir()
    (root / "big" / "deep" / "b.bin").write_bytes(b"x" * 200_000)
    (root / "small").mkdir()
    (root / "small" / "c.txt").write_bytes(b"x" * 1000)
    (root / "loose.bin").write_bytes(b"x" * 5000)


class TestFolderScan:
    def test_sizes_aggregate_to_top_folders(self, tmp_path):
        make_tree(tmp_path)
        res = scan(str(tmp_path))
        sizes = dict(res.folders)
        assert sizes[str(tmp_path / "big")] == 500_000
        assert sizes[str(tmp_path / "big" / "deep")] == 200_000
        assert sizes[str(tmp_path / "small")] == 1000
        assert res.files == 4 and res.total_bytes == 506_000 and not res.partial
        assert res.folders[0][0] == str(tmp_path / "big")

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
    def test_junctions_are_not_followed(self, tmp_path):
        make_tree(tmp_path)
        link = tmp_path / "loop"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)], check=True, capture_output=True)
        res = scan(str(tmp_path))
        assert res.skipped_links >= 1 and res.total_bytes == 506_000

    def test_cloud_only_files_count_zero_and_are_never_opened(self, tmp_path):
        class Entry:
            def __init__(self, path, size, attrs, is_dir=False):
                self.path, self.name = path, os.path.basename(path)
                self._st = SimpleNamespace(st_mode=(stat_mod.S_IFDIR if is_dir else stat_mod.S_IFREG), st_size=size,
                                           st_file_attributes=attrs, st_reparse_tag=0)

            def stat(self, follow_symlinks=False):
                return self._st

            def is_symlink(self):
                return False

        class It(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        root = str(tmp_path)
        sub = os.path.join(root, "OneDrive")
        tree = {root: It([Entry(sub, 0, 0, True)]),
                sub: It([Entry(os.path.join(sub, "cloud.mp4"), 4_000_000_000, CLOUD_ONLY & 0x400000),
                         Entry(os.path.join(sub, "local.doc"), 1000, 0)])}
        res = scan(root, scandir=lambda p: tree[p])
        assert res.cloud_only == 1 and res.total_bytes == 1000

    def test_permission_errors_skipped(self, tmp_path):
        make_tree(tmp_path)
        real = os.scandir

        def guarded(p):
            if p.endswith("small"):
                raise PermissionError(p)
            return real(p)

        res = scan(str(tmp_path), scandir=guarded)
        assert res.denied == 1 and res.total_bytes == 505_000

    def test_reparse_link_tags_skipped(self, tmp_path):
        class Entry:
            path = str(tmp_path / "jn")
            name = "jn"

            def stat(self, follow_symlinks=False):
                return SimpleNamespace(st_mode=stat_mod.S_IFDIR, st_size=0,
                                       st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
                                       st_reparse_tag=IO_REPARSE_TAG_MOUNT_POINT)

            def is_symlink(self):
                return False

        class It(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        res = scan(str(tmp_path), scandir=lambda p: It([Entry()]))
        assert res.skipped_links == 1

    def test_cancel_and_time_budget_give_partial_results(self, tmp_path):
        make_tree(tmp_path)
        ev = threading.Event()
        ev.set()
        res = scan(str(tmp_path), cancel=ev)
        assert res.cancelled and res.partial
        ticks = iter(range(0, 1000, 100))
        res = scan(str(tmp_path), budget_s=50, clock=lambda: next(ticks))
        assert res.partial and not res.cancelled

    def test_background_scanner(self, tmp_path):
        make_tree(tmp_path)
        done = threading.Event()
        out = {}
        sc = FolderScanner()
        sc.start(str(tmp_path), lambda r: (out.setdefault("r", r), done.set()))
        assert done.wait(10)
        assert out["r"].total_bytes == 506_000 and not sc.running
        sc.cancel()
