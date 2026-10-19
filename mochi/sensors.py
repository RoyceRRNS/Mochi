"""CPU temperature, GPU temperature and fan speeds from vendor sensors.

Where each reading comes from (all local; nothing leaves the PC):
- NVIDIA GPUs: the driver's NVML library, no admin needed (platform/windows.py).
- LibreHardwareMonitor, when the person runs it: its WMI namespace is readable without admin.
- Lenovo laptops: the firmware's LENOVO_GAMEZONE_DATA WMI methods. Windows only lets
  administrators call them, so an optional helper process runs elevated (Windows asks the
  person first) and writes its readings to a small JSON file that Mochi reads. The helper
  only reads sensors, takes no commands, and exits when Mochi does.
- Windows' ACPI thermal zones often report a fixed placeholder (27.9 °C on many laptops),
  so a zone reading below 30 °C is never shown as the CPU temperature.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .model import Fan, SystemRaw

log = logging.getLogger(__name__)

LENOVO = "Lenovo sensors"
LHM = "LibreHardwareMonitor"
ACPI = "ACPI thermal zone"
FEED_NAME = "sensors.json"
STOP_NAME = "sensors.stop"
MAX_FANS = 8


class SensorError(Exception):
    pass


@dataclass
class SensorReading:
    ts: float
    source: str
    cpu_temp: float | None = None
    gpu_temp: float | None = None
    fans: list[Fan] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.cpu_temp is None and self.gpu_temp is None and not self.fans

    def to_json(self) -> str:
        return json.dumps({"ts": self.ts, "source": self.source, "cpu_temp": self.cpu_temp, "gpu_temp": self.gpu_temp,
                           "fans": [{"label": f.label, "rpm": f.rpm, "max_rpm": f.max_rpm} for f in self.fans]})

    @classmethod
    def from_json(cls, text: str) -> "SensorReading":
        """Parse a feed file. Anything malformed or implausible raises ValueError."""
        d = json.loads(text)
        if not isinstance(d, dict):
            raise ValueError("not an object")
        ts = _num(d.get("ts"), 0, 1e11)
        if ts is None:
            raise ValueError("missing timestamp")
        source = d.get("source")
        if not isinstance(source, str) or not 0 < len(source) <= 40:
            raise ValueError("bad source")
        fans_in = d.get("fans") or []
        if not isinstance(fans_in, list) or len(fans_in) > MAX_FANS:
            raise ValueError("bad fans")
        fans = []
        for f in fans_in:
            if not isinstance(f, dict) or not isinstance(f.get("label"), str) or not 0 < len(f["label"]) <= 40:
                raise ValueError("bad fan")
            rpm = _num(f.get("rpm"), 0, 20000)
            if rpm is None:
                raise ValueError("bad fan speed")
            fans.append(Fan(f["label"], rpm, _num(f.get("max_rpm"), 500, 20000)))
        return cls(ts, source, plausible_temp(d.get("cpu_temp")), plausible_temp(d.get("gpu_temp")), fans)


def _num(v, lo: float, hi: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
    return float(v)


def plausible_temp(v) -> float | None:
    """0 °C or 255 °C mean "no sensor" on most firmware; real chips run 1-125 °C."""
    return _num(v, 1.0, 125.0)


def merge(raw: SystemRaw, r: SensorReading | None) -> None:
    """Fill readings the platform couldn't get. A vendor sensor beats an ACPI zone."""
    if r is None:
        return
    if r.cpu_temp is not None and (raw.temp_c is None or raw.sources.get("temp") == ACPI):
        raw.temp_c = r.cpu_temp
        raw.sources["temp"] = r.source
        raw.unavailable.pop("temp", None)
    if r.gpu_temp is not None and raw.gpu_temp_c is None:
        raw.gpu_temp_c = r.gpu_temp
        raw.sources["gpu_temp"] = r.source
        raw.unavailable.pop("gpu_temp", None)
    if r.fans and LHM not in raw.sources.get("fan", ""):     # LibreHardwareMonitor already lists every fan
        have = {f.label for f in raw.fans}
        extra = [Fan(f.label, f.rpm, f.max_rpm) for f in r.fans if f.label not in have]
        if extra:
            raw.sources["fan"] = f"{raw.sources.get('fan', 'this PC')} + {r.source}" if raw.fans else r.source
            raw.fans = raw.fans + extra
            raw.unavailable.pop("fan", None)


