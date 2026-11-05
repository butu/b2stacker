"""Dateien in den Papierkorb legen – oder, wo es keinen gibt (NAS, USB-Stick), ausdrücklich endgültig löschen.

Windows: Papierkorb nur auf lokalen Festplatten (``DRIVE_FIXED``); Netzlaufwerke und UNC-Pfade haben keinen,
Windows würde dort stillschweigend endgültig löschen. Deshalb entscheidet ``can_trash`` vorher, und das
endgültige Löschen braucht eine eigene Bestätigung.
Linux/macOS: Papierkorb nach freedesktop.org im Home-Verzeichnis (gleiches Laufwerk), sonst keiner.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import urllib.parse

DRIVE_FIXED = 3


def _win_drive_type(path: str) -> int:
    import ctypes
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))


def _home_trash() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "Trash")


def can_trash(path: str) -> bool:
    """Gibt es für diese Datei einen Papierkorb?"""
    try:
        if sys.platform == "win32":
            p = os.path.abspath(path)
            if p.startswith("\\\\"):
                return False
            return _win_drive_type(p) == DRIVE_FIXED
        home = os.path.expanduser("~")
        return os.stat(os.path.dirname(os.path.abspath(path)) or ".").st_dev == os.stat(home).st_dev
    except (OSError, AttributeError, ValueError):
        return False


def _win_recycle(path: str) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 16, 64, 1024
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=os.path.abspath(path) + "\0\0",
                         fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise OSError("Papierkorb hat die Datei nicht angenommen (Fehler %s)" % rc)


def _fd_trash(path: str) -> None:
    trash = _home_trash()
    files, info = os.path.join(trash, "files"), os.path.join(trash, "info")
    os.makedirs(files, exist_ok=True)
    os.makedirs(info, exist_ok=True)
    base = name = os.path.basename(path)
    n = 1
    while os.path.exists(os.path.join(files, name)) or os.path.exists(os.path.join(info, name + ".trashinfo")):
        stem, ext = os.path.splitext(base)
        name = "%s.%d%s" % (stem, n, ext)
        n += 1
    with open(os.path.join(info, name + ".trashinfo"), "w", encoding="utf-8") as fh:
        fh.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (
            urllib.parse.quote(os.path.abspath(path)), datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    shutil.move(path, os.path.join(files, name))


def remove(path: str, permanent: bool) -> str:
    """Datei löschen. → ``trash`` oder ``permanent``. Wirft OSError mit dem Grund."""
    if not os.path.isfile(path):
        raise FileNotFoundError("Datei nicht gefunden")
    if not permanent:
        if not can_trash(path):
            raise OSError("kein Papierkorb an diesem Ort")
        if sys.platform == "win32":
            _win_recycle(path)
        else:
            _fd_trash(path)
        return "trash"
    os.remove(path)
    return "permanent"
