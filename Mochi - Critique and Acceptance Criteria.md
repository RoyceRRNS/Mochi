# Mochi: Critique and Acceptance Criteria

Review of *Mochi - Baseline-Aware PC Health Buddy.pdf* (concept document, 16 October 2026).

The concept is strong and the scope is sensible. But as written, several things would stop the core demo from working.

- **Part 1** covers those problems.
- **Part 2** lists the decisions to make before writing code.
- **Part 3** is the acceptance criteria, as a checklist.

The five fixes that matter most for the core demo are problems 1–4 and 9. Without them, alerts never fire or fire repeatedly, and "I meant to do that" can't be measured.

---

## Part 1: Problems that would break the core as written

### 1. The baseline absorbs the anomaly before any alert can fire

The pseudocode updates the EWMA on every sample, including anomalous ones. Large deviations inflate σ quickly, so z collapses. Simulated with 5 s samples and a plateau that starts at +kσ:

| α | Plateau | z > 3 lasts | z > 2 lasts |
|---|---|---|---|
| 0.01 | +4σ | 25 s | 85 s |
| 0.01 | +10σ | 50 s | 110 s |
| 0.05 | +10σ | 10 s | 25 s |

- Psst needs 2 min and Heads up needs 5 min, so neither level can ever fire. The planned unit test ("a 6-minute plateau must alert") would fail.
- At a 5 s sample rate, α = 0.05 remembers only about 100 s of history. That is not "slowly follows habits".
- **Fix:** freeze the update (or clip its input to μ ± 2σ) while z > 2 or an episode is open. Add a way to accept a lasting "new normal".

### 2. Absolute limits only run during cold start

`check_absolute_limits` sits inside the `if b.samples < 30 … continue` branch. Once a bucket is trusted, "disk > 95%" and "temp > 95 °C" never fire. Free disk space isn't in the metric loop at all.

### 3. Trend-based rules have no trigger

Diagnosis runs only when one of the five z-metrics fires. A 50 MB/min leak or a slowly filling disk won't trip a z-score for a long time. Leak and disk-fill detection need their own periodic trend check.

### 4. The pseudocode has one alert level; the table has three

- It uses a single `Z_ALERT` and `SUSTAIN_SECONDS`.
- `streak = 0` after notifying means the same issue re-notifies every 5 minutes.
- One sample under the threshold resets the streak, so a plateau hovering around z ≈ 3 never alerts.
- `+5` assumes perfect 5 s ticks. Ticks slip exactly when the PC is struggling.
- **Fix:** a time-based alert state machine (open → escalate → close, with dedupe and cooldown). It should also merge related metrics: a memory hog raises RAM, swap and disk I/O at once, and that should be one alert, not three.

### 5. The PC is on Windows, and much of the diagnosis evidence is Linux-only in psutil

| What the rules need | psutil on Windows | Use instead |
|---|---|---|
| Paging / major faults | not available (`sin`/`sout` are always 0) | `Memory\Pages Input/sec` perf counter |
| Commit pressure | not available | `Memory\% Committed Bytes In Use` |
| I/O wait, disk busy | `iowait` and `busy_time` are Linux-only | `PhysicalDisk\% Idle Time`, `Avg. Disk Queue Length`, `Avg. Disk sec/Transfer` |
| Run queue | `getloadavg()` is emulated | `System\Processor Queue Length` |
| Throttling | `cpu_freq()` usually reports a fixed nominal value | `Processor Information\% Processor Performance`, `% Performance Limit` |
| Temperature | `sensors_temperatures()` doesn't exist | LibreHardwareMonitor (admin + kernel driver), or treat as optional |
| Battery drain rate | only % and time left | WMI `BatteryStatus.DischargeRate` |
| Foreground app / idle / full-screen | none | `GetForegroundWindow`, `GetLastInputInfo`, `SHQueryUserNotificationState` |

- pywin32 (`win32pdh`, `win32gui`) belongs in the tech stack.
- WMI is slow and needs `CoInitialize` in worker threads.
- Windows compresses memory before it pages, so "swap rising" shows up late.
- If Linux stays a target: Wayland has no general way to read the foreground window.

