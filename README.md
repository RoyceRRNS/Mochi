# Mochi: a baseline-aware PC health buddy

Operating Systems mini project by **Royce B Selvaraj (24BCB0078)** and **Srujan Buddharaju (24BDS0347)**.

Mochi is a Windows 11 tray app that learns what "normal" looks like on your PC, then tells
you in plain words when something is off, why it is happening, and one thing to do about
it. A rice-cake mascot in the tray shows the machine's health at a glance: puffy when
memory is full, sweaty when the CPU is hot, dizzy when the PC is swapping, grumpy when a
drive is nearly full.

- Concept: [Mochi - Baseline-Aware PC Health Buddy.pdf](Mochi%20-%20Baseline-Aware%20PC%20Health%20Buddy.pdf)
- Critique and acceptance criteria: [Mochi - Critique and Acceptance Criteria.md](Mochi%20-%20Critique%20and%20Acceptance%20Criteria.md)
- Case study report: [Mochi - OS Case Study.pdf](Mochi%20-%20OS%20Case%20Study.pdf)

## Setup and running

Requirements: Windows 10/11, Python 3.11+ (developed on 3.12). No admin rights needed.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python -m pip install -e .[test]
```

| Command | What it does |
| --- | --- |
| `python -m mochi` | Tray app with the dashboard (first run shows a short welcome) |
| `python -m mochi --start-hidden` | Start quietly in the tray (used by "start at sign-in") |
| `python -m mochi --profile dev` | Separate data folder for development, so it never touches your real data |
| `python -m mochi --demo` | Demo profile: much shorter waits before alerts, clearly labelled in the UI |
| `python -m mochi --simulate` | The full UI on simulated data (a memory hog appears every 10 minutes) |
| `python -m mochi --headless --seconds 60` | Engine only, no UI (used for overhead measurements) |

Data lives in `%LOCALAPPDATA%\Mochi\<profile>` (database, config, logs). It never goes
into the repo, and Mochi never connects to the internet.

## How it works

```
Windows ──> Collector thread ──(bounded queue)──> Processing thread ──> SQLite (WAL, writer thread)
            every 5 s                             │
                                                  ├─ Baselines: EWMA per hour-of-week x activity
                                                  ├─ Detector: sustained z-scores, absolute limits
                                                  ├─ Trends: leak, disk filling, runaway process
                                                  ├─ Diagnosis: ordered rules -> cause, culprit, tip
                                                  ├─ Alerts: one episode per culprit
                                                  └─ Mood ──> UI (tray, popups, six screens)
Supervisor thread restarts a dead worker; a startup watcher times app launches;
benchmarks run in a separate process.
```

| Module | Responsibility |
| --- | --- |
| `mochi/platform/windows.py` | All processes from one `NtQuerySystemInformation` call; paging, commit, disk busy/latency, run queue and throttling from performance counters (PDH); battery drain from `CallNtPowerInformation`; idle time, foreground app and full-screen state via ctypes |
| `mochi/collector.py`, `grouping.py` | Per-process rates; processes grouped into apps (all `chrome.exe` = one app; scripts named, e.g. `python.exe (memory_hog.py)`; Mochi and its children tagged as self) |
| `mochi/activity.py`, `buckets.py` | Activity from the foreground app (editable mapping), one shared idle definition, 48 time slots, fallback buckets |
| `mochi/baseline.py` | EWMA mean/variance, trust rule (>= 30 samples on >= 2 days), contamination guard, 20-minute delayed commit, "new normal" relearning |
| `mochi/detector.py`, `trends.py` | Sustained detection (>= 80% of samples over 2 or 5 minutes), practical floors, absolute limits, leak / disk-fill / runaway trackers |
| `mochi/diagnosis.py` | Rules in order: memory pressure, runaway, disk I/O, leak, memory hog, throttling, storage, idle CPU, CPU hog, battery; plain-language fallback |
| `mochi/alerts.py`, `feedback.py` | Episode lifecycle (escalate, close after 2 quiet minutes, 30-minute cooldown, 5 popups a day, snooze, Shh mode); "I meant to do that", "Not a problem", "new normal" |
| `mochi/actions.py`, `folderscan.py` | Safe actions: close gracefully then end, lower priority, open location; biggest-folder scan that skips junctions and never downloads OneDrive files |
| `mochi/storage.py` | SQLite schema with migrations, batched commits, retention (raw 14 days, minute averages 90 days, hourly forever), export and delete |
| `mochi/startup_tracker.py`, `benchmarks.py`, `bench_worker.py`, `checkup.py`, `quirks.py` | App launch timing, unbuffered benchmarks + Health Score, guided PC check, weather, OS facts, Feed Mochi, achievements, diary, records |
| `mochi/ui/` | PySide6 "Soft Serve" UI: mascot, tray, non-focus-stealing popups, Home, Timeline, Detective, Check-up, Diary, Settings, onboarding |

### Health Score

```
reference_t = median of the first 10 valid runs with the same power source and power mode
              (needs >= 3 runs; until then the score is "still learning")
