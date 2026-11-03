"""Ablaufschritte mit Erklärung – gemeinsam für Oberfläche, CLI und Protokoll.

Jeder Schritt: key, Titel, Symbol (Name für die Oberfläche), was passiert, wozu es gut ist.
"""
from __future__ import annotations

import json
import os

ANALYZE = [
    dict(key="scan", title="Ordner durchsuchen", icon="folder",
         what="Alle FITS/XISF-Dateien werden gefunden und ihre Kopfdaten gelesen.",
         why="Daraus ergibt sich, welche Lights, Flats, Bias und Darks zusammengehören."),
    dict(key="flats", title="Flats prüfen", icon="sun",
         what="Jede Flat-Serie wird vermessen: Helligkeit je Farbkanal, Farbabfall zum Rand, Streuung.",
         why="Zu helle Flats erzeugen Farbringe, zu wenige bringen Rauschen – das soll vor dem Stacken auffallen."),
    dict(key="measure", title="Fotos vermessen", icon="star",
         what="Jedes Light wird auf Sternschärfe (FWHM), Sternform, Sternzahl, Hintergrund und Rauschen geprüft.",
         why="So werden Wolken, Unschärfe und Nachführfehler erkannt, bevor sie ins Ergebnis gelangen."),
    dict(key="report", title="Bericht & Plan", icon="doc",
         what="Bewertung, Rig-Diagnose und ein Stacking-Vorschlag mit Begründung werden erstellt.",
         why="Du entscheidest danach mit allen Infos, was gestackt wird."),
]

STACK = [
    dict(key="check", title="Vorbereiten", icon="check",
         what="Platz, Arbeitsspeicher, laufende PixInsight-Fenster und Kalibrierdateien werden geprüft.",
         why="Verhindert Abstürze mitten im Lauf."),
    dict(key="calibrate", title="Kalibrieren", icon="sliders",
         what="Bias wird abgezogen und durch das Master-Flat geteilt.",
         why="Entfernt Ausleserauschen-Muster, Vignettierung und Staubschatten."),
    dict(key="cosmetic", title="Hotpixel", icon="sparkle",
         what="Keine Darks vorhanden: PixInsight (CosmeticCorrection, Auto Detect) ersetzt einzelne heiße und kalte Pixel.",
         why="Sonst richtet PixInsight die Bilder an den Hotpixeln statt an den Sternen aus (Geistersterne), "
             "und ohne Dithering bleiben sie als farbige Punkte im Stack."),
    dict(key="debayer", title="Debayern", icon="grid",
         what="Aus dem Farbmosaik der Kamera wird ein Farbbild.",
         why="Nötig, damit die Farbkanäle gemeinsam ausgerichtet werden können."),
    dict(key="register", title="Bewerten & Ausrichten", icon="target",
         what="Jedes Foto wird bewertet (PSF-Signal) und Stern für Stern auf die Referenz gelegt.",
         why="Gute Fotos zählen mehr; alle Bilder liegen danach pixelgenau übereinander."),
    dict(key="integrate", title="Normalisieren & Stacken", icon="layers",
         what="Helligkeitsverläufe werden angeglichen, dann werden alle Fotos gewichtet gemittelt, "
              "Ausreißer (Satelliten, Flugzeuge) fallen heraus.",
         why="Hier entsteht das rauscharme Endergebnis."),
    dict(key="finalize", title="Farbsäume & Aufteilen", icon="palette",
         what="Rot und Blau werden auf Grün ausgerichtet; Dualband wird in Ha/OIII bzw. SII/OIII geteilt.",
         why="Atmosphärische Dispersion verschiebt die Farben – ohne Korrektur entstehen farbige Sternsäume."),
    dict(key="quality", title="Qualität messen", icon="gauge",
         what="Rauschen, Sternschärfe, Sternform und Verlauf des fertigen Stacks werden gemessen.",
         why="So siehst du, ob der neue Stack besser ist als der letzte."),
]

BY_KEY = {s["key"]: s for s in ANALYZE + STACK}

# Siril-Texte: nennen kein PixInsight/WBPP (Siril hat keine PixInsight-Fenster und keine CosmeticCorrection).
_SIRIL = {
    "check": dict(what="Platz, Arbeitsspeicher und Kalibrierdateien werden geprüft."),
    "cosmetic": dict(what="Keine Darks vorhanden: b2stacker entfernt einzelne heiße und kalte Pixel vor dem Stacken.",
                     why="Sonst richtet Siril die Bilder an den Hotpixeln statt an den Sternen aus (Geistersterne), "
                         "und ohne Dithering bleiben sie als farbige Punkte im Stack."),
}


def stack_for(engine: str) -> list[dict]:
    """Schrittliste für die Oberfläche je Engine. PixInsight-Texte bleiben unverändert."""
    if engine != "siril":
        return STACK
    return [dict(s, **_SIRIL.get(s["key"], {})) for s in STACK]


def stack_steps(mode: str, osc: bool = True, cosmetic: str | bool = False) -> list[str]:
    """cosmetic: "after" = PixInsight CosmeticCorrection nach dem Kalibrieren, "before"/True = eigene Bereinigung vorher."""
    keys = ["check"] + (["cosmetic"] if cosmetic in (True, "before") else []) + ["calibrate"]
    if cosmetic == "after":
        keys.append("cosmetic")
    if osc and mode in ("debayer", "extract"):
        keys.append("debayer")
    keys += ["register", "integrate", "finalize", "quality"]
    return keys


# ------------------------------------------------------------ Erfahrungswerte für Restzeit

def _timing_file(workdir: str) -> str:
    return os.path.join(workdir, "timing.json")


def load_timing(workdir: str) -> dict:
    try:
        return json.load(open(_timing_file(workdir), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def learn_timing(workdir: str, key: str, seconds: float, frames: int):
    """Sekunden pro Frame je Schritt merken (gleitender Mittelwert) – für künftige Restzeit-Schätzungen."""
    if frames <= 0 or seconds <= 0:
        return
    t = load_timing(workdir)
    v = seconds / frames
    t[key] = round(v if key not in t else 0.6 * t[key] + 0.4 * v, 3)
    try:
        os.makedirs(workdir, exist_ok=True)
        json.dump(t, open(_timing_file(workdir), "w", encoding="utf-8"), indent=1)
    except OSError:
        pass
