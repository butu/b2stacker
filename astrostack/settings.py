"""Einstellungen (JSON). Ablage: %APPDATA%\\AstroStack\\settings.json bzw. ~/.config/astrostack/settings.json."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field


def _default_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AstroStack")
    return os.path.join(os.path.expanduser("~"), ".config", "astrostack")


def _default_pixinsight() -> str:
    for p in (r"C:\Program Files\PixInsight\bin\PixInsight.exe", "/opt/PixInsight/bin/PixInsight",
              "/Applications/PixInsight/PixInsight.app/Contents/MacOS/PixInsight"):
        if os.path.exists(p):
            return p
    return r"C:\Program Files\PixInsight\bin\PixInsight.exe"


@dataclass
class Settings:
    workdir: str = ""                          # Arbeitsverzeichnis (Zwischendateien, Masters, Berichte)
    preview_cache_dir: str = ""                # lokaler Vorschau-Cache; leer = OS-Standard
    pixinsight: str = field(default_factory=_default_pixinsight)
    sources: list[str] = field(default_factory=list)   # zuletzt verwendete Ordner
    custom_filters: dict[str, str] = field(default_factory=dict)
    timezone: str = "Europe/Vienna"
    analysis_workers: int = 2
    keep_awake: bool = True
    cleanup_intermediate: bool = True
    open_results: bool = True                  # Ergebnisse am Ende in PixInsight öffnen
    min_free_ram_gb: float = 8.0
    language: str = "de"
    # Ersatz-Standort, wenn die FITS-Daten keinen enthalten (Mondstand, Objekthöhe, Nachtgrenze)
    site_lat: float | None = None
    site_lon: float | None = None
    site_elev: float | None = None

    @property
    def wbpp_script(self) -> str:
        base = os.path.dirname(os.path.dirname(self.pixinsight))
        return os.path.join(base, "src", "scripts", "BatchPreprocessing", "WBPP.js")

    @staticmethod
    def path() -> str:
        return os.environ.get("ASTROSTACK_SETTINGS") or os.path.join(_default_dir(), "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        """Einstellungen laden. Eine kaputte Datei (Absturz beim Speichern, Handbearbeitung) darf den Start
        nie verhindern: sie wird als ``settings.json.kaputt-<zeit>`` gesichert, es gelten die Standardwerte."""
        p = cls.path()
        s = cls()
        problems = []
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("kein JSON-Objekt")
            except (OSError, ValueError) as e:
                data = {}
                problems.append("Einstellungsdatei unlesbar (%s)" % e)
                _quarantine(p)
            defaults = cls()
            for k, v in data.items():
                if not hasattr(s, k):
                    continue
                want = getattr(defaults, k)
                if v is None and k in _OPTIONAL:
                    setattr(s, k, None)
                elif _type_ok(want, v, k):
                    setattr(s, k, float(v) if isinstance(want, float) or k in _OPTIONAL else v)
                else:
                    problems.append("%s=%r ungültig, Standard %r verwendet" % (k, v, want))
        if not s.workdir:
            s.workdir = os.path.join(os.path.expanduser("~"), "AstroStack")
        for msg in problems:
            if msg not in LOAD_PROBLEMS:
                LOAD_PROBLEMS.append(msg)
        return s

    def save(self) -> str:
        p = self.path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        data = asdict(self)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, p)                      # atomar: nie eine halb geschriebene Datei
        return p


# Probleme beim Laden (für Oberfläche und Protokoll); gilt für den ganzen Prozess.
LOAD_PROBLEMS: list[str] = []
_OPTIONAL = ("site_lat", "site_lon", "site_elev")


def _type_ok(default, v, key) -> bool:
    if key in _OPTIONAL:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if isinstance(default, bool):
        return isinstance(v, bool)
    if isinstance(default, int):
        return isinstance(v, int) and not isinstance(v, bool)
    if isinstance(default, float):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, type(default))


def _quarantine(p: str) -> None:
    import time
    try:
        os.replace(p, p + ".kaputt-" + time.strftime("%Y%m%d-%H%M%S"))
    except OSError:
        pass
