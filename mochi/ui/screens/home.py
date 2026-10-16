"""Home: big Mochi, the weather line, three soft gauges, and the inbox."""

from __future__ import annotations

import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QMenu, QProgressBar, QSizePolicy, QVBoxLayout,
                               QWidget)

from ...alerts import Episode
from ...engine import Notice, SampleView
from ...model import Level
from ...moods import LABELS, Mood
from ..mascot import MochiWidget
from ..theme import THEME
from ..widgets import (Banner, Card, Chip, RiceCakes, RingGauge, SpeechBubble, button, clear_layout, hbox, label,
                       level_chip, sky_pixmap, vbox)
from .common import Page, time_ago

RESPONSE_TEXT = {
    "intentional": "You said this was on purpose. I'll stay quiet about it.",
    "not_a_problem": "You said this wasn't a problem. Noted.",
    "new_normal": "Learning this as your new normal.",
    "action": "You took action. Thanks!",
    "dismissed": "Dismissed.",
}
MOOD_FILL = {Mood.HAPPY: "good", Mood.SLEEPY: "info", Mood.PUFFY: "warning", Mood.SWEATY: "warning",
             Mood.DIZZY: "critical", Mood.GRUMPY: "critical", Mood.ZOOMIES: "info", Mood.PROUD: "accent"}


class AlertRow(QWidget):
    def __init__(self, page: "HomePage", ep: Episode) -> None:
        super().__init__()
        self.page = page
        self.ep = ep
        ctx = page.ctx
        d = ep.diagnosis
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(level_chip(int(ep.max_level)))
        top.addWidget(label(d.title, "H3"))
        state = "ongoing" if ep.is_open else "resolved"
        top.addWidget(label(f"{time_ago(ep.opened_ts, ctx.engine.clock.time())} · {state}", muted=True))
        top.addStretch(1)
        if ep.suppressed:
            top.addWidget(Chip("kept quiet", "surface_alt"))
        lay.addLayout(top)
        cause = label(d.cause, wrap=True)
        cause.setStyleSheet("font-size: 14px; font-weight: 600;")
        lay.addWidget(cause)
        lay.addWidget(label(d.tip, wrap=True, muted=True))
        if ctx.nerd():
            z = f"z = {ep.z:.1f}" if ep.z is not None else "rule-based"
            lay.addWidget(label(f"Nerd stats: {z} · metrics {', '.join(sorted(ep.metrics))} · bucket "
                                f"{ep.bucket_level or 'n/a'} · activity {ep.activity} · rule {d.rule}", muted=True,
                                wrap=True, sel=True))
        if ep.response and ep.response != "dismissed":
            lay.addWidget(label(RESPONSE_TEXT.get(ep.response, ep.response), muted=True))
            return
        row = QHBoxLayout()
        row.setSpacing(8)
        usable = [a for a in d.actions if not (a.kind == "open_location" and not a.target.get("path"))]
        for a in usable[:2]:
            row.addWidget(button(a.label, "Primary" if a is usable[0] else "",
                                 on_click=lambda _=False, a=a: self._do(a)))
        row.addWidget(button("I meant to do that", on_click=lambda: ctx.respond(ep.id, "intentional")))
        more = button("More", "Ghost", "More options for this alert")
        menu = QMenu(more)
        for a in usable[2:]:
            menu.addAction(a.label, lambda a=a: self._do(a))
        menu.addAction("Not a problem", lambda: ctx.respond(ep.id, "not_a_problem"))
        if ep.z is not None:
            menu.addAction("This is my new normal", lambda: ctx.respond(ep.id, "new_normal"))
        menu.addAction("Dismiss", lambda: ctx.respond(ep.id, "dismiss"))
        more.setMenu(menu)
        self.more_menu = menu
        row.addWidget(more)
        row.addStretch(1)
        lay.addLayout(row)

    def _do(self, a) -> None:
        ctx = self.page.ctx
        if a.kind.startswith("show_"):
            ctx.navigate(a.kind.removeprefix("show_"))
        elif a.kind == "biggest_folders":
            ctx.navigate("checkup")
            page = ctx.window.pages.get("checkup") if ctx.window else None
            if page is not None:
                page.start_scan(a.target.get("mount"))
        else:
            ctx.act(a, self.ep.id)


class NoticeRow(QWidget):
    def __init__(self, page: "HomePage", n: Notice) -> None:
        super().__init__()
        ctx = page.ctx
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.addWidget(Chip("Idea", "accent"))
        lay.addLayout(vbox(label(n.title, "H3"), label(n.text, wrap=True, muted=True)), 1)
        if n.action == "run_benchmark":
            lay.addWidget(button("Run speed test", "Primary", on_click=ctx.run_benchmark))
        elif n.action == "run_checkup":
            lay.addWidget(button("Start check-up", "Primary", on_click=lambda: (ctx.navigate("checkup"), ctx.run_checkup())))
        lay.addWidget(button("Not now", "Ghost", on_click=lambda: (
            ctx.engine.dismiss_notice(n.id, declined_checkup=n.action == "run_checkup"), page.refresh_inbox())))


