"""Fester Installationsort für b2stacker unter Windows (ohne Admin-Rechte).

Bisher lief b2stacker direkt aus dem Download-Ordner (``b2stacker (13).exe``). Folgen: jede neue Version war eine
neue Datei, der Eintrag in der Claude-App zeigte auf eine alte Kopie, und Updates tauschten eine Datei, die man
nicht wiederfand. Jetzt:

- Ort: ``%LOCALAPPDATA%\\b2stacker\\b2stacker.exe`` (immer derselbe Name).
- Startet b2stacker von woanders, bietet es „Installieren“ an: kopiert sich dorthin, legt eine
  Startmenü-Verknüpfung an, biegt einen vorhandenen Claude-Eintrag auf die installierte Datei um und startet von
  dort neu. Die Download-Datei kann danach gelöscht werden.
- Der Updater tauscht immer genau diese eine Datei.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

APP = "b2stacker"


def install_dir() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(local, APP)


def install_exe() -> str:
    return os.path.join(install_dir(), APP + ".exe")


def shortcut_path() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", APP + ".lnk")


def current_exe() -> str | None:
    return sys.executable if getattr(sys, "frozen", False) else None


def is_installed(exe: str | None = None) -> bool:
    exe = exe or current_exe()
    return bool(exe) and os.path.normcase(os.path.abspath(exe)) == os.path.normcase(install_exe())


def status() -> dict:
    exe = current_exe()
    return dict(frozen=bool(exe), exe=exe, target=install_exe(), installed=is_installed(exe),
                target_exists=os.path.exists(install_exe()),
                offer=bool(exe) and sys.platform == "win32" and not is_installed(exe))


def _shortcut(target: str) -> bool:
    """Startmenü-Verknüpfung über die Windows-Shell (PowerShell, ohne Zusatzpakete)."""
    lnk = shortcut_path()
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';"
          "$s.WorkingDirectory='%s';$s.Description='b2stacker – Astro-Aufnahmen verwalten und stacken';$s.Save()"
          % (lnk.replace("'", "''"), target.replace("'", "''"), os.path.dirname(target).replace("'", "''")))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           timeout=30, creationflags=0x08000000 if sys.platform == "win32" else 0)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install(src: str | None = None, start=None, quit_fn=None) -> dict:
    """b2stacker an den festen Ort kopieren, Verknüpfung anlegen, Claude-Eintrag umbiegen, von dort starten.

    ``start(path)`` startet die installierte exe (Standard: losgelöst), ``quit_fn()`` beendet diese Instanz."""
    src = src or current_exe()
    if not src:
        raise RuntimeError("Aus dem Quellcode gestartet – Installation nur für die exe.")
    if is_installed(src):
        return dict(status(), changed=False, text="b2stacker ist bereits installiert.")
    dst = install_exe()
    os.makedirs(install_dir(), exist_ok=True)
    tmp = dst + ".neu"
    shutil.copy2(src, tmp)
    try:
        os.replace(tmp, dst)
    except PermissionError:
        os.remove(tmp)
        raise RuntimeError("Die installierte b2stacker.exe läuft gerade (oder wird von Claude benutzt). Bitte diese "
                           "Instanz beenden und erneut versuchen.") from None
    shortcut = _shortcut(dst)
    claude = None
    try:
        from . import claude_link
        st = claude_link.status()
        if st.get("installed"):                       # vorhandenen Eintrag auf die installierte Datei umbiegen
            claude = claude_link.install(command=dst)
    except Exception as e:  # noqa: BLE001 – Installation gilt trotzdem
        claude = dict(error=str(e))
    from . import logbook
    logbook.event("installed", source=src, target=dst, shortcut=shortcut, claude=bool(claude and not claude.get("error")))
    if start is None:
        from .appclient import independent_env
        start = lambda p: subprocess.Popen([p], close_fds=True, env=independent_env(),          # noqa: E731
                                           creationflags=0x00000008 | 0x00000200 if sys.platform == "win32" else 0)
    start(dst)
    if quit_fn:
        quit_fn()
    return dict(status(), changed=True, shortcut=shortcut, claude=claude,
                text="Installiert nach %s. b2stacker startet von dort neu; die Datei im Download-Ordner kann "
                     "gelöscht werden." % dst)