class SensorFeed:
    """Reads the helper's JSON file. Stale or malformed files count as no reading."""

    def __init__(self, path: Path, max_age_s: float = 20.0) -> None:
        self.path = Path(path)
        self.max_age_s = max_age_s
        self.errors = 0

    def read(self, now: float) -> SensorReading | None:
        # Read every time (a ~200-byte file, once per sample): a file rewritten within one
        # timestamp tick keeps its mtime and size, so caching on those could serve old data.
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            r = SensorReading.from_json(text)
        except ValueError:
            self.errors += 1
            return None
        if abs(now - r.ts) > self.max_age_s:
            return None
        return r


# ----------------------------------------------------------------- readers (Windows)
def _wmi(namespace: str):
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    return win32com.client.GetObject(f"winmgmts:\\\\.\\{namespace}")


def _out_value(out) -> float | None:
    """The number a GameZone method returns (its "Data" out-parameter)."""
    best = None
    for prop in out.Properties_:
        if prop.Name == "ReturnValue":
            continue
        v = prop.Value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if prop.Name == "Data":
            return float(v)
        best = float(v) if best is None else best
    return best


class LenovoGameZone:
    """Lenovo Legion/LOQ firmware sensors. Needs admin; raises SensorError otherwise."""

    def __init__(self, wmi=None) -> None:
        try:
            svc = wmi or _wmi(r"root\WMI")
            objs = list(svc.ExecQuery("SELECT * FROM LENOVO_GAMEZONE_DATA"))
        except Exception as exc:
            raise SensorError(f"Lenovo sensors not readable ({exc.__class__.__name__})") from exc
        if not objs:
            raise SensorError("no Lenovo sensors on this PC")
        self.obj = objs[0]
        self.max_rpm = _num(self._call("GetFanMaxSpeed"), 1000, 20000)
        self.fan_methods = [m for m in ("GetFan1Speed", "GetFan2Speed") if self._call(m) is not None]

    def _call(self, method: str) -> float | None:
        try:
            return _out_value(self.obj.ExecMethod_(method))
        except Exception:
            return None

    def read(self) -> SensorReading:
        fans = []
        for i, m in enumerate(self.fan_methods, start=1):
            rpm = _num(self._call(m), 0, 20000)
            if rpm is not None:
                fans.append(Fan(f"Fan {i}", rpm, self.max_rpm))
        return SensorReading(time.time(), LENOVO, plausible_temp(self._call("GetCPUTemp")),
                             plausible_temp(self._call("GetGPUTemp")), fans)


def read_lhm(wmi=None) -> SensorReading | None:
    """LibreHardwareMonitor's WMI sensors, if it's running. None otherwise."""
    try:
        svc = wmi or _wmi(r"root\LibreHardwareMonitor")
        rows = list(svc.ExecQuery("SELECT Name, Value, SensorType, Identifier FROM Sensor"))
    except Exception:
        return None
    cpu: dict[str, float] = {}
    gpu: dict[str, float] = {}
    fans: list[Fan] = []
    for r in rows:
        kind, name, ident = str(r.SensorType), str(r.Name), str(r.Identifier).lower()
        if kind == "Temperature":
            t = plausible_temp(r.Value)
            if t is None:
                continue
            if "cpu" in ident:
                cpu[name] = t
            elif "gpu" in ident:
                gpu[name] = t
        elif kind == "Fan" and len(fans) < MAX_FANS:
            rpm = _num(r.Value, 0, 20000)
            if rpm is not None:
                fans.append(Fan(name[:40] or f"Fan {len(fans) + 1}", rpm))
    cpu_t = next((cpu[k] for k in ("CPU Package", "Core Max", "Core (Tctl/Tdie)") if k in cpu), max(cpu.values(), default=None))
    gpu_t = gpu.get("GPU Core", max(gpu.values(), default=None))
    reading = SensorReading(time.time(), LHM, cpu_t, gpu_t, fans)
    return None if reading.empty else reading


