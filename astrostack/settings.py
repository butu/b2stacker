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
        p = cls.path()
        s = cls()
        if os.path.exists(p):
            data = json.load(open(p, encoding="utf-8"))
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        if not s.workdir:
            s.workdir = os.path.join(os.path.expanduser("~"), "AstroStack")
        return s

    def save(self) -> str:
        p = self.path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump(asdict(self), open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        return p