### 6. The runaway rule misses the common case

"> 50% of total CPU" can't catch a stuck single-threaded script, which uses 12.5% of an 8-thread CPU. The tip "python.exe at 98%" uses per-core units. The all-cores fault test passes, but real runaways won't trigger it.

### 7. Per-process attribution doesn't work for multi-process apps

- Chrome, Edge, VS Code, Discord and Teams each run 10–50 processes, and none looks big alone. "Closing Chrome (5.8 GB)" requires grouping processes by app.
- Summing RSS double-counts shared memory, so Detective totals can exceed physical RAM.
- A leaker outside the top-N list is invisible until it is already big.
- PIDs get reused, so identify processes by (pid, create_time).

### 8. Mochi and the test scripts are both `python.exe`

An alert saying "python.exe is the culprit" could mean either. Show the script name, or package Mochi as `Mochi.exe`, and exclude Mochi's own process.

### 9. "I meant to do that" can't work as described

- By the time the user clicks, 5+ minutes of the episode are already in the EWMA, and EWMA updates can't be undone. `is_marked_intentional(now)` checks the wrong moment. This needs a delayed commit or a snapshot/restore.
- "Similar episodes not flagged again" has no scope or expiry, so it becomes a permanent blind spot.
- A separate "Not a problem" answer is also needed. The false-alarm target can't be measured without it.

### 10. Idle detection is wrong in common cases, and Windows does its heaviest work while you're idle

- Watching a film means no input for two hours. Gamepad input usually doesn't reset `GetLastInputInfo`.
- Windows deliberately runs Defender scans, Update and the Search indexer when you're idle. The "High CPU at idle: unwanted software" rule will flag Windows itself.

### 11. The demo script doesn't fit the rules or the timeline

- Heads up needs 5 minutes sustained, and the whole demo is about 5 minutes.
- The Health Score is relative to the first two weeks, but benchmarks are built in week 5 and run weekly. That gives at most 2 runs by the demo.
- Demo steps 4–7 all depend on stretch features.

### 12. The disk benchmark measures RAM, not the disk

Reading back a file you just wrote is served from the OS cache, so use unbuffered I/O. Also:

- Running benchmarks in a Python thread holds the GIL, which freezes the UI and delays samples. Run them in a subprocess.
- Benchmarks spike the CPU, so Mochi's own detector will flag them unless they're excluded.

### 13. The overhead targets clash with the stack

- On Windows, pywebview runs on WebView2, which starts extra `msedgewebview2.exe` processes. Counted honestly, that likely breaks 100 MB.
- PySide6 + pyqtgraph + numpy is already close to 100 MB.
- "2% CPU" is undefined: of what total, with the dashboard open or closed?

### 14. The palette fails its own WCAG claim

Computed contrast ratios:

- Light mode: Milk Tea text is **3.83:1** on Rice Cream and **4.03:1** on white. Cocoa on Cherry is **4.46:1**. All three are below 4.5:1.
- Dark mode: light text on the pastel fills is **1.5–2.2:1**. Use dark text on fills there.
- Light-mode fills against the background are **1.3–2.2:1** (Mango is 1.33). That fails the 3:1 rule for gauges, badges and graph lines.

### 15. Buckets are sparse, and 30 samples is too few to trust

- 48 slots × 7 activities = 336 buckets per metric.
- 30 samples is 2.5 minutes of one session. Consecutive samples are nearly identical, so σ comes out tiny and false alarms follow.
- After 3 days, the weekend buckets may hold no data at all.
- This needs a fallback order, a minimum absolute change on top of z, and a learning % that can actually reach 100%.

### 16. The timeline has gaps

- Week 1 must already record the foreground app, idle time and per-process data. Otherwise week-1 data can't be replayed into baselines.
- Week 1 also needs autostart and a dev database separate from the always-on one.
- Notifications (week 3) need a tray shell first.
- Detective is scheduled in week 4 but listed as stretch. Settings and Diary aren't scheduled at all.
- Fault-injection tests come in week 6, after the gate that needs them.

**One cheap improvement:** scheduling coverage is thin; Mochi only reads the run queue. A "lower priority" action is safer than ending a process and actually demonstrates scheduling.

