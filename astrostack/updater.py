"""Selbst-Aktualisierung der exe über GitHub-Releases.

Ablauf: Oben rechts erscheint „Update x.y.z“, sobald auf GitHub eine neuere Version liegt (Prüfung beim Start und
alle 6 h). Klick → neue exe wird neben die laufende geladen und geprüft → kleines Hilfsskript wartet, bis
b2stacker beendet ist, tauscht die Datei aus und startet die neue Version. Die alte bleibt als
b2stacker.old.exe liegen, bis die neue erfolgreich läuft (Rückfall bei Fehlern).
Läuft nicht während eines Stackings und nicht aus dem Quellcode (dort: git pull).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import __version__, logbook

REPO = "butu/b2stacker"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
ASSET = "b2stacker.exe"
CHECK_EVERY_S = 6 * 3600

_STATE = {"checked": 0.0, "info": None, "dl": None}
_LOCK = threading.Lock()


def _ver(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV")
    out = []
    for p in v.split("."):
        num = "".join(c for c in p if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def is_newer(latest: str, current: str = __version__) -> bool:
    return _ver(latest) > _ver(current)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_path() -> str | None:
    return os.path.abspath(sys.executable) if frozen() else None


def _get_json(url: str, timeout=10) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "b2stacker/" + __version__})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check(force: bool = False, fetch=_get_json) -> dict:
    """Neueste Version auf GitHub (zwischengespeichert). Fehler → newer=False mit error."""
    with _LOCK:
        if not force and _STATE["info"] and time.time() - _STATE["checked"] < CHECK_EVERY_S:
            return dict(_STATE["info"], download=_STATE["dl"])
    info = {"current": __version__, "latest": None, "newer": False, "can_install": frozen()}
    try:
        rel = fetch(API)
        tag = rel.get("tag_name") or ""
        asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
        info.update(latest=tag.lstrip("vV"), notes=(rel.get("body") or "")[:3000], page=rel.get("html_url"),
                    published=rel.get("published_at"), url=asset and asset.get("browser_download_url"),
                    size=asset and asset.get("size"))
        info["newer"] = bool(asset) and is_newer(tag)
    except Exception as e:  # noqa: BLE001 – offline o. Ä. ist kein Fehler fürs Programm
        info["error"] = str(e)[:200]
    with _LOCK:
        _STATE.update(checked=time.time(), info=info)
        return dict(info, download=_STATE["dl"])


def status() -> dict:
    """Für die Oberfläche; prüft im Hintergrund neu, wenn die letzte Prüfung zu alt ist."""
    with _LOCK:
        stale = time.time() - _STATE["checked"] > CHECK_EVERY_S
        out = dict(_STATE["info"] or {"current": __version__, "newer": False, "can_install": frozen()},
                   download=_STATE["dl"], updated_from=_STATE.get("updated_from"))
    if stale and not _STATE.get("checking"):
        _STATE["checking"] = True

        def bg():
            try:
                check(force=True)
            finally:
                _STATE["checking"] = False
        threading.Thread(target=bg, daemon=True, name="update-check").start()
    return out


# ------------------------------------------------------------ Installation

SWAP_CMD = r"""@echo off
setlocal
set "EXE={exe}"
set "NEW={new}"
set "OLD={old}"
set N=0
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 goto again
tasklist /FI "PID eq {ppid}" 2>nul | find "{ppid}" >nul
if not errorlevel 1 goto again
goto swap
:again
set /a N+=1
if %N% geq 120 goto fail
ping -n 2 127.0.0.1 >nul
goto wait
:swap
ping -n 2 127.0.0.1 >nul
if exist "%OLD%" del /f /q "%OLD%" >nul 2>&1
move /y "%EXE%" "%OLD%" >nul 2>&1
if errorlevel 1 goto fail
move /y "%NEW%" "%EXE%" >nul 2>&1
if errorlevel 1 (
  move /y "%OLD%" "%EXE%" >nul 2>&1
  goto fail
)
set "B2_UPDATED_FROM={cur}"
start "" "%EXE%"
goto end
:fail
start "" "%EXE%"
:end
del "%~f0"
"""


def _onefile() -> bool:
    mei = getattr(sys, "_MEIPASS", None)
    return bool(mei) and os.path.normcase(os.path.abspath(mei)) != os.path.normcase(os.path.dirname(exe_path() or ""))


def _write_cmd(path: str, text: str):
    """cmd.exe liest Batch-Dateien in der OEM-Codepage (DE: 850) – Umlaute in Pfaden passend kodieren."""
    if sys.platform == "win32":
        try:
            data = text.replace("\n", "\r\n").encode("oem")
        except (UnicodeEncodeError, LookupError):
            data = ("@chcp 65001 >nul\r\n" + text.replace("\n", "\r\n")).encode("utf-8")
    else:
        data = text.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)


def _download(url: str, dst: str, size: int | None, progress) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "b2stacker/" + __version__})
    tmp = dst + ".part"
    got = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        total = size or int(r.headers.get("Content-Length") or 0)
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            fh.write(b)
            got += len(b)
            progress(got, total)
    with open(tmp, "rb") as fh:
        if fh.read(2) != b"MZ":
            os.remove(tmp)
            raise RuntimeError("Download ist keine Windows-Programmdatei.")
    if size and got != size:
        os.remove(tmp)
        raise RuntimeError("Download unvollständig (%d von %d Bytes)." % (got, size))
    os.replace(tmp, dst)


def start_install(quit_fn, busy_fn=lambda: None, downloader=_download) -> dict:
    """Neue Version laden und Austausch anstoßen. quit_fn() beendet b2stacker danach."""
    exe = exe_path()
    if not exe:
        raise RuntimeError("Aus dem Quellcode gestartet – bitte mit „git pull“ aktualisieren.")
    if busy_fn():
        raise RuntimeError("Es läuft gerade ein Auftrag. Bitte warten, bis er fertig ist.")
    info = check()
    if not info.get("newer") or not info.get("url"):
        raise RuntimeError("Keine neuere Version gefunden.")
    folder = os.path.dirname(exe)
    if not os.access(folder, os.W_OK):
        raise RuntimeError("Keine Schreibrechte in %s – b2stacker.exe bitte in einen eigenen Ordner legen." % folder)
    with _LOCK:
        if _STATE["dl"] and _STATE["dl"].get("state") in ("download", "restart"):
            return dict(_STATE["dl"])
        _STATE["dl"] = {"state": "download", "done": 0, "total": info.get("size") or 0, "version": info["latest"]}

    base = os.path.splitext(exe)[0]
    new, old = base + ".new.exe", base + ".old.exe"

    def prog(d, t):
        with _LOCK:
            _STATE["dl"].update(done=d, total=t)

    def work():
        try:
            logbook.event("update_start", current=__version__, latest=info["latest"], exe=exe)
            downloader(info["url"], new, info.get("size"), prog)
            cmd = os.path.join(tempfile.gettempdir(), "b2stacker_update_%d.cmd" % os.getpid())
            # onefile: der Starter (Elternprozess) hält die exe offen – auf beide warten
            ppid = os.getppid() if _onefile() else os.getpid()
            text = SWAP_CMD.format(exe=exe, new=new, old=old, pid=os.getpid(), ppid=ppid, cur=__version__)
            _write_cmd(cmd, text)
            flags = 0x08000000 | 0x00000008 if sys.platform == "win32" else 0   # kein Fenster, losgelöst
            from .appclient import independent_env
            subprocess.Popen(["cmd.exe", "/c", cmd], creationflags=flags, close_fds=True,
                             cwd=tempfile.gettempdir(), env=independent_env())   # neue exe eigenständig
            with _LOCK:
                _STATE["dl"].update(state="restart")
            logbook.event("update_restart", latest=info["latest"])
            quit_fn()
        except Exception as e:  # noqa: BLE001
            with _LOCK:
                _STATE["dl"].update(state="error", error=str(e)[:300])
            logbook.error("Update fehlgeschlagen")
            try:
                os.remove(new)
            except OSError:
                pass

    threading.Thread(target=work, daemon=True, name="update").start()
    return dict(_STATE["dl"])


def cleanup_after_update():
    """Beim Start: Reste eines Updates entfernen (alte exe), erfolgreiches Update protokollieren."""
    exe = exe_path()
    if not exe:
        return
    frm = os.environ.pop("B2_UPDATED_FROM", None)
    if frm:
        logbook.event("update_done", previous=frm, current=__version__)
        with _LOCK:
            _STATE["updated_from"] = frm
    old = os.path.splitext(exe)[0] + ".old.exe"
    for _ in range(5):
        try:
            if os.path.exists(old):
                os.remove(old)
            return
        except OSError:
            time.sleep(1)
