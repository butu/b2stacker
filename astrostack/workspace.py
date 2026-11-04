"""Arbeitsverzeichnis des Neubaus (Konzept Abschnitt 3).

```
<workdir>/
  projects/<projektname>/   Ergebnisse: ein Ordner je Stack, masters/, vom Nutzer abgelegte Dateien
  temp/                     ein gemeinsamer Temp-Ordner, vor und nach jedem Stack geleert
  system/                   b2stacker.db, backup/, previews/, logs/  – nicht von Hand bearbeiten
```

Ordnernamen sind englisch und klein. Projektordner tragen den Projektnamen, bereinigt für Windows.
"""
from __future__ import annotations

import os
import re
import shutil
import time

from . import db as dbm

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *("com%d" % i for i in range(1, 10)), *("lpt%d" % i for i in range(1, 10))}


def folder_name(name: str) -> str:
    """Projektname → gültiger Windows-Ordnername (ohne Pfadtrenner, ohne reservierte Namen, max. 80 Zeichen)."""
    n = _BAD.sub("_", (name or "").strip())
    n = re.sub(r"\s+", " ", n).strip(" .")[:80].strip(" .")
    if not n:
        n = "Projekt"
    if n.lower().split(".")[0] in _RESERVED:
        n = "_" + n
    return n


class Workspace:
    def __init__(self, workdir: str):
        self.root = os.path.abspath(workdir)
        self.projects = os.path.join(self.root, "projects")
        self.temp = os.path.join(self.root, "temp")
        self.system = os.path.join(self.root, "system")
        self.db_path = os.path.join(self.system, dbm.DB_NAME)
        self.backups = os.path.join(self.system, "backup")
        self.previews = os.path.join(self.system, "previews")
        self.logs = os.path.join(self.system, "logs")

    def ensure(self) -> "Workspace":
        for d in (self.projects, self.temp, self.system, self.backups, self.previews, self.logs):
            os.makedirs(d, exist_ok=True)
        return self

    def open_db(self) -> dbm.Database:
        self.ensure()
        return dbm.open_database(self.db_path, backup_dir=self.backups)

    def project_dir(self, folder: str) -> str:
        return os.path.join(self.projects, folder)

    def unique_folder(self, name: str, taken: set[str] | None = None) -> str:
        """Freier Ordnername für ein Projekt (Groß-/Kleinschreibung zählt nicht, wie unter Windows)."""
        base = folder_name(name)
        taken = {t.lower() for t in (taken or set())}
        try:
            taken |= {e.lower() for e in os.listdir(self.projects)}
        except OSError:
            pass
        cand, n = base, 2
        while cand.lower() in taken:
            cand = "%s (%d)" % (base, n)
            n += 1
        return cand

    def rename_folder(self, old: str, new: str, attempts: int = 3, wait_s: float = 0.5) -> None:
        """Projektordner umbenennen. Ist eine Datei darin geöffnet (Windows sperrt dann), mehrfach versuchen;
        scheitert es weiter, kommt ``PermissionError`` mit Klartext."""
        src, dst = self.project_dir(old), self.project_dir(new)
        if not os.path.exists(src):
            os.makedirs(dst, exist_ok=True)
            return
        if os.path.normcase(src) == os.path.normcase(dst):
            if src != dst:                                   # nur Groß-/Kleinschreibung: über Zwischennamen
                tmp = dst + ".umbenennen"
                os.rename(src, tmp)
                os.rename(tmp, dst)
            return
        last = None
        for i in range(attempts):
            try:
                os.rename(src, dst)
                return
            except PermissionError as e:
                last = e
                time.sleep(wait_s * (i + 1))
        raise PermissionError("Der Projektordner kann gerade nicht umbenannt werden – vermutlich ist eine Datei darin "
                              "geöffnet. Bitte schließen und erneut versuchen. (%s)" % last)

    def clear_temp(self) -> int:
        """Inhalt von temp/ löschen (vor und nach jedem Stack). → Anzahl entfernter Einträge"""
        n = 0
        try:
            entries = os.listdir(self.temp)
        except OSError:
            return 0
        for e in entries:
            p = os.path.join(self.temp, e)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                n += 1
            except OSError:
                pass
        return n

    def usage(self, folder: str | None = None) -> dict:
        """Belegter Platz in Bytes: temp/, Vorschauen und (wenn angegeben) ein Projektordner."""
        out = dict(temp=_du(self.temp), previews=_du(self.previews))
        if folder:
            out["project"] = _du(self.project_dir(folder))
        return out


def _du(path: str) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total