---

## Part 2: Decisions to make first

| Decision | Recommendation |
|---|---|
| Primary platform | Windows 11, the PC that collects data and runs the demo. Linux is a bonus. |
| UI stack | PySide6 + pyqtgraph (one process) |
| Notifications | plyer and `QSystemTrayIcon.showMessage` can't show buttons or custom images. Use a custom Qt popup that doesn't steal focus, or WinRT toasts (these need an AppUserModelID). |
| CPU % unit | Store both % of total and % of one core; each rule states which it uses |
| Memory unit | Private bytes, summed per app group |
| Privileges | Always run without admin; admin-only checks say "needs admin" |
| Baseline updates | Freeze/clip during episodes, plus a delayed commit so feedback can exclude episodes |
| "Each user" | One baseline set per OS account on this PC |
| False alarm | An alert the user marks "Not a problem"; Psst counted separately |
| Out of scope | GPU, network, per-app power, processes inside WSL/Docker. Write these down. |

---

## Part 3: Acceptance criteria

**Legend:** untagged = core · **[S]** = stretch · ★ = not in the concept document.

The numbers are proposals. Tune them, but keep a number on every criterion so it stays testable.

### A. Lifecycle and resilience

- [ ] **LIF-1** ★ Starts at login without admin; toggle in Settings. Needed in week 1, because gate 1 depends on daily collection.
- [ ] **LIF-2** ★ Only one instance runs; launching again focuses the running one.
- [ ] **LIF-3** ★ Closing the window hides Mochi to the tray; Quit is in the tray menu. On quit, logoff or shutdown, buffered samples are saved within 2 s.
- [ ] **LIF-4** ★ After a hard kill: no database corruption, at most 60 s of samples lost, baselines intact.
- [ ] **LIF-5** ★ A gap over 15 s (sleep, hibernate) resets all streaks. Grace periods: 3 min after resume, 5 min after login.
- [ ] **LIF-6** ★ Durations use a monotonic clock. Daylight-saving, time-zone and manual clock changes cause no crash and no negative or double-counted time.
- [ ] **LIF-7** ★ A crashed worker thread is logged and restarted. The UI shows "last sample N s ago".
- [ ] **LIF-8** ★ Rotating log file (≤ 10 MB).
- [ ] **LIF-9** ★ All tunables live in one config file. A bad config falls back to defaults with a warning.
- [ ] **LIF-10** ★ Uptime features account for Windows Fast Startup, where shutting down doesn't reset boot time.

### B. Collector

- [ ] **COL-1** System sample every 5 s ± 1 s on fixed ticks: CPU total and per core, available RAM, commit %, paging rate, per-disk busy/queue/latency, free space per fixed volume, CPU performance %, temperature and battery where available, context-switch and interrupt counters, idle seconds, foreground exe, full-screen state.
- [ ] **COL-2** Top 15 processes by CPU and by memory each sample. ★ Every app group's memory every 60 s.
- [ ] **COL-3** ★ Processes identified by (pid, create_time). Process objects are cached between samples, because psutil's first `cpu_percent()` call returns 0.
- [ ] **COL-4** ★ CPU stored as both % of total and % of one core.
- [ ] **COL-5** ★ Processes grouped into apps; app memory = summed private bytes.
- [ ] **COL-6** ★ Processes that vanish or deny access are handled one by one. Unreadable values are stored as null, never 0.
- [ ] **COL-7** ★ "System Idle Process" is never a culprit; Mochi's own process is tagged.
- [ ] **COL-8** Unavailable metrics show "unavailable (reason)", never 0 and never a crash.
- [ ] **COL-9** Bounded queue; when full, the oldest samples are dropped and the drops counted.
- [ ] **COL-10** ★ The week-1 schema holds everything later weeks need.
- [ ] **COL-11** ★ One collector cycle takes ≤ 300 ms (95th percentile).

### C. Storage