ratio_t     = min(1.2, reference_t / current_t)
score       = min(100, 100 * sum(w_t * ratio_t) / sum(w_t))
weights     = CPU 0.35, memory 0.25, disk write 0.20, disk read 0.20
```

Tests run daily for the first 14 days, then weekly, only while you're away and plugged in.
Because the score compares the PC with its own early runs, a PC that was already slow when
Mochi was installed still scores 100; "Re-baseline" starts a new reference after an upgrade.

## OS concepts covered

| Module of the syllabus | Where it appears in Mochi |
| --- | --- |
| OS services, system calls | `NtQuerySystemInformation`, PDH counters, `CallNtPowerInformation`, `GetLastInputInfo`, `SHQueryUserNotificationState`, WinVerifyTrust |
| Processes and threads | Per-process attribution by (PID, start time), process trees and app grouping, five cooperating threads |
| Scheduling | Run-queue length, context switches (OS fact of the day), "let it run slower" = lowering process priority |
| Concurrency | Producer-consumer bounded queue, locks around baselines, supervisor restarting dead threads, single-instance lock |
| Memory management | Paging rate, commit charge, private working set vs committed memory, memory-pressure and leak detection |
| Storage and file systems | Disk busy time and latency, unbuffered disk benchmark (`FILE_FLAG_NO_BUFFERING`), folder scan with junction and placeholder handling, SQLite WAL |
| Protection and security | Runs without admin (least privilege), never ends system processes, signature and publisher checks, no network, no window titles or command lines stored |

## Design decisions that differ from the concept

- **The baseline never learns from anomalous readings.** A plain EWMA updated on every
  sample absorbs a sustained plateau within about a minute, so no 2- or 5-minute alert could
  ever fire (see the critique, problem 1).
- **Heads up and Uh-oh also need a practical floor.** z-scores on steady metrics get huge for
  small changes, so Uh-oh for memory needs >= 85% of RAM in use, for CPU >= 85%, and so on
  (`level_floor_ok` in `detector.py`). Psst stays purely statistical.
- **One episode per culprit.** A memory hog that turns into memory pressure stays one alert
  whose explanation sharpens, instead of two notifications.
- **Two extra rules:** "memory hog" and "CPU hog" name the busiest app before slower evidence
  (paging, 5 minutes of runaway history) exists.
- **Learning mode** (first 3 days) uses only absolute limits and objective trends; the
  per-process runaway rule starts after it.
- **Uh-oh gets through Shh mode and snooze**, even over a full-screen app (safety over
  politeness); Psst and Heads up are held and summarised afterwards.
- **NumPy's BLAS is pinned to one thread** (`mochi/__init__.py`); otherwise it commits about
  750 MB of memory on a 32-thread CPU.

## Results

Measured on the development laptop (Intel Core i9-14900HX, 32 threads, 32 GB, Windows 11):

| Measure | Target | Result |
| --- | --- | --- |
| Mochi's CPU, tray only | < 2% of total | 0.35% (90 s average) |
| Mochi's CPU, dashboard open | < 5% of total | 0.34% average, 1.3% peak (about 11% of one core on average) |
| Mochi's memory | < 100 MB private | 95 MB peak |
| Background engine alone | < 2% CPU, < 100 MB | Passed (`test_headless_overhead_is_small`) |
| Reading all processes | Cheap | One system call vs about 430 ms of CPU per scan with psutil |
| Memory hog, simulated | Popup within 7 min | Passed; Puffy within 30 s |
| Runaway, one core and all cores | Within 6 min | Passed; also caught on this PC with `tools/cpu_burner.py` |
| Leak at 50 MB/min | Within 20 min, rate +-20% | Passed; real `tools/leaky.py` detected |
| Disk bottleneck / disk filling | 5 min / 1 min | Passed; real `tools/disk_thrash.py` attributed |
| Normal heavy use in its usual slot | No alert | Passed (gaming at 9 pm) |
| False alarms | < 1 per day | Simulated week passed; a real week on this PC is still to be run |

## Tests

453 tests, all passing:

| Suite | Tests | What it covers |
| --- | --- | --- |
| `tests/unit` | 320 | Every module, positive and negative cases (maths, rules, lifecycle, storage, privacy, contrast) |
| `tests/e2e/test_scenarios.py` | 30 | Full pipeline on a warmed-up simulated PC: every fault in the test table, feedback, quiet modes, learning mode, restart, sleep, clock changes, benchmarks, check-up, quirks |
| `tests/e2e/test_real_machine.py` | 19 | Real Windows readings, real process actions, CLI, single instance, hard kill, real benchmark, light capped fault injection, overhead |
| `tests/ui` | 84 | Widgets, mascot, tray, popups, every screen, keyboard and theme handling, full user journeys |

```bash
.venv\Scripts\python -m pytest
```

```bash
.venv\Scripts\python -m pytest -m "not realmachine and not slow"
```

The real-machine tests put light, capped load on the PC (about 30 s of one CPU core, a
2 GB allocation, a 1 GB temp-file loop, a short leak); every script stops itself and cleans up.

### Fault-injection scripts (`tools/`)

| Script | Example |
| --- | --- |
| `memory_hog.py` | `python tools/memory_hog.py --gb 2 --ramp-s 20 --hold-s 60` |
| `cpu_burner.py` | `python tools/cpu_burner.py --threads 1 --seconds 60` |
| `leaky.py` | `python tools/leaky.py --mb-per-min 50 --minutes 25` |
| `disk_thrash.py` | `python tools/disk_thrash.py --mb 1024 --seconds 90` |
| `disk_filler.py` | `python tools/disk_filler.py --until-free-pct 9 --max-gb 50` |
| `measure_overhead.py` | `python tools/measure_overhead.py --seconds 3600` |

## Known limitations

- CPU temperature and SMART details need admin rights or LibreHardwareMonitor; without
  them Mochi shows "unavailable (needs admin)", never 0.
- GPU, network and per-app power use are not monitored.
- Windows' Do Not Disturb is detected only through the "quiet time" notification state.
- The Linux/macOS adapter (`platform/generic.py`) is best effort and untested on those systems.
- The real week-long false-alarm study is still to be done.
