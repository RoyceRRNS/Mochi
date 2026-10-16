"""Find the biggest folders without hurting the PC (ACT-3).

- Runs on a worker thread and can be cancelled.
- Never follows junctions or symlinks (Windows has junction loops like "Application Data").
- Never opens files: it only reads directory entries, so OneDrive cloud-only files are
  not downloaded. Their size counts as 0 because they don't use local disk.
- Permission errors are skipped and counted.
- Stops at a time budget and returns partial results.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C
LINK_TAGS = {IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK}


@dataclass
class ScanResult:
    root: str
    folders: list[tuple[str, int]]          # (path, bytes), biggest first
    total_bytes: int = 0
    files: int = 0
    skipped_links: int = 0
    denied: int = 0
    cloud_only: int = 0
    partial: bool = False
    cancelled: bool = False
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def _attrs(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0) or 0


def scan(root: str, depth: int = 2, top: int = 15, budget_s: float = 60.0,
         cancel: threading.Event | None = None, progress: Callable[[int, str], None] | None = None,
         scandir: Callable = os.scandir, clock: Callable[[], float] = time.monotonic) -> ScanResult:
    start = clock()
    res = ScanResult(root, [])
    sizes: dict[str, int] = {}
    root = os.path.abspath(root)
    root_depth = root.rstrip("\\/").count(os.sep)
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            res.cancelled = res.partial = True
            break
        if clock() - start > budget_s:
            res.partial = True
            break
        folder = stack.pop()
        try:
            it = scandir(folder)
        except PermissionError:
            res.denied += 1
            continue
        except OSError as exc:
            res.denied += 1
            if len(res.errors) < 20:
                res.errors.append(f"{folder}: {exc.__class__.__name__}")
            continue
        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except (PermissionError, OSError):
                    res.denied += 1
                    continue
                attrs = _attrs(st)
                tag = getattr(st, "st_reparse_tag", 0) or 0
                # Junctions and symlinks are skipped; cloud (OneDrive) reparse folders are walked.
                is_link = entry.is_symlink() or bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT and tag in LINK_TAGS)
                if stat.S_ISDIR(st.st_mode):
                    if is_link or (hasattr(os.path, "isjunction") and os.path.isjunction(entry.path)):
                        res.skipped_links += 1
                        continue
                    stack.append(entry.path)
                    continue
                if entry.is_symlink():
                    res.skipped_links += 1
                    continue
                size = 0 if attrs & CLOUD_ONLY else st.st_size
                if attrs & CLOUD_ONLY:
                    res.cloud_only += 1
                res.files += 1
                res.total_bytes += size
                # Credit the size to each ancestor folder up to `depth` levels below root.
                parent = os.path.dirname(entry.path)
                rel_depth = parent.rstrip("\\/").count(os.sep) - root_depth
                while rel_depth > depth:
                    parent = os.path.dirname(parent)
                    rel_depth -= 1
                while rel_depth >= 1:
                    sizes[parent] = sizes.get(parent, 0) + size
                    parent = os.path.dirname(parent)
                    rel_depth -= 1
        if progress is not None and res.files and res.files % 2000 < 50:
            progress(res.files, folder)
    res.folders = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:top]
    res.seconds = clock() - start
    return res


class FolderScanner:
    """Background wrapper with cancel and a done callback."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.result: ScanResult | None = None

    def start(self, root: str, done: Callable[[ScanResult], None], budget_s: float = 60.0,
              progress: Callable[[int, str], None] | None = None) -> None:
        self.cancel_event.clear()

        def run():
            self.result = scan(root, budget_s=budget_s, cancel=self.cancel_event, progress=progress)
            done(self.result)

        self.thread = threading.Thread(target=run, name="mochi-folderscan", daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()