- [ ] **STO-1** SQLite in `%LOCALAPPDATA%\Mochi`, WAL mode, one writer thread, commits batched about every 30 s.
- [ ] **STO-2** ★ Retention: raw samples 14 days → 1-minute averages 90 days → hourly forever. Database < 500 MB at the demo.
- [ ] **STO-3** ★ Schema versioning and migrations; an update never wipes data.
- [ ] **STO-4** Baselines persist across restarts.
- [ ] **STO-5** ★ Hourly baseline snapshots, so the Timeline shows the band as it was at that time.
- [ ] **STO-6** ★ Each alert records: times, level, metrics, cause, culprit, tip, user response, bucket level used.
- [ ] **STO-7** ★ Export and "Delete all my data" in Settings.
- [ ] **STO-8** ★ Database and logs are git-ignored.
- [ ] **STO-9** ★ Dev runs use a separate database from the always-on instance.

### D. Context buckets

- [ ] **CTX-1** 48 time slots: local hour × weekday/weekend.
- [ ] **CTX-2** Activity comes from an editable app mapping with defaults for ≥ 30 apps; unknown apps → "other".
- [ ] **CTX-3** ★ Activity = the dominant class over the last 60 s.
- [ ] **CTX-4** ★ One shared definition of idle: no input for ≥ 10 min, AND not full-screen, AND no media or game app in the foreground. The lock screen counts as idle.
- [ ] **CTX-5** ★ A bucket is trusted after ≥ 30 samples spread across ≥ 2 days.
- [ ] **CTX-6** ★ Fallback when a bucket isn't trusted: slot + activity → activity only → global → absolute limits.
- [ ] **CTX-7** ★ Each sample records whether the PC is on mains power or battery.

### E. Baseline and detection

- [ ] **DET-1** EWMA seeded from the first samples; matches a reference implementation on synthetic data.
- [ ] **DET-2** ★ z uses μ and σ from before the update. The document's formula and pseudocode currently disagree on this.
- [ ] **DET-3** ★ No baseline update while z > 2. Test: a +4σ plateau still has z > 3 at minute 6.
- [ ] **DET-4** ★ A per-metric MIN_STD, plus a minimum absolute change (e.g. CPU +15 points, RAM +1 GB).
- [ ] **DET-5** ★ Each z-metric is documented as one-sided (only high values count).
- [ ] **DET-6** Sustain is time-based with tolerance (≥ 80% of samples above threshold). A 10 s spike → no alert. A 6-min plateau → alert. ★ A plateau oscillating around the threshold → alert.
- [ ] **DET-7** One episode escalates Psst → Heads up → Uh-oh without duplicate alerts.
- [ ] **DET-8** ★ Absolute limits are checked on every sample.
- [ ] **DET-9** 3-day learning mode. ★ Learning % = share of active time spent in trusted buckets.
- [ ] **DET-10** ★ A trend detector runs every 5 min: app memory slope and free-space slope.
- [ ] **DET-11** ★ The sensitivity slider maps to documented thresholds and applies without a restart.
- [ ] **DET-12** ★ Mochi's own benchmarks and check-ups are excluded from baselines and detection.
- [ ] **DET-13** ★ A lasting change becomes the new normal after N days, or when the user confirms it.

### F. Alert lifecycle

- [ ] **ALR-1** ★ An episode = (cause, culprit app). One notification per level per episode.
- [ ] **ALR-2** ★ Related metrics merge into one episode.
- [ ] **ALR-3** ★ An episode closes after 2 min back within baseline.
- [ ] **ALR-4** ★ 30-min cooldown before re-alerting the same issue, unless it gets worse.
- [ ] **ALR-5** ★ At most 5 Heads-up popups a day; the rest go into a summary.
- [ ] **ALR-6** ★ Snooze from the tray: 1 hour, or until tomorrow.
- [ ] **ALR-7** Shh mode holds Psst and Heads up during full-screen apps ★ and Windows Do Not Disturb. Uh-oh still gets through. A summary appears within 1 min afterwards.
- [ ] **ALR-8** Uh-oh stays until dismissed. ★ It returns only if things get worse or after 24 h.

### G. Diagnosis

