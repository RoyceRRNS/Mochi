"""Settings: learning progress, sensitivity, quiet rules, app activities, your data."""

from __future__ import annotations

import os
import subprocess
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QProgressBar, QSlider,
                               QTableWidget,
                               QTableWidgetItem, QWidget)

from ... import __version__
from ...config import SENSITIVITY_STEPS
from ...model import ACTIVITIES
from ..widgets import Banner, Card, button, clear_layout, hbox, label, vbox
from .common import Page


class SettingsPage(Page):
    key = "settings"
    title = "Settings"
    subtitle = "Make Mochi chill or watchful, and manage your data"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        eng = ctx.engine
        cfg = eng.base_cfg

        # --- learning
        learn = Card(padding=20)
        learn.add(label("Learning", "H2"))
        self.learn_bar = QProgressBar()
        self.learn_bar.setRange(0, 100)
        self.learn_bar.setAccessibleName("Learning progress")
        self.learn_text = label("", muted=True, wrap=True)
        learn.add(self.learn_bar)
        learn.add(self.learn_text)
        self.body.addWidget(learn)

        # --- alerts
        alerts = Card(padding=20)
        alerts.add(label("Alerts", "H2"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, len(SENSITIVITY_STEPS) - 1)
        self.slider.setValue(cfg.sensitivity)
        self.slider.setPageStep(1)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setAccessibleName("Sensitivity")
        self.slider.valueChanged.connect(self._sensitivity)
        self.sens_text = label("", muted=True, wrap=True)
        alerts.add(hbox(label("Chill"), self.slider, label("Watchful"), spacing=12))
        alerts.add(self.sens_text)
        self.snooze_text = label("", muted=True)
        alerts.add(hbox(label("Snooze:", "H3"), button("1 hour", on_click=lambda: self._snooze(3600)),
                        button("Until tomorrow", on_click=lambda: self._snooze(None)),
                        button("Resume", "Ghost", on_click=lambda: self._snooze(0)), self.snooze_text, None))
        self.demo = QCheckBox("Demo mode: much shorter waits before alerts (for presentations)")
        self.demo.setChecked(cfg.demo_mode)
        self.demo.toggled.connect(lambda on: self._set(demo_mode=on))
        alerts.add(self.demo)
        self.body.addWidget(alerts)

        # --- behaviour + appearance
        row = QHBoxLayout()
        row.setSpacing(18)
        beh = Card(padding=20)
        beh.add(label("Behaviour", "H2"))
        self.autostart = QCheckBox("Start Mochi when I sign in")
        self.autostart.setChecked(eng.autostart.is_enabled() if hasattr(eng.autostart, "is_enabled") else cfg.autostart)
        self.autostart.toggled.connect(lambda on: self._set(autostart=on))
        self.start_hidden = QCheckBox("Start quietly in the tray")
        self.start_hidden.setChecked(cfg.start_hidden)
        self.start_hidden.toggled.connect(lambda on: self._set(start_hidden=on))
        self.roasts = QCheckBox("Gentle roasts (off by default)")
        self.roasts.setChecked(cfg.roasts)
        self.roasts.toggled.connect(lambda on: self._set(roasts=on))
        for w in (self.autostart, self.start_hidden, self.roasts):
            beh.add(w)
        row.addWidget(beh, 1)
        look = Card(padding=20)
        look.add(label("Appearance", "H2"))
        self.theme = QComboBox()
        self.theme.setAccessibleName("Theme")
        for text, val in (("Match Windows", "system"), ("Light", "light"), ("Midnight Mochi (dark)", "dark")):
            self.theme.addItem(text, val)
        self.theme.setCurrentIndex(max(0, self.theme.findData(cfg.theme)))
        self.theme.currentIndexChanged.connect(lambda _i: self._set(theme=self.theme.currentData()))
        self.motion = QComboBox()
        self.motion.setAccessibleName("Reduce motion")
        for text, val in (("Match Windows", "system"), ("Always reduce motion", "on"), ("Always animate", "off")):
            self.motion.addItem(text, val)
        self.motion.setCurrentIndex(max(0, self.motion.findData(cfg.reduce_motion)))
        self.motion.currentIndexChanged.connect(lambda _i: self._set(reduce_motion=self.motion.currentData()))
        look.add(hbox(label("Theme"), None, self.theme))
        look.add(hbox(label("Motion"), None, self.motion))
        row.addWidget(look, 1)
        self.body.addLayout(row)

        # --- quiet rules
        quiet = Card(padding=20)
        quiet.add(label("Things I keep quiet about", "H2"))
        quiet.add(label("Created when you press \"I meant to do that\". They expire after 30 days, and Uh-oh alerts "
                        "still get through.", muted=True, wrap=True))
        self.quiet_list = vbox(spacing=6)
        quiet.add(self.quiet_list)
        self.body.addWidget(quiet)

        # --- activities
        acts = Card(padding=20)
        acts.add(label("App activities", "H2"))
        acts.add(label("Which apps count as browsing, coding, gaming, media or office. Changes apply from now on; "
                       "past readings keep the activity they had.", muted=True, wrap=True))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["App (exe name)", "Activity", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(2, 110)
        self.table.setMinimumHeight(260)
        self.table.setAccessibleName("App activity mapping")
        acts.add(self.table)
        self.new_exe = QLineEdit()
        self.new_exe.setPlaceholderText("e.g. blender.exe")
        self.new_exe.setAccessibleName("New app exe name")
        self.new_cls = QComboBox()
        for a in ACTIVITIES:
            if a != "idle":
                self.new_cls.addItem(a, a)
        acts.add(hbox(self.new_exe, self.new_cls, button("Add", "Primary", on_click=self._add_mapping), None))
        self.body.addWidget(acts)

        # --- data
        data = Card(padding=20)
        data.add(label("Your data", "H2"))
        data.add(label("Everything stays on this PC. Mochi never connects to the internet. It stores app names (never "
                       "window titles, web addresses or command lines), readings and alerts.", muted=True, wrap=True))
        self.data_text = label("", muted=True, wrap=True, sel=True)
        data.add(self.data_text)
        data.add(hbox(button("Open folder", "", "Open the data folder", self._open_folder, "folder"),
                      button("Export…", "", "Save everything as CSV files", self._export, "export"),
                      button("Delete all my data…", "Danger", on_click=self._delete), None))
        self.body.addWidget(data)

        self.warn_box = vbox(spacing=8)
        self.body.addLayout(self.warn_box)
        about = label(f"Mochi {__version__} · profile '{eng.profile}' · admin features: "
                      f"{'available' if eng.platform.is_admin() else 'temperature and SMART need admin'}", muted=True)
        self.body.addWidget(about)
        self.body.addStretch(1)
        self._sensitivity_label()

    # ---------------------------------------------------------------- helpers
    def _set(self, **changes) -> None:
        warnings = self.ctx.engine.update_config(**changes)
        for w in warnings:
            self.ctx.toast(w)
        if self.ctx.window is not None:
            self.ctx.window.config_changed(changes)

    def _sensitivity(self, v: int) -> None:
        self._set(sensitivity=v)
        self._sensitivity_label()

    def _sensitivity_label(self) -> None:
        name, (p, h, u) = SENSITIVITY_STEPS[self.slider.value()]
        self.sens_text.setText(f"{name}: a quiet badge when a reading stays {p} standard deviations above your normal for "
                               f"2 minutes, a notification at {h}, and Uh-oh at {u}.")

    def _snooze(self, secs) -> None:
        until = self.ctx.engine.snooze(secs)
        self.on_show()
        self.ctx.toast("Alerts resumed." if not until else f"Snoozed until {time.strftime('%H:%M', time.localtime(until))}.")

    def _add_mapping(self) -> None:
        exe = self.new_exe.text().strip()
        if not exe or " " in exe.strip():
            self.ctx.toast("Type an app's exe name, like blender.exe.")
            return
        if not exe.lower().endswith(".exe"):
            exe += ".exe"
        self.ctx.engine.set_mapping(exe, self.new_cls.currentData())
        self.new_exe.clear()
        self._fill_table()
        self.ctx.toast(f"{exe} now counts as {self.new_cls.currentData()}.")

    def _fill_table(self) -> None:
        mapping = sorted(self.ctx.engine.mapper.mapping.items())
        self.table.setRowCount(len(mapping))
        for r, (exe, cls) in enumerate(mapping):
            item = QTableWidgetItem(exe)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, item)
            combo = QComboBox()
            combo.setAccessibleName(f"Activity for {exe}")
            for a in ACTIVITIES:
                if a != "idle":
                    combo.addItem(a, a)
            combo.setCurrentIndex(combo.findData(cls))
            combo.currentIndexChanged.connect(lambda _i, e=exe, c=combo: self.ctx.engine.set_mapping(e, c.currentData()))
            self.table.setCellWidget(r, 1, combo)
            rm = button("Remove", "Ghost", on_click=lambda _=False, e=exe: (self.ctx.engine.set_mapping(e, None),
                                                                            self._fill_table()))
            self.table.setCellWidget(r, 2, rm)
            self.table.setRowHeight(r, 44)

    def _fill_quiet(self) -> None:
        clear_layout(self.quiet_list)
        sups = self.ctx.engine.suppressions()
        if not sups:
            self.quiet_list.addWidget(label("Nothing yet.", muted=True))
        for s in sups:
            days = max(0, int((s.expires_ts - self.ctx.engine.clock.time()) // 86400))
            self.quiet_list.addLayout(hbox(label(s.label or f"{s.app} · {s.metric} · {s.activity}"),
                                           label(f"{days} days left", muted=True), None,
                                           button("Undo", on_click=lambda _=False, sid=s.id: self._undo(sid))))

    def _undo(self, sid: int) -> None:
        self.ctx.engine.remove_suppression(sid)
        self._fill_quiet()
        self.ctx.toast("Okay, I'll speak up about that again.")

    def _open_folder(self) -> None:
        path = str(self.ctx.engine.paths.root)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])

    def _export(self) -> None:
        def done(res):
            if isinstance(res, BaseException):
                self.ctx.toast(f"Export failed: {res}")
            else:
                self.ctx.toast(f"Exported to {res}")
        self.ctx.bridge.run_async(self.ctx.engine.export_data, done, done)

    def _delete(self) -> None:
        if self.ctx.confirm("Delete all your Mochi data?", "This removes every reading, baseline, alert and record. "
                            "Mochi will start learning from scratch. This can't be undone.", "Delete everything"):
            self.ctx.engine.delete_all_data()
            self.on_show()
            self.ctx.toast("All data deleted. I'll start learning again.")

    def on_show(self) -> None:
        eng = self.ctx.engine
        v = eng.latest
        prog = int(round((v.progress if v else 0) * 100))
        self.learn_bar.setValue(prog)
        if v and v.learning:
            self.learn_text.setText(f"Learning mode, day {min(v.learning_day, 3)} of 3: I only use safe absolute limits "
                                    f"until I know your normal. {prog}% of your usual time is in contexts I know well.")
        else:
            self.learn_text.setText(f"{prog}% of your usual time is in contexts (hour of the week + activity) I know well. "
                                    "Contexts I don't know yet borrow from broader ones.")
        su = eng.quiet.snoozed_until
        self.snooze_text.setText(f"snoozed until {time.strftime('%a %H:%M', time.localtime(su))}" if su > eng.clock.time()
                                 else "alerts are on")
        st = eng.status()
        self.data_text.setText(f"Folder: {eng.paths.root}\nDatabase: {st['db_mb']:.1f} MB · readings kept "
                               f"{eng.cfg.raw_days:.0f} days, minute averages {eng.cfg.minute_days:.0f} days, hourly forever")
        self._fill_quiet()
        self._fill_table()
        clear_layout(self.warn_box)
        for w in st["config_warnings"]:
            self.warn_box.addWidget(Banner(w, "warning"))
