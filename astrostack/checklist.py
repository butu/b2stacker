"""Bearbeitungs-Checkliste nach dem Stacken – Reihenfolge für PixInsight (mit RC-Astro-Werkzeugen).

Grundregel: Alles, was das Bild „misst“ (Verläufe, Farbe, Schärfe, Rauschen), passiert LINEAR – also vor dem
Strecken. Erst danach wird gestreckt und gestaltet. Die Schlüssel sind stabil, damit abgehakte Punkte auch mit
neuen Programmversionen erhalten bleiben.
"""
from __future__ import annotations

_COMMON_LINEAR = [
    dict(key="inspect", phase="linear", title="Ergebnis ansehen", tool="STF (Auto-Stretch)",
         why="Kurz prüfen: Farbringe, Staubkringel, Kanten, Satellitenreste? Fehler hier lassen sich später nicht wegbearbeiten."),
    dict(key="crop", phase="linear", title="Ränder beschneiden", tool="DynamicCrop",
         why="Durch Dithering sind die Ränder unsauber. Bei mehreren Masters denselben Ausschnitt verwenden (Prozess-Icon auf alle ziehen)."),
    dict(key="gradient", phase="linear", title="Verläufe entfernen", tool="GraXpert oder DBE",
         why="Lichtverschmutzung und Mond erzeugen Helligkeitsverläufe. Vor der Farbkalibrierung entfernen, sonst wird falsch kalibriert."),
]

_RGB_LINEAR = [
    dict(key="solve", phase="linear", title="Astrometrie", tool="ImageSolver",
         why="Ordnet jedem Stern seine Position zu – Voraussetzung für die Farbkalibrierung mit Sternkatalog."),
    dict(key="bxt_correct", phase="linear", title="Sterne korrigieren", tool="BlurXTerminator – Correct only",
         why="Gleicht restliche Sternverformungen und Farbsäume aus, ohne zu schärfen."),
    dict(key="spcc", phase="linear", title="Farbkalibrierung", tool="SPCC",
         why="Farben nach echten Sternspektren – ergibt natürliche, neutrale Farben."),
]

_SHARP_NOISE = [
    dict(key="bxt", phase="linear", title="Schärfen", tool="BlurXTerminator",
         why="Dekonvolution wirkt nur auf lineare Daten richtig. Sterne etwas verkleinern, Nebel moderat schärfen."),
    dict(key="nxt", phase="linear", title="Entrauschen (leicht)", tool="NoiseXTerminator",
         why="Linear sanft entrauschen (ca. 0,5–0,7). Kräftiger erst nach dem Strecken, falls nötig."),
]

_DUAL = [
    dict(key="combine_hoo", phase="linear", title="Farbbild aus Ha und OIII", tool="PixelMath (R=Ha, G=OIII, B=OIII)",
         why="HOO-Mischung. Vorher OIII per LinearFit an Ha angleichen, damit beide Kanäle ähnlich hell sind."),
]

_MIX = [
    dict(key="nb_into_rgb", phase="linear", title="Ha in RGB einmischen (optional)", tool="NBRGBCombination / PixelMath",
         why="Bringt Ha-Strukturen ins Farbbild, ohne die Sternfarben zu verfälschen."),
]

_NONLINEAR = [
    dict(key="stretch", phase="nonlinear", title="Strecken", tool="GHS oder Statistical Stretch",
         why="Macht das lineare Bild sichtbar. Ab hier ist das Bild nicht mehr linear."),
    dict(key="sxt", phase="nonlinear", title="Sterne trennen", tool="StarXTerminator",
         why="Nebel und Sterne getrennt bearbeiten – Kontrast im Nebel, ohne dass Sterne aufblähen."),
    dict(key="starless", phase="nonlinear", title="Nebel gestalten", tool="Curves, LHE/MLT, Sättigung",
         why="Kontrast, Farbe und Struktur im sternlosen Bild."),
    dict(key="stars", phase="nonlinear", title="Sterne zurück", tool="PixelMath (Screen)",
         why="Sterne dezent wieder einblenden: ~((~starless)*(~stars))."),
    dict(key="export", phase="nonlinear", title="Exportieren", tool="Speichern als TIFF/JPG",
         why="Für Web sRGB-JPG, für Druck 16-bit TIFF. Die XISF-Bearbeitung behalten."),
]


def items(outputs: list[str]) -> list[dict]:
    """Checkliste passend zu den vorhandenen Ergebnissen (z. B. ["RGB"], ["Ha","OIII"], ["RGB","Ha","OIII"])."""
    outs = {o.upper() for o in outputs}
    has_rgb = bool(outs & {"RGB", "L", "R", "G", "B"}) or not outs
    has_nb = bool(outs & {"HA", "OIII", "SII"})
    lst = list(_COMMON_LINEAR)
    if has_rgb:
        lst += _RGB_LINEAR
    lst += _SHARP_NOISE
    if has_nb and not has_rgb:
        lst += _DUAL
    if has_nb and has_rgb:
        lst += _DUAL + _MIX
    lst += _NONLINEAR
    return lst