- [ ] **DIA-1** Rules run in a documented order. Each returns a cause, a culprit (or "no single culprit") and a tip. Every number comes from live data.
- [ ] **DIA-2** ★ If no rule matches, Mochi says so and lists the top resource users.
- [ ] **DIA-3** ★ Co-occurrence tests: memory pressure wins over disk I/O; runaway wins over throttling.
- [ ] **DIA-4** Memory pressure: paging ≥ k × baseline AND (available RAM < 10% OR commit > 90%).
- [ ] **DIA-5** Runaway: ★ ≥ 90% of one core for 5 min, OR ≥ 50% of total CPU. System processes are never called "stuck".
- [ ] **DIA-6** Disk I/O: busy ≥ 90% or high latency for ≥ 2 min. The culprit is worded "probably", ★ because Windows per-process I/O counters aren't disk-only.
- [ ] **DIA-7** Leak: growth ≥ 100 MB/h over ≥ 60 min, R² ≥ 0.8, no drop > 10%. ★ Ignore list for apps that grow by design (browsers, IDEs).
- [ ] **DIA-8** [S] Throttling: CPU busy AND performance % ≤ 70%. ★ Temperature limits relative to the chip's maximum; Ryzen 7000 runs at 95 °C by design.
- [ ] **DIA-9** Disk space: free < 10% AND ★ < 20 GB, or ★ projected full within 7 days. Fixed drives only.
- [ ] **DIA-10** ★ Known Windows maintenance processes (MsMpEng, TiWorker, SearchIndexer…) get a reassuring explanation.
- [ ] **DIA-11** [S] Battery: ★ worded as "top CPU user on battery", since per-app power can't be measured.
- [ ] **DIA-12** ★ Never calls a process malicious. "Unfamiliar" = unsigned, or running from Temp/Downloads; show its publisher.
- [ ] **DIA-13** ★ VmmemWSL and Docker are explained as work happening inside a virtual machine.
- [ ] **DIA-14** ★ No claims Mochi can't measure, such as "syncing 3 GB" or "47 tabs".

### H. Actions

- [ ] **ACT-1** Open file location, open startup settings, show biggest folders, end process, ★ lower priority.
- [ ] **ACT-2** ★ End process: the confirm dialog shows name and PID plus an unsaved-work warning. Re-check (pid, create_time) before acting. Close gracefully first, force after 5 s. System processes are never offered.
- [ ] **ACT-3** ★ Folder scan runs off the UI thread and can be cancelled. It skips junctions, doesn't download OneDrive cloud-only files, and shows partial results within 60 s.

### I. Feedback

- [ ] **FB-1** "I meant to do that" on every alert; the reply names the app.
- [ ] **FB-2** ★ The whole episode is excluded from the baseline. Test: the result equals a baseline computed without the episode.
- [ ] **FB-3** ★ The suppression is scoped to (app, metric, activity), expires after 30 days, and can be undone in Settings.
- [ ] **FB-4** ★ A separate "Not a problem" button.

### J. Notifications

- [ ] **NOT-1** Mood face, level title, cause with "vs your usual", one tip, two buttons.
- [ ] **NOT-2** ★ The chosen mechanism supports buttons, the mood image and persistence on Windows 11.
- [ ] **NOT-3** ★ Never steals focus; never appears over full-screen apps.
- [ ] **NOT-4** Psst = tray badge only.
- [ ] **NOT-5** ★ An alert inbox on Home for anything missed.

### K. Screens and tray

- [ ] **UI-1** Tray mood face legible at 16 and 32 px, ★ on both light and dark taskbars. Tooltip shows status. Menu: Open / Snooze / Quit.
- [ ] **UI-2** ★ First-run screen: data stays local, learning mode, pinning the tray icon (Windows 11 hides new ones), which features need admin.
- [ ] **UI-3** Home: mood plus a text label, and gauges with numbers. ★ Decide whether the "disk" gauge means space or activity.
- [ ] **UI-4** Timeline: ★ 1 h / 24 h / 7 d ranges. ★ Band = μ ± 2σ of the bucket active at that time. Anomaly markers explain themselves on hover.
- [ ] **UI-5** Settings: everything in the concept document, plus ★ autostart, snooze, suppression list, export/delete, theme.
- [ ] **UI-6** Nerd stats: z, μ, σ, the bucket and fallback level used, queue drops, Mochi's own CPU and RAM.
- [ ] **UI-7** ★ The UI responds within 2 s during the memory-pressure test.
- [ ] **UI-8** ★ Animations stop while the window is hidden.

