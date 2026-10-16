"""Group processes into the apps people recognise (COL-5, critique problem 7).

- All ``chrome.exe`` processes become one "chrome.exe" app; memory is the sum of
  private bytes, which doesn't double-count shared pages.
- Helper processes (WebView2, conhost) are attached to the app that started them.
- Interpreters are split by script, so ``python.exe (memory_hog.py)`` and Mochi
  itself never get mixed up (problem 8).
- Mochi and anything it starts (benchmarks) are tagged as "self" (COL-7).
"""

from __future__ import annotations

import os
from typing import Callable

from .model import App, Proc

INTERPRETERS = {
    "python.exe", "pythonw.exe", "py.exe", "python3.exe", "node.exe", "java.exe", "javaw.exe",
    "powershell.exe", "pwsh.exe", "ruby.exe", "perl.exe", "dotnet.exe", "wscript.exe", "cscript.exe",
    "python", "python3", "node", "java", "ruby", "perl",
}
SCRIPT_EXTS = (".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".jar", ".ps1", ".rb", ".pl", ".dll")
HELPERS = {"msedgewebview2.exe", "conhost.exe", "crashpad_handler.exe"}
PSEUDO_NAMES = {"system idle process", "idle"}

SYSTEM_NAMES = {
    "system", "registry", "memory compression", "secure system", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe",
    "spoolsv.exe", "sihost.exe", "ctfmon.exe", "audiodg.exe", "searchindexer.exe", "searchhost.exe",
    "searchprotocolhost.exe", "searchfilterhost.exe", "msmpeng.exe", "mpdefendercoreservice.exe", "nissrv.exe",
    "tiworker.exe", "trustedinstaller.exe", "mousocoreworker.exe", "usoclient.exe", "wuauclt.exe",
    "compattelrunner.exe", "defrag.exe", "mpcmdrun.exe", "taskhostw.exe", "runtimebroker.exe", "wmiprvse.exe",
    "dllhost.exe", "explorer.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "securityhealthservice.exe", "sgrmbroker.exe", "lockapp.exe", "logonui.exe", "smartscreen.exe",
    "mscorsvw.exe", "ngen.exe", "dismhost.exe", "sppsvc.exe", "wlanext.exe", "unsecapp.exe",
}

# Windows deliberately does this work while you're idle (critique problem 10, DIA-10).
KNOWN_MAINTENANCE = {
    "msmpeng.exe": "Microsoft Defender is scanning",
    "mpcmdrun.exe": "Microsoft Defender is scanning",
    "mpdefendercoreservice.exe": "Microsoft Defender is scanning",
    "tiworker.exe": "Windows Update is installing updates",
    "trustedinstaller.exe": "Windows Update is installing updates",
    "mousocoreworker.exe": "Windows Update is checking for updates",
    "usoclient.exe": "Windows Update is checking for updates",
    "wuauclt.exe": "Windows Update is checking for updates",
    "searchindexer.exe": "Windows Search is indexing your files",
    "searchprotocolhost.exe": "Windows Search is indexing your files",
    "searchfilterhost.exe": "Windows Search is indexing your files",
    "compattelrunner.exe": "Windows is running a compatibility check",
    "defrag.exe": "Windows is optimising your drive",
    "mscorsvw.exe": ".NET is optimising installed apps",
    "ngen.exe": ".NET is optimising installed apps",
    "dismhost.exe": "Windows is servicing system components",
    "sppsvc.exe": "Windows is checking its licence",
}

# Work happening inside a virtual machine Mochi can't see into (DIA-13).
VM_HOSTS = {
    "vmmem": "WSL or a virtual machine",
    "vmmemwsl": "WSL (Windows Subsystem for Linux)",
    "vmwp.exe": "a Hyper-V virtual machine",
    "com.docker.backend.exe": "Docker",
    "docker desktop.exe": "Docker",
    "vboxheadless.exe": "a VirtualBox virtual machine",
    "virtualboxvm.exe": "a VirtualBox virtual machine",
    "vmware-vmx.exe": "a VMware virtual machine",
}

SELF_KEY = "mochi"


def is_system_name(name: str) -> bool:
    return name.lower() in SYSTEM_NAMES


def script_from_cmdline(cmd: list[str] | None) -> str | None:
    """Return the script or module an interpreter is running (basename only, SEC-3)."""
    if not cmd:
        return None
    args = cmd[1:]
    for i, arg in enumerate(args):
        if arg in ("-m", "-c"):
            if arg == "-m" and i + 1 < len(args):
                return args[i + 1]
            return None
        if arg.startswith("-"):
            continue
        base = os.path.basename(arg.strip('"'))
        if base.lower().endswith(SCRIPT_EXTS):
            return base
    return None


class Grouper:
    def __init__(self, self_pid: int, cmdline: Callable[[int, float], list[str] | None]) -> None:
        self.self_pid = self_pid
        self._cmdline = cmdline
        self._scripts: dict[tuple[int, float], str | None] = {}

    def _script(self, p: Proc) -> str | None:
        key = p.ident
        if key not in self._scripts:
            if len(self._scripts) > 4096:
                self._scripts.clear()
            self._scripts[key] = script_from_cmdline(self._cmdline(p.pid, p.create_time))
        return self._scripts[key]

    @staticmethod
    def _parent(p: Proc, by_pid: dict[int, Proc]) -> Proc | None:
        parent = by_pid.get(p.ppid)
        # A parent must have started before its child; otherwise the PID was reused.
        if parent is None or parent.pid == p.pid or parent.create_time > p.create_time + 1.0:
            return None
        return parent

    def _is_self(self, p: Proc, by_pid: dict[int, Proc]) -> bool:
        cur: Proc | None = p
        for _ in range(12):
            if cur is None:
                return False
            if cur.pid == self.self_pid:
                return True
            cur = self._parent(cur, by_pid)
        return False

    def key_for(self, p: Proc, by_pid: dict[int, Proc]) -> tuple[str, str]:
        """Return (app key, display name)."""
        if self._is_self(p, by_pid):
            return SELF_KEY, "Mochi"
        name_l = p.name.lower()
        if name_l in HELPERS:
            cur = self._parent(p, by_pid)
            for _ in range(12):
                if cur is None:
                    break
                if cur.name.lower() not in HELPERS:
                    return self.key_for(cur, by_pid)
                cur = self._parent(cur, by_pid)
        if name_l in INTERPRETERS:
            script = self._script(p)
            if script:
                return f"{name_l}:{script.lower()}", f"{p.name} ({script})"
        return name_l, p.name

    def group(self, procs: list[Proc]) -> dict[str, App]:
        by_pid = {p.pid: p for p in procs}
        apps: dict[str, App] = {}
        members: dict[str, list[Proc]] = {}
        alive = set()
        for p in procs:
            if p.pid == 0 or p.name.lower() in PSEUDO_NAMES:
                continue
            alive.add(p.ident)
            key, display = self.key_for(p, by_pid)
            p.app = key
            app = apps.get(key)
            if app is None:
                app = apps[key] = App(
                    key=key, display=display, exe_name=p.name, pids=[], root_pid=p.pid,
                    root_create_time=p.create_time, ws_private=0,
                    is_system=is_system_name(p.name) or p.pid == 4, is_self=key == SELF_KEY,
                )
                members[key] = []
            members[key].append(p)
            app.pids.append(p.pid)
            app.proc_count += 1
            app.cpu_core += p.cpu_core
            app.cpu_total += p.cpu_total
            app.private_bytes += p.private_bytes or 0
            if p.ws_private is None or app.ws_private is None:
                app.ws_private = None
            else:
                app.ws_private += p.ws_private
            app.io_bps += p.io_bps or 0.0
            app.hard_faults_ps += p.hard_faults_ps or 0.0
        for key, plist in members.items():
            app = apps[key]
            roots = [p for p in plist if (par := self._parent(p, by_pid)) is None or par.app != key]
            root = min(roots or plist, key=lambda q: q.create_time)
            app.root_pid, app.root_create_time = root.pid, root.create_time
            if key != SELF_KEY:
                app.exe_name = root.name
        # forget scripts of processes that have exited
        if len(self._scripts) > len(alive) * 2 + 64:
            self._scripts = {k: v for k, v in self._scripts.items() if k in alive}
        return apps