# --------------------------------------------------------------------- the helper
def helper_command(feed: Path, stop: Path, parent_pid: int, interval: float) -> tuple[str, list[str]]:
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    exe = windowless if windowless.exists() else exe
    args = ["-m", "mochi.sensor_helper", "--out", str(feed), "--stop", str(stop), "--parent", str(parent_pid),
            "--interval", f"{interval:g}"]
    return str(exe), args


def _quote(arg: str) -> str:
    return subprocess.list2cmdline([arg])


def shell_runas(exe: str, args: list[str], cwd: str) -> int:
    """Start ``exe`` elevated. Windows shows its own consent prompt. Returns > 32 on success."""
    import ctypes

    shell32 = ctypes.WinDLL("shell32")
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    params = " ".join(_quote(a) for a in args)
    return int(shell32.ShellExecuteW(None, "runas", exe, params, cwd, 0) or 0)


class SensorHelper:
    """Starts, watches and stops the optional elevated sensor helper."""

    def __init__(self, root: Path, is_admin: Callable[[], bool], interval: float = 3.0,
                 runas: Callable[[str, list[str], str], int] = shell_runas,
                 spawn: Callable[..., object] = subprocess.Popen) -> None:
        self.feed_path = Path(root) / FEED_NAME
        self.stop_path = Path(root) / STOP_NAME
        self.is_admin = is_admin
        self.interval = interval
        self.runas = runas
        self.spawn = spawn
        self.state = "off"          # off | asking | running | declined | failed
        self.message = ""
        self.started = 0.0
        self._thread: threading.Thread | None = None

    def age(self) -> float:
        """Seconds since the helper started (0 if it isn't running)."""
        return time.monotonic() - self.started if self.state == "running" else 0.0

    def start(self, parent_pid: int, wait: bool = False) -> None:
        """Launch the helper. Without admin, Windows asks first, so this runs off the caller's thread."""
        if self.state in ("asking", "running"):
            return
        try:
            self.stop_path.unlink()
        except OSError:
            pass
        self.state, self.message = "asking", "Waiting for Windows to ask for permission…"
        self._thread = threading.Thread(target=self._launch, args=(parent_pid,), name="mochi-sensor-launch",
                                        daemon=True)
        self._thread.start()
        if wait:
            self._thread.join(30.0)

    def _launch(self, parent_pid: int) -> None:
        exe, args = helper_command(self.feed_path, self.stop_path, parent_pid, self.interval)
        cwd = str(Path(__file__).resolve().parent.parent)
        try:
            if self.is_admin():
                self.spawn([exe, *args], cwd=cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                ok = True
            else:
                rc = self.runas(exe, args, cwd)
                ok = rc > 32
                if not ok:
                    self.state = "declined" if rc == 5 else "failed"   # 5 = SE_ERR_ACCESSDENIED (said no)
                    self.message = ("Windows didn't give permission, so CPU temperature and fans stay off."
                                    if self.state == "declined" else f"Couldn't start the sensor helper (code {rc}).")
                    return
        except Exception as exc:
            log.exception("sensor helper launch failed")
            self.state, self.message = "failed", f"Couldn't start the sensor helper ({exc.__class__.__name__})."
            return
        if ok:
            self.started = time.monotonic()
            self.state, self.message = "running", "Reading CPU temperature and fans with admin permission."

    def stop(self) -> None:
        if self.state in ("running", "asking"):
            try:
                self.stop_path.write_text("stop", encoding="utf-8")
            except OSError:
                pass
        self.state, self.message = "off", ""