### L. Mascot and moods

- [ ] **MOOD-1** ★ A precedence order for overlapping moods (e.g. Dizzy > Sweaty > Grumpy > Puffy > Zoomies > Proud > Sleepy > Happy).
- [ ] **MOOD-2** ★ The mood reacts within 30 s, independently of the 5-minute alert rule, and lasts at least 30 s so it doesn't flicker.
- [ ] **MOOD-3** Every mood has a text label and is distinguishable in grayscale.
- [ ] **MOOD-4** Original art. The OS "reduce motion" setting turns off animation loops (★ read Windows' "Animation effects" setting).

### M. Design and accessibility

- [ ] **DS-1** All text ≥ 4.5:1. Fix Milk Tea, Cocoa on Cherry, and dark-mode text on fills.
- [ ] **DS-2** ★ Gauges, badges and graph lines ≥ 3:1 against their background. Alert level is never shown by colour alone.
- [ ] **DS-3** ★ Works at 100–200% Windows scaling and in high-contrast mode. Fully keyboard-navigable; custom widgets have screen-reader names.
- [ ] **DS-4** ★ Tabular figures verified to work (needs Qt ≥ 6.7's `QFont.setFeature`).
- [ ] **DS-5** ★ Qt stylesheets have no box-shadow, so check the CPU cost of `QGraphicsDropShadowEffect`.

### N. Startup timing [S]

- [ ] **STR-1** ★ App launches detected within 1 s. 5 s sampling can't tell 2.1 s from 7.8 s.
- [ ] **STR-2** "Ready" = the app's processes stay under 5% CPU for 2 s; those 2 s are excluded from the time. No settle within 60 s → discarded.
- [ ] **STR-3** ★ The first launch after boot (cold) is compared only with other cold launches.
- [ ] **STR-4** ★ Relaunching an already-open app doesn't count.
- [ ] **STR-5** ★ The trend rule needs ≥ 10 earlier launches. "Several apps" = ≥ 3.

### O. Benchmarks and Health Score [S]

- [ ] **BEN-1** ★ Benchmarks run in a subprocess.
- [ ] **BEN-2** ★ ≤ 15 s total. Aborted if the user comes back or background load is above 10%.
- [ ] **BEN-3** ★ Records power source, power mode and background load; compares only like with like.
- [ ] **BEN-4** ★ Each test runs 3 times and the median is kept. Measure the run-to-run spread; it tells you which drops are real.
- [ ] **BEN-5** ★ The disk test bypasses the OS cache, is skipped if free space < 2 GB, and its temp file is cleaned up.
- [ ] **BEN-6** ★ Daily runs for the first 14 days, then weekly. Desktops count as plugged in.
- [ ] **BEN-7** ★ Document the score formula. Add a "re-baseline" button for hardware changes. Note that a PC already slow when Mochi is installed still scores 100.
- [ ] **BEN-8** ★ Suggest a check-up only after 2 low scores in a row; after a "no", wait 7 days.

### P. Guided PC check [S]

- [ ] **CHK-1** ★ Each item has thresholds for fine / worth a look / needs action, plus an "unavailable (why)" state.
- [ ] **CHK-2** SMART status requires smartctl and admin; otherwise Mochi explains why it's missing.
- [ ] **CHK-3** Startup items include ★ logon scheduled tasks. Boot delay is shown only if it can be measured (the Windows log for it usually needs admin).
- [ ] **CHK-4** ★ The load test lasts ≤ 30 s and stops early if the CPU nears its temperature limit.
- [ ] **CHK-5** ★ The whole check takes ≤ 90 s. The report card is saved; the top 3 suggestions are ranked by severity.
- [ ] **CHK-6** Battery health = full-charge capacity ÷ design capacity; hidden on desktops.

### Q. Quirks [S]

- [ ] **QRK-1** ★ Detective totals never exceed physical RAM.
- [ ] **QRK-2** ★ The weather report appears at the first session after 5 am, is calculated from past evenings, and stays hidden until 3 days of data exist.
- [ ] **QRK-3** ★ OS facts are calculated since the start of the day and handle reboots.
- [ ] **QRK-4** ★ "Feed Mochi" counts only when an app named in a tip exits within 10 min of that tip.
- [ ] **QRK-5** ★ Roasts use only measurable facts. Chrome's process count is not its tab count.
- [ ] **QRK-6** ★ "Under your RAM baseline all week" can't happen: the baseline is roughly your average, so you're above it about half the time. Use "no RAM alerts for 7 days" instead. Hide achievements whose metric is unavailable.
- [ ] **QRK-7** ★ Calm streaks count only days with ≥ 1 h of data.
- [ ] **QRK-8** ★ "Fastest boot" needs a defined way to measure boot time.

### R. Privacy and security

- [ ] **SEC-1** ★ Zero network traffic, checked over a 24 h run.
- [ ] **SEC-2** ★ Runs without admin.
- [ ] **SEC-3** ★ Never stores window titles, URLs or full command lines; command lines can contain tokens.
- [ ] **SEC-4** ★ Works with no third-party drivers installed. Older LibreHardwareMonitor builds ship the WinRing0 driver, which Defender now flags.

### S. Performance

- [ ] **PERF-1** CPU ≤ 2% of total (1-hour average, dashboard closed); ★ ≤ 5% with the Timeline open. Measure Mochi plus its child processes and state the core count.
- [ ] **PERF-2** RAM ≤ 100 MB of private bytes, ★ child processes included.
- [ ] **PERF-3** ★ 72-hour soak test: Mochi's own memory grows less than 10 MB.

### T. Testing and evaluation

- [ ] **TST-1** Unit tests also cover ★: oscillating plateaus, the baseline not absorbing an anomaly, rule order when causes overlap, bucket edges (midnight, Friday → Saturday, daylight saving), the alert state machine, and feedback exclusion.
- [ ] **TST-2** ★ A replay harness feeds recorded samples through the detector at ≥ 100× speed. The same input always produces the same alerts.
- [ ] **TST-3** ★ Mocks for denied access, vanished processes, and missing sensors or battery.
- [ ] **TST-4** Each fault test runs 3 times against a time limit:
  - [ ] Memory hog → alert naming `memory_hog.py` within 7 min.
  - [ ] Runaway ★ (all cores and single thread) → within 6 min.
  - [ ] Leak → within 20 min, with growth rate accurate to ±20%.
  - [ ] Disk bottleneck → within 5 min. ★ stress-ng is Linux-only; use diskspd.
  - [ ] Normal heavy use in a trusted bucket → 0 alerts.
- [ ] **TST-5** ★ Fault scripts are capped, stop themselves on a timer, and clean up after themselves.
- [ ] **TST-6** False-alarm week: every alert labeled; false Heads up + Uh-oh < 1 per day. ★ Report Psst separately.
- [ ] **TST-7** ★ Tests for sleep/resume, restart, hard kill and database migration.

### U. Demo

- [ ] **DEMO-1** ★ The demo runs on the same PC that collected ≥ 14 days of data.
- [ ] **DEMO-2** ★ A clearly labeled demo profile shortens the sustain times, or the hog starts 6 min before the demo.
- [ ] **DEMO-3** ★ A core-only version of the script, in case stretch features get cut.
- [ ] **DEMO-4** ★ At least 10 benchmark runs exist before demo day.
- [ ] **DEMO-5** ★ Backup: a screen recording, plus a replay of a captured incident.
- [ ] **DEMO-6** ★ Rehearsed on the demo PC with the projector.

### V. Deliverables

- [ ] **DOC-1** The report maps each OS concept to the code and to a demo moment.
- [ ] **DOC-2** ★ README covering setup, permissions, data location, Windows metric limits and the out-of-scope list.
- [ ] **DOC-3** ★ A git tag at each gate.
- [ ] **DOC-4** ★ The report includes false-alarm rate, detection latency and overhead, with the method used for each.
