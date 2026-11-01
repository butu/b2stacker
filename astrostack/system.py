"""Systemzustand: freier Arbeitsspeicher/Plattenplatz, Energiesparmodus, PixInsight-Prozesse."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys


def free_disk_gb(path: str) -> float | None:
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        return shutil.disk_usage(p or ".").free / 1024 ** 3
    except OSError:
        return None


def ram_gb() -> tuple[float | None, float | None]:
    """(frei, gesamt) in GiB."""
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / 1024 ** 3, st.ullTotalPhys / 1024 ** 3
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k] = float(v.split()[0]) / 1024 ** 2
        return info.get("MemAvailable"), info.get("MemTotal")
    except OSError:
        return None, None


class KeepAwake:
    """Verhindert den Energiesparmodus, solange der Block läuft (Windows)."""

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        return False


def pixinsight_processes() -> list[dict]:
    """Laufende PixInsight-Prozesse (Windows: tasklist). → [{pid, mem_mb}]"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PixInsight.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 5 and parts[0].lower().startswith("pixinsight"):
            mem = parts[4].replace(".", "").replace(",", "").replace(" K", "").replace("K", "").strip()
            try:
                res.append(dict(pid=int(parts[1]), mem_mb=int(mem) / 1024))
            except ValueError:
                res.append(dict(pid=parts[1], mem_mb=None))
    return res


def estimate_disk_gb(n_frames: int, width: int, height: int, osc: bool, mode: str) -> float:
    """Zwischendateien eines WBPP-Laufs (kalibriert + debayert/extrahiert + registriert, Float32)."""
    px = width * height
    cal = px * 4
    if mode == "debayer":
        per = cal + 2 * px * 3 * 4
    elif mode == "extract":
        per = cal + 2 * (px / 4) * 4 * 2
    else:
        per = cal + px * 4
    return n_frames * per / 1024 ** 3 * 1.05 + 3


def estimate_ram_gb(n_frames: int, width: int, height: int, channels: int) -> float:
    """Grobe Schätzung für ImageIntegration (Puffer + Rejection) – lieber großzügig."""
    return 4 + n_frames * channels * width * height * 4 / 1024 ** 3 * 0.25
