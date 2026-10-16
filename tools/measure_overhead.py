"""Measure Mochi's own CPU and memory (PERF-1, PERF-2).

    python tools/measure_overhead.py --seconds 3600            # headless engine, 1 hour
    python tools/measure_overhead.py --seconds 600 --ui         # with the dashboard open

Measures the Mochi process *and its children* from the outside every second and prints
the average CPU as % of the whole machine, plus peak private memory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import psutil


def measure(cmd: list[str], seconds: float, warmup: float = 15.0) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(cmd, env=env)
    p = psutil.Process(proc.pid)
    ncpu = psutil.cpu_count() or 1
    cpu_samples: list[float] = []
    mem_peak = 0
    cached: dict[int, psutil.Process] = {}
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < seconds + warmup and proc.poll() is None:
            time.sleep(1.0)
            procs = [p] + p.children(recursive=True)
            total_cpu = 0.0
            mem = 0
            for q in procs:
                c = cached.setdefault(q.pid, q)
                try:
                    total_cpu += c.cpu_percent(None)
                    mi = c.memory_info()
                    mem += getattr(mi, "private", mi.rss)
                except psutil.Error:
                    continue
            if time.monotonic() - t0 > warmup:
                cpu_samples.append(total_cpu / ncpu)
                mem_peak = max(mem_peak, mem)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    avg = sum(cpu_samples) / len(cpu_samples) if cpu_samples else None
    return {"seconds": seconds, "cpu_avg_pct_of_total": avg, "cpu_max_pct_of_total": max(cpu_samples or [0]),
            "private_mb_peak": mem_peak / 1024 ** 2, "logical_cpus": ncpu, "samples": len(cpu_samples)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--ui", action="store_true")
    ap.add_argument("--profile", default="overhead")
    a = ap.parse_args(argv)
    cmd = [sys.executable, "-m", "mochi", "--profile", a.profile]
    cmd += [] if a.ui else ["--headless", "--seconds", str(a.seconds + 30)]
    print(json.dumps(measure(cmd, a.seconds), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
