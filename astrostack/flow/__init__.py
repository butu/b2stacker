"""Automatischer Ablauf (Konzept Abschnitt 5): alles automatisch außer Stacken.

Pro Datei, einmal, im Hintergrund:
  1. Entdecken (``discover``)  – Pfad, Größe, Datum, ohne die Datei zu öffnen
  2. Header lesen (``headers``) – Typ, Filter, Kamera, Gain, Belichtung …
  3. Vermessen (``measure``)    – Datei genau einmal lesen: Prüfsumme, Messwerte, Thumbnail
Pro Projekt, aus der Datenbank:
  4. Zuordnen & bewerten, 5. Plan aktualisieren (folgen)

Jeder Schritt speichert seine Versionsnummer; ändert sich ein Schritt, rechnet er nur die betroffenen Dateien
nach. ``run_project`` führt die Schritte für ein Projekt nacheinander aus.
"""
from __future__ import annotations

from .assess import ASSESS_VERSION, assess_project, decide
from .discover import discover_project
from .headers import HEADER_VERSION, read_headers
from .measure import MEASURE_VERSION, measure_files, prepare_fast

__all__ = ["ASSESS_VERSION", "HEADER_VERSION", "MEASURE_VERSION", "assess_project", "decide", "discover_project",
           "measure_files", "prepare_fast", "read_headers", "run_project"]


def run_project(db, ws, pid: int, progress=None, cancel=None, workers: int = 2) -> dict:
    """Schritte 1–4 für ein Projekt. → Zusammenfassung je Schritt"""
    out = {"discover": discover_project(db, pid, progress=progress, cancel=cancel)}
    out["headers"] = read_headers(db, pid, progress=progress, cancel=cancel)
    out["measure"] = measure_files(db, ws, pid, progress=progress, cancel=cancel, workers=workers)
    out["assess"] = assess_project(db, pid, progress=progress)
    return out
