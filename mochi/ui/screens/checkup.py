"""Check-up: Health Score, speed-test results, the guided PC check and biggest folders."""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QProgressBar, QWidget

from ...benchmarks import TEST_LABELS, WEIGHTS
from ...checkup import CheckupReport
from ...model import GB
from ..widgets import Card, JellyBar, RingGauge, button, clear_layout, hbox, label, status_chip, vbox
from .common import Page


class CheckupPage(Page):
    key = "checkup"
    title = "Check-up"
    subtitle = "Is your PC slower than it used to be? Let's look together."

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        top = QHBoxLayout()
        top.setSpacing(18)
        score_card = Card(padding=18)
        self.score = RingGauge("Health Score", size=170)
        score_card.add(self.score)
        self.score_note = label("", muted=True, wrap=True)
        self.score_note.setAlignment(Qt.AlignCenter)
        score_card.add(self.score_note)
        score_card.lay.addStretch(1)
        top.addWidget(score_card, 3)

        run_card = Card(padding=20)
        run_card.add(label("Guided PC check", "H2"))
        run_card.add(label("About a minute. I'll check free space, disk health, startup programs, how the CPU copes with a "
                           "short load, background apps, growing memory and the battery, then run my speed test.",
                           wrap=True, muted=True))
        self.run_btn = button("Start check-up", "Primary", on_click=self.start)
        self.bench_btn = button("Speed test only", on_click=self.speed_only)
        self.cancel_btn = button("Cancel", "Ghost", on_click=self.cancel)
        self.cancel_btn.hide()
        self.rebase_btn = button("Re-baseline", "Ghost", "Start a new reference, e.g. after a hardware upgrade",
                                 self.rebaseline)
        run_card.add(hbox(self.run_btn, self.bench_btn, self.cancel_btn, None, self.rebase_btn))
        self.preview = QWidget()
        prev = QGridLayout(self.preview)
        prev.setContentsMargins(0, 6, 0, 0)
        prev.setHorizontalSpacing(24)
        prev.setVerticalSpacing(6)
        for i, text in enumerate(("Free disk space and biggest folders", "Disk health", "Programs that start with Windows",
                                  "CPU temperature and throttling", "Background apps while you're away",
                                  "Programs with growing memory", "Battery health (laptops)", "Speed test and Health Score")):
            prev.addWidget(label(f"\u2022  {text}", muted=True), i % 4, i // 4)
        run_card.add(self.preview)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.step = label("", muted=True)
        run_card.add(self.progress)
        run_card.add(self.step)
        run_card.lay.addStretch(1)
        top.addWidget(run_card, 7)
        self.body.addLayout(top)

        self.report_card = Card(padding=20)
        self.report_title = label("Report card", "H2")
        self.report_card.add(self.report_title)
        self.top3 = vbox(spacing=6)
        self.report_card.add(self.top3)
        self.items = vbox(spacing=10)
        self.report_card.add(self.items)
        self.report_card.hide()
        self.body.addWidget(self.report_card)

        mid = QHBoxLayout()
        mid.setSpacing(18)
        self.bench_card = Card(padding=20)
        self.bench_card.add(label("Speed test", "H2"))
        self.bench_grid = QGridLayout()
        self.bench_grid.setHorizontalSpacing(18)
        self.bench_card.add(self.bench_grid)
        self.bench_note = label("", muted=True, wrap=True)
        self.bench_card.add(self.bench_note)
        mid.addWidget(self.bench_card, 5)

        folders = Card(padding=20)
        folders.add(label("Biggest folders", "H2"))
        self.root_box = QComboBox()
        self.root_box.setAccessibleName("Folder to scan")
        home = os.path.expanduser("~")
        self.root_box.addItem(f"Your folder ({home})", home)
        latest = ctx.engine.latest
        for mount, *_ in (latest.volumes if latest else []):
            self.root_box.addItem(f"Whole drive {mount}", mount)
        self.scan_btn = button("Scan", "Primary", on_click=lambda: self.start_scan(None))
        self.scan_cancel = button("Stop", "Ghost", on_click=self._stop_scan)
        self.scan_cancel.hide()
        folders.add(hbox(self.root_box, self.scan_btn, self.scan_cancel))
        self.scan_status = label("I only read folder listings, so cloud-only OneDrive files aren't downloaded.",
                                 muted=True, wrap=True)
        folders.add(self.scan_status)
        self.folder_list = vbox(spacing=6)
        folders.add(self.folder_list)
        mid.addWidget(folders, 5)
        self.body.addLayout(mid)

        self.history_card = Card(padding=18)
        self.history_card.add(label("Past check-ups", "H3"))
        self.history = vbox(spacing=4)
        self.history_card.add(self.history)
        self.body.addWidget(self.history_card)
        self.body.addStretch(1)
        self._cancel = threading.Event()
        self.running = False
        ctx.bridge.checkup.connect(self.show_report)
        ctx.bridge.benchmark.connect(lambda _s: self.refresh_bench())
        ctx.bridge.progress.connect(self._on_progress)

    # --------------------------------------------------------------- refresh
    def on_show(self) -> None:
        self.refresh_bench()
        self.refresh_history()
        if self.ctx.engine.last_checkup and not self.report_card.isVisible():
            self.show_report(self.ctx.engine.last_checkup)

    def refresh_history(self) -> None:
        clear_layout(self.history)
        rows = self.ctx.engine.checkup_history()
        if not rows:
            self.history.addWidget(label("No check-ups yet.", muted=True))
        for ts, score in rows[:8]:
            s = f"{score:.0f}" if score is not None else "still learning"
            self.history.addWidget(label(f"{time.strftime('%a %d %b %Y, %H:%M', time.localtime(ts))} · Health Score {s}",
                                         muted=True))

    def refresh_bench(self) -> None:
        h = self.ctx.engine.health_summary()
        latest, ref, score = h["latest"], h["reference"], h["score"]
        if score is None:
            runs = h["runs"]
            self.score.set(0, "--", "still learning", caption="out of 100")
            self.score_note.setText(f"I need {max(0, 3 - runs)} more speed test{'s' if 3 - runs != 1 else ''} before I can "
                                    f"compare. Tests run daily for the first two weeks while you're away and plugged in."
                                    if runs < 3 else "Not enough matching runs yet (same power source and power mode).")
        else:
            state = "good" if score >= 90 else "warning" if score >= 80 else "critical"
            self.score.set(score, f"{score:.0f}", "", None, state,
                           caption="out of 100")
            self.score_note.setText("Compared with this PC's own early speed tests. 100 means as fast as it used to be.")
        clear_layout(self.bench_grid)
        if latest is None:
            self.bench_note.setText("No speed test yet. It takes about 10 seconds and compares CPU, memory and disk "
                                    "speed with this PC's own early runs.")
            return
        for c, head in enumerate(("Test", "Now", "Usual", "Speed")):
            self.bench_grid.addWidget(label(head, muted=True), 0, c)
        for r, t in enumerate(WEIGHTS, start=1):
            cur = getattr(latest, t)
            usual = ref.get(t) if ref else None
            speed = f"{usual / cur * 100:.0f}%" if cur and usual else "--"
            self.bench_grid.addWidget(label(TEST_LABELS[t]), r, 0)
            self.bench_grid.addWidget(label(f"{cur:.3f} s" if cur else "skipped"), r, 1)
            self.bench_grid.addWidget(label(f"{usual:.3f} s" if usual else "--", muted=True), r, 2)
            self.bench_grid.addWidget(label(speed), r, 3)
        when = time.strftime("%a %d %b, %H:%M", time.localtime(latest.ts))
        mode = f" · disk test {latest.disk_mode}" if latest.disk_mode else ""
        note = f" · {latest.note}" if latest.note else ""
        self.bench_note.setText(f"Last run {when} · {'on mains power' if latest.on_ac else 'on battery'}"
                                f" · power mode {latest.power_mode or 'unknown'}{mode}{note}")

    # ----------------------------------------------------------------- runs
    def _busy(self, on: bool, text: str = "") -> None:
        self.running = on
        self.preview.setVisible(not on)
        self.run_btn.setEnabled(not on)
        self.bench_btn.setEnabled(not on)
        self.cancel_btn.setVisible(on)
        self.progress.setVisible(on)
        self.step.setText(text)

    def _on_progress(self, step: str, frac: float) -> None:
        self.progress.setValue(int(frac * 100))
        self.step.setText(step + "...")

    def start(self) -> None:
        if self.running:
            return
        self._cancel.clear()
        self._busy(True, "Starting...")
        bridge = self.ctx.bridge

        def work():
            return self.ctx.engine.run_checkup(progress=lambda s, f: bridge.progress.emit(s, f), cancel=self._cancel)

        def done(res):
            self._busy(False)
            if isinstance(res, BaseException):
                self.ctx.toast(f"The check-up stopped: {res}")
            self.refresh_bench()
            self.refresh_history()

        bridge.run_async(work, done, done)

    def speed_only(self) -> None:
        if self.running:
            return
        self._busy(True, "Running the speed test (about 10 s)...")
        self.progress.setRange(0, 0)

        def done(res):
            self.progress.setRange(0, 100)
            self._busy(False)
            if isinstance(res, BaseException):
                self.ctx.toast(f"The speed test didn't finish: {res}")
            elif not res.valid:
                self.ctx.toast(f"The speed test didn't count: {res.note}")
            else:
                self.ctx.toast("Speed test done." + (f" Health Score {res.score:.0f}." if res.score is not None else ""))
            self.refresh_bench()

        self.ctx.bridge.run_async(lambda: self.ctx.engine.run_benchmark(scheduled=False), done, done)

    def cancel(self) -> None:
        self._cancel.set()
        self.step.setText("Stopping after this step...")

    def rebaseline(self) -> None:
        if self.ctx.confirm("Start a new reference?", "Future speed tests will be compared with new runs instead of the "
                            "old ones. Use this after a hardware upgrade.", "Re-baseline"):
            self.ctx.engine.set_rebaseline()
            self.refresh_bench()
            self.ctx.toast("Okay! I'll build a new reference from the next few speed tests.")

    def show_report(self, report: CheckupReport) -> None:
        self.report_card.show()
        clear_layout(self.top3)
        clear_layout(self.items)
        when = time.strftime("%H:%M", time.localtime(report.ts))
        self.report_title.setText(f"Report card · {when} · took {report.seconds:.0f} s")
        if report.top3:
            self.top3.addWidget(label("My top suggestions", "H3"))
            for i, s in enumerate(report.top3, 1):
                self.top3.addWidget(label(f"{i}. {s}", wrap=True))
        else:
            self.top3.addWidget(label("Everything looks good. Nothing to do!", "H3"))
        for item in report.items:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 4, 0, 4)
            chip = status_chip(item.status)
            chip.setFixedWidth(118)
            lay.addWidget(chip, 0, Qt.AlignTop)
            texts = vbox(label(item.title, "H3"), label(item.detail, wrap=True, muted=True, sel=True), spacing=2)
            if item.suggestion:
                texts.addWidget(label(item.suggestion, wrap=True))
            lay.addLayout(texts, 1)
            self.items.addWidget(row)
        self.refresh_history()

    # ------------------------------------------------------------- folders
    def start_scan(self, mount: str | None) -> None:
        if mount:
            idx = self.root_box.findData(mount)
            if idx >= 0:
                self.root_box.setCurrentIndex(idx)
        root = self.root_box.currentData()
        self.scan_btn.setEnabled(False)
        self.scan_cancel.show()
        self.scan_status.setText(f"Scanning {root}... (up to a minute)")
        clear_layout(self.folder_list)
        self.ctx.engine.scan_biggest(root, lambda res: self.ctx.bridge._task_done.emit(self._scan_done, res, None))

    def _stop_scan(self) -> None:
        self.ctx.engine.scanner.cancel()

    def _scan_done(self, res) -> None:
        self.scan_btn.setEnabled(True)
        self.scan_cancel.hide()
        extra = []
        if res.partial:
            extra.append("stopped early, so these are partial results" if res.cancelled else "time budget reached; partial results")
        if res.cloud_only:
            extra.append(f"{res.cloud_only} cloud-only files counted as 0 (not on this disk)")
        if res.denied:
            extra.append(f"{res.denied} folders I wasn't allowed to read")
        if res.skipped_links:
            extra.append(f"{res.skipped_links} links skipped")
        self.scan_status.setText(f"{res.files:,} files, {res.total_bytes / GB:.1f} GB in {res.seconds:.0f} s"
                                 + (" · " + "; ".join(extra) if extra else ""))
        biggest = res.folders[0][1] if res.folders else 1
        for path, size in res.folders[:10]:
            bar = JellyBar(height=12)
            bar.set_segments([(size / biggest, "info", "info_strong")], f"{path}: {size / GB:.1f} GB")
            row = QWidget()
            row.setLayout(vbox(hbox(label(path, wrap=True, sel=True), None, label(f"{size / GB:.1f} GB")), bar, spacing=2))
            self.folder_list.addWidget(row)