class HomePage(Page):
    key = "home"
    title = "Home"
    subtitle = "How your PC is doing compared with its own normal"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(18)
        self.body.addLayout(grid)

        # --- hero
        hero = Card(padding=22)
        self.mochi = MochiWidget(220)
        self.mochi.poked.connect(self._poke)
        self.mood_chip = Chip("Happy", "good")
        self.mood_desc = label("Everything is within your normal.", muted=True, wrap=True)
        self.mood_desc.setAlignment(Qt.AlignCenter)
        self.bubble = SpeechBubble("I'm feeling great! Everything looks normal.")
        hero.add(self.mochi)
        hero.lay.setAlignment(self.mochi, Qt.AlignHCenter)
        hero.add(hbox(None, self.mood_chip, None))
        hero.add(self.mood_desc)
        hero.add(self.bubble)
        self.learn_box = QWidget()
        lb = QVBoxLayout(self.learn_box)
        lb.setContentsMargins(0, 4, 0, 0)
        self.learn_label = label("Getting to know your PC", muted=True)
        self.learn_bar = QProgressBar()
        self.learn_bar.setRange(0, 100)
        self.learn_bar.setAccessibleName("Learning progress")
        lb.addWidget(self.learn_label)
        lb.addWidget(self.learn_bar)
        hero.add(self.learn_box)
        hero.lay.addStretch(1)
        grid.addWidget(hero, 0, 0, 3, 1)

        # --- weather
        self.weather_card = Card(padding=16)
        self.sky = QLabel()
        self.sky.setFixedSize(44, 44)
        self.weather_text = label("I'll have a forecast after a few days together.", wrap=True)
        self.weather_text.setStyleSheet("font-size: 15px; font-weight: 700;")
        self.weather_card.add(hbox(self.sky, vbox(label("Daily PC weather", muted=True), self.weather_text), spacing=14))
        self.weather_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        grid.addWidget(self.weather_card, 0, 1)

        # --- gauges
        gauges = Card(padding=16)
        self.g_cpu = RingGauge("CPU")
        self.g_ram = RingGauge("Memory")
        self.g_disk = RingGauge("Disk space")
        gauges.add(hbox(self.g_cpu, self.g_ram, self.g_disk, spacing=4))
        self.activity_label = label("", muted=True, wrap=True)
        self.activity_label.setAlignment(Qt.AlignCenter)
        gauges.add(self.activity_label)
        grid.addWidget(gauges, 1, 1)

        # --- small facts under the gauges
        facts = QHBoxLayout()
        facts.setSpacing(18)
        fact = Card(padding=16)
        fact.add(label("OS fact of the day", "H3"))
        self.fact_text = label("Collecting kernel counters...", wrap=True, muted=True)
        fact.add(self.fact_text)
        facts.addWidget(fact, 3)
        calm = Card(padding=16)
        self.calm_title = label("Calm streak", "H3")
        self.cakes = RiceCakes(max_cakes=10)
        self.calm_text = label("", muted=True)
        calm.add(self.calm_title)
        calm.add(self.cakes)
        calm.add(self.calm_text)
        facts.addWidget(calm, 2)
        grid.addLayout(facts, 2, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 7)

        self.roast_card = Card(padding=16, alt=True)
        self.roast_text = label("", wrap=True)
        self.roast_card.add(self.roast_text)
        self.roast_card.hide()
        self.body.addWidget(self.roast_card)

        # --- inbox (full width)
        self.inbox_card = Card(padding=20)
        self.inbox_title = label("Inbox", "H2")
        self.inbox_card.add(hbox(self.inbox_title, None, button("Mark all read", "Ghost", on_click=self._mark_read)))
        self.inbox_list = QVBoxLayout()
        self.inbox_list.setSpacing(4)
        self.inbox_card.add(self.inbox_list)
        self.body.addWidget(self.inbox_card)
        self.body.addStretch(1)

        self._slow_timer = QTimer(self)
        self._slow_timer.setInterval(60_000)
        self._slow_timer.timeout.connect(self.refresh_slow)
        self._poke_until = 0.0
        self.last_view: SampleView | None = None

    # ------------------------------------------------------------- updates
    def on_show(self) -> None:
        self.refresh_slow()
        self.refresh_inbox()
        self._slow_timer.start()

    def refresh_slow(self) -> None:
        eng = self.ctx.engine
        try:
            w = eng.weather()
        except Exception:
            w = None
        if w:
            self.weather_text.setText(w)
            self.sky.setPixmap(sky_pixmap(w.split(",")[0]))
        else:
            self.weather_text.setText("I'll have a forecast after a few days together.")
            self.sky.setPixmap(sky_pixmap("Mostly sunny"))
        try:
            fact = eng.os_fact()
        except Exception:
            fact = None
        self.fact_text.setText(fact["text"] if fact else "I'm still counting today's context switches and interrupts.")
        try:
            streak = eng.calm_streak()
        except Exception:
            streak = 0
        self.cakes.set_count(streak)
        self.calm_text.setText(f"{streak} calm day{'s' if streak != 1 else ''} in a row (no Uh-oh)")

    def refresh_inbox(self) -> None:
        clear_layout(self.inbox_list)
        eng = self.ctx.engine
        notices = eng.active_notices()
        eps = eng.inbox(20)
        for n in notices:
            self.inbox_list.addWidget(NoticeRow(self, n))
        for ep in eps[:12]:
            self.inbox_list.addWidget(AlertRow(self, ep))
        if not notices and not eps:
            empty = label("Nothing here. When something is off, I'll explain it here with one tip.", muted=True, wrap=True)
            self.inbox_list.addWidget(empty)
        open_n = sum(1 for e in eps if e.is_open)
        self.inbox_title.setText(f"Inbox · {open_n} ongoing" if open_n else "Inbox")

    def _mark_read(self) -> None:
        self.ctx.engine.alerts.clear_badge()
        self.ctx.toast("Inbox marked as read.")

    def _poke(self) -> None:
        v = self.last_view
        if v is None:
            return
        lines = [f"CPU {v.cpu:.0f}%" + self._usual(v, "cpu", "%") if v.cpu is not None else "CPU: warming up",
                 f"Memory {v.ram_used_gb:.1f} of {v.ram_total_gb:.0f} GB" + self._usual(v, "ram", " GB")]
        if v.primary_volume:
            m, free, fp = v.primary_volume
            lines.append(f"Disk {m} {free:.0f} GB free ({fp:.0f}%)")
        self.bubble.setText("\n".join(lines))
        self._poke_until = time.monotonic() + 8.0

    @staticmethod
    def _usual(v: SampleView, metric: str, unit: str) -> str:
        b = v.bands.get(metric)
        if not b:
            return " (still learning)"
        lo, hi = max(0.0, b[0] - 2 * b[1]), b[0] + 2 * b[1]
        return f" (usual {lo:.0f}-{hi:.0f}{unit})" if unit == "%" else f" (usual {lo:.1f}-{hi:.1f}{unit})"

    def on_view(self, v: SampleView) -> None:
        self.last_view = v
        name, desc = LABELS[v.mood]
        self.mochi.set_mood(v.mood)
        self.mood_chip.setText(name)
        self.mood_chip.fill = MOOD_FILL.get(v.mood, "good")
        self.mood_chip.restyle()
        self.mood_desc.setText(desc)
        if time.monotonic() > self._poke_until:
            self.bubble.setText(v.roast if v.roast and v.mood == Mood.HAPPY else v.speech)
        # learning
        if v.learning or v.progress < 0.9:
            self.learn_box.show()
            pctv = int(round(v.progress * 100))
            self.learn_bar.setValue(pctv)
            day = f" · learning mode, day {min(v.learning_day, 3)} of 3" if v.learning else ""
            self.learn_label.setText(f"Getting to know your PC: {pctv}%{day}")
        else:
            self.learn_box.hide()
        # gauges
        self._gauge_cpu(v)
        self._gauge_ram(v)
        self._gauge_disk(v)
        idle = "you're away" if v.user_idle else f"you're {v.activity}" if v.activity not in ("other", "idle") else "you're active"
        self.activity_label.setText(f"Compared with your normal for this hour while {idle} · sensitivity: {v.sensitivity}")
        self.roast_card.setVisible(bool(v.roast))
        if v.roast:
            self.roast_text.setText(v.roast)

    @staticmethod
    def _state(z: float | None) -> str:
        if z is None:
            return "good"
        return "critical" if z > 3 else "warning" if z > 2 else "good"

    def _gauge_cpu(self, v: SampleView) -> None:
        b = v.bands.get("cpu")
        band = (b[0] - 2 * b[1], b[0] + 2 * b[1]) if b else None
        sub = f"usual {max(0, band[0]):.0f}-{band[1]:.0f}%" if band else "learning your normal"
        self.g_cpu.set(v.cpu, f"{v.cpu:.0f}%" if v.cpu is not None else "--", sub, band, self._state(v.z.get("cpu")))

    def _gauge_ram(self, v: SampleView) -> None:
        b = v.bands.get("ram")
        total = v.ram_total_gb or 1
        band = ((b[0] - 2 * b[1]) / total * 100, (b[0] + 2 * b[1]) / total * 100) if b else None
        sub = f"usual {max(0, b[0] - 2 * b[1]):.1f}-{b[0] + 2 * b[1]:.1f} GB" if b else f"of {total:.0f} GB"
        state = self._state(v.z.get("ram"))
        if v.ram_used_pct >= 90:
            state = "critical"
        self.g_ram.set(v.ram_used_pct, f"{v.ram_used_gb:.1f} GB", sub, band, state)

    def _gauge_disk(self, v: SampleView) -> None:
        if not v.primary_volume:
            self.g_disk.set(0, "--", "no drive reported")
            return
        mount, free, fp = v.primary_volume
        state = "critical" if fp < 10 else "warning" if fp < 20 else "good"
        self.g_disk.set(100 - fp, f"{free:.0f} GB", f"free on {mount.rstrip(chr(92))}", None, state)

    def on_nerd(self, on: bool) -> None:
        self.refresh_inbox()
