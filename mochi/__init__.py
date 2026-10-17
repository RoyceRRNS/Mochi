"""Mochi - a baseline-aware PC health buddy.

Package layout (pure logic is kept free of OS and Qt calls so it can be unit tested):

- ``platform``   OS adapters (Windows, generic psutil, scripted fake for tests)
- ``collector``  turns raw OS readings into timestamped samples
- ``baseline``   contextual EWMA baselines with contamination guard and delayed commit
- ``detector``   sustained z-score detection plus absolute limits
- ``trends``     leak and disk-fill trend detection
- ``diagnosis``  ordered rules that explain an anomaly and pick a culprit
- ``alerts``     episode state machine (dedupe, escalate, cooldown, snooze, shh)
- ``engine``     wires everything together on worker threads
- ``ui``         PySide6 front end ("Soft Serve" design language)
"""

import os as _os

# NumPy's OpenBLAS reserves a work buffer per hardware thread when it loads: about 750 MB
# of committed memory on a 32-thread CPU. Mochi does no linear algebra, so one thread is
# plenty (PERF-2). Must run before anything imports numpy (pyqtgraph does).
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    _os.environ.setdefault(_var, "1")

__version__ = "0.1.0"
APP_NAME = "Mochi"
