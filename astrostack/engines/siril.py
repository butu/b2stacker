"""Siril als Stacking-Engine: Erkennung der siril-cli (Linux, Windows, macOS, Flatpak).

Phase 1: nur Erkennung und Prüfung. Der Lauf selbst (.ssf-Skript) folgt in Phase 2.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from ..system import _decode
from .pixinsight import Status, StackError

MIN_VERSION = (1, 2, 0)        # siril-cli mit Skriptmodus (-s) gibt es seit 1.2
FLATPAK_ID = "org.siril.Siril"
FLATPAK_LABEL = "flatpak:" + FLATPAK_ID
FLATPAK_DIRS = ("/var/lib/flatpak", os.path.expanduser("~/.local/share/flatpak"))
_NO_WINDOW = 0x08000000
_VERSION_CACHE: dict[tuple, str] = {}       # nur erfolgreiche Abfragen; Fehlschläge werden erneut geprüft

if sys.platform == "win32":
    SEARCH_PATHS = [r"C:\Program Files\Siril\bin\siril-cli.exe"]
elif sys.platform == "darwin":
    SEARCH_PATHS = ["/Applications/Siril.app/Contents/MacOS/siril-cli"]
else:
    SEARCH_PATHS = ["/usr/bin/siril-cli", "/usr/local/bin/siril-cli"]


@dataclass(frozen=True)
class Install:
    cli: tuple            # Aufruf-Präfix für die Kommandozeile (Flatpak = mehrere Wörter)
    gui: tuple | None     # Aufruf zum Öffnen von Ergebnissen in der Siril-Oberfläche
    label: str            # Anzeige und Wert für die Einstellung

    def cli_cmd(self, *args: str) -> list[str]:
        return list(self.cli) + list(args)


def _flatpak_install() -> Install | None:
    if not sys.platform.startswith("linux") or shutil.which("flatpak") is None:
        return None
    if not any(os.path.isdir(os.path.join(d, "app", FLATPAK_ID)) for d in FLATPAK_DIRS):
        return None
    return Install(cli=("flatpak", "run", "--command=siril-cli", FLATPAK_ID),
                   gui=("flatpak", "run", FLATPAK_ID), label=FLATPAK_LABEL)


def _from_binary(path: str) -> Install | None:
    """Pfad zu siril-cli – oder zum Siril-Programm, dann liegt die CLI daneben."""
    base = os.path.basename(path).lower()
    d = os.path.dirname(path)
    if base.startswith("siril") and not base.startswith("siril-cli"):
        cli = os.path.join(d, "siril-cli" + (".exe" if base.endswith(".exe") else ""))
        if not os.path.isfile(cli):
            return None
        return Install(cli=(cli,), gui=(path,), label=cli)
    if not os.path.isfile(path):
        return None
    gui = os.path.join(d, base.replace("siril-cli", "siril", 1))
    return Install(cli=(path,), gui=(gui,) if os.path.isfile(gui) else None, label=path)


def locate(setting: str = "") -> Install | None:
    """Einstellung auswerten; leer = automatische Erkennung. None = nicht gefunden."""
    v = (setting or "").strip().strip('"')
    if v == FLATPAK_LABEL:
        return _flatpak_install()
    if not v:
        found = shutil.which("siril-cli")
        if found:
            return _from_binary(found)
        for p in SEARCH_PATHS:
            if os.path.isfile(p):
                return _from_binary(p)
        return _flatpak_install()
    if os.path.isdir(v):
        for cand in (os.path.join(v, "siril-cli"), os.path.join(v, "bin", "siril-cli"),
                     os.path.join(v, "siril-cli.exe"), os.path.join(v, "bin", "siril-cli.exe")):
            if os.path.isfile(cand):
                return _from_binary(cand)
        return None
    return _from_binary(v)


def _parse(version: str) -> tuple:
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def version_of(inst: Install) -> str | None:
    """`siril-cli --version` → „1.4.4“. Timeout, damit eine hängende Installation die Oberfläche nicht blockiert."""
    key = inst.cli
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    try:
        out = subprocess.run(list(inst.cli) + ["--version"], capture_output=True, timeout=20,
                             creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+(?:\.\d+)?", _decode(out.stdout + out.stderr))
    if not m:
        return None
    _VERSION_CACHE[key] = m.group(0)
    return m.group(0)


def check(setting: str = "") -> dict:
    """Ist eine passende Siril-Installation da? → Felder für Oberfläche und Blocker-Meldung."""
    v = (setting or "").strip()
    inst = locate(v)
    if inst is None:
        msg = ("Pfad nicht gefunden – siril-cli prüfen oder „flatpak:%s“ eintragen." % FLATPAK_ID) if v else \
            "Siril nicht gefunden – siril-cli im PATH oder im Standardordner nötig (Flatpak: %s)." % FLATPAK_ID
        return dict(path=v, ok=False, found=False, version=None, cmd=None, message=msg)
    ver = version_of(inst)
    if ver is None:
        return dict(path=v, ok=False, found=True, version=None, cmd=inst.label,
                    message="Siril gefunden, aber „siril-cli --version“ antwortet nicht – Installation prüfen.")
    if _parse(ver) < MIN_VERSION:
        return dict(path=v, ok=False, found=True, version=ver, cmd=inst.label,
                    message="Siril %s ist zu alt – mindestens %s nötig." % (ver, ".".join(map(str, MIN_VERSION))))
    return dict(path=v, ok=True, found=True, version=ver, cmd=inst.label, message="Siril %s gefunden." % ver)


def run_plan(plan: dict, settings, **_kw) -> Status:
    """Phase-1-Stub: prüft die Installation und meldet, dass der Siril-Lauf noch fehlt."""
    c = check(settings.siril)
    if not c["ok"]:
        raise StackError(c["message"])
    raise StackError("Stacking mit Siril ist noch nicht implementiert – bitte vorerst PixInsight wählen.")
