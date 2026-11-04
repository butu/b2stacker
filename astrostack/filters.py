"""Filter-Katalog: Filtername → Filtertyp → Verarbeitungsart.

Die Verarbeitung hängt von zwei Dingen ab:
  * Filtertyp (Breitband, Dualband Ha/OIII, Dualband SII/OIII, Einzel-Schmalband, L/R/G/B)
  * Kamera (Farbkamera mit Bayer-Matrix = OSC, oder Mono)

Unbekannte Filter liefern kind="unknown"; die Oberfläche fragt dann einmal nach und speichert
die Zuordnung in den Einstellungen (``custom_filters``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Filtertypen
BROADBAND = "broadband"            # RGB/UV-IR-Cut/Clear/L-Pro … an Farbkamera; L an Mono
DUAL_HA_OIII = "dualband_ha_oiii"  # z. B. HaO3, L-eXtreme, L-eNhance, ALP-T
DUAL_SII_OIII = "dualband_sii_oiii"
NARROW_HA = "narrow_ha"
NARROW_SII = "narrow_sii"
NARROW_OIII = "narrow_oiii"
MONO_L = "mono_l"
MONO_R = "mono_r"
MONO_G = "mono_g"
MONO_B = "mono_b"
UNKNOWN = "unknown"

# Aliase (normalisiert: klein, ohne Leer-/Sonderzeichen)
_ALIASES: dict[str, str] = {}


def _add(kind: str, *names: str) -> None:
    for n in names:
        _ALIASES[_norm(n)] = kind


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_add(BROADBAND, "rgb", "uvir", "uv/ir", "uvircut", "ircut", "irc", "uvirblock", "clear", "none", "nofilter",
     "cls", "clsccd", "lpro", "l-pro", "lpr", "uhc", "lps", "lpsd1", "lpsd2", "lpsp2", "cbp", "osc", "color", "colour",
     "ir", "uvirfilter", "irfilter", "farbe")
_add(DUAL_HA_OIII, "hao3", "haoiii", "ha/oiii", "ha-oiii", "ha+oiii", "hoo", "lextreme", "l-extreme", "optolonglextreme",
     "lenhance", "l-enhance", "lultimate", "l-ultimate", "alpt", "alp-t", "alpthao3", "duoband", "duo", "dual",
     "dualband", "nbz", "idasnbz", "triad", "triband", "quadband", "qbp", "askarcolourmagicd1")
_add(DUAL_SII_OIII, "siio3", "s2o3", "sii/oiii", "sii-oiii", "siioiii", "so3", "soo", "alptsiio3",
     "askarcolourmagicd2", "lsynergy")
_add(NARROW_HA, "ha", "h-a", "halpha", "h-alpha", "hα", "ha3nm", "ha5nm", "ha6nm", "ha7nm", "ha12nm")
_add(NARROW_SII, "sii", "s2", "s-ii", "sulfur", "sulphur", "schwefel", "sii3nm", "sii5nm", "sii6nm", "sii7nm")
_add(NARROW_OIII, "oiii", "o3", "o-iii", "oiii3nm", "oiii5nm", "oiii6nm", "oiii7nm")
_add(MONO_L, "l", "lum", "luminance", "lumi")
_add(MONO_R, "r", "red", "rot")
_add(MONO_G, "g", "green", "grün", "gruen")
_add(MONO_B, "b", "blue", "blau")


def classify(filter_name: str | None, custom: dict[str, str] | None = None) -> str:
    """Filtername → Filtertyp. ``custom`` überschreibt den eingebauten Katalog."""
    key = _norm(filter_name)
    if custom:
        for name, kind in custom.items():
            if _norm(name) == key:
                return kind
    if key in _ALIASES:
        return _ALIASES[key]
    # Teilstrings für typische Herstellernamen ("Optolong L-eXtreme 2"")
    for token, kind in (("lextreme", DUAL_HA_OIII), ("lenhance", DUAL_HA_OIII), ("lultimate", DUAL_HA_OIII),
                        ("lsynergy", DUAL_SII_OIII)):
        if token in key:
            return kind
    # Linien einzeln erkennen und kombinieren ("Ha/OIII 7nm", "OIII-SII", "Antlia 3nm Ha Pro"):
    # erst die Wort-Bausteine, damit "Ha" in "Halpha" oder "OIII" in "Ha-OIII" nicht doppelt zählt.
    words = re.split(r"[^a-z0-9α]+", (filter_name or "").lower())
    lines = set()
    for w in words:
        k = _ALIASES.get(_norm(w))
        if k in (DUAL_HA_OIII, DUAL_SII_OIII):
            return k
        if k in (NARROW_HA, NARROW_SII, NARROW_OIII):
            lines.add(k)
    if len(lines) == 3:
        return UNKNOWN          # Triband: Zuordnung der Kanäle fragt die Oberfläche einmal nach
    joined = key
    for token, kind in (("siioiii", DUAL_SII_OIII), ("oiiisii", DUAL_SII_OIII), ("haoiii", DUAL_HA_OIII),
                        ("oiiiha", DUAL_HA_OIII), ("hao3", DUAL_HA_OIII)):
        if token in joined:
            return kind
    for token, kind in (("sii", NARROW_SII), ("oiii", NARROW_OIII), ("halpha", NARROW_HA)):
        if token in joined:
            lines.add(kind)
    if lines == {NARROW_HA, NARROW_OIII}:
        return DUAL_HA_OIII
    if lines == {NARROW_SII, NARROW_OIII}:
        return DUAL_SII_OIII
    if len(lines) == 1:
        return lines.pop()
    for token, kind in (("uvir", BROADBAND), ("lpro", BROADBAND)):
        if token in key:
            return kind
    if key == "":
        return BROADBAND  # kein Filtereintrag: in der Regel ohne Filter / UV-IR in der Kamera
    return UNKNOWN


@dataclass(frozen=True)
class Processing:
    """Wie ein Filter-/Kamera-Paar verarbeitet wird."""

    mode: str                         # "debayer" | "extract" | "mono"
    outputs: tuple[str, ...]          # Namen der Ergebnis-Masters
    channel_map: dict[str, str] = field(default_factory=dict)  # Ausgabe → CFA-Kanäle ("R", "GB", ...)
    note: str = ""


def processing_for(kind: str, is_osc: bool) -> Processing:
    """Verarbeitungsart für Filtertyp + Kameratyp."""
    if not is_osc:
        name = {MONO_L: "L", MONO_R: "R", MONO_G: "G", MONO_B: "B", NARROW_HA: "Ha", NARROW_SII: "SII",
                NARROW_OIII: "OIII", BROADBAND: "L"}.get(kind, "Mono")
        return Processing("mono", (name,), note="Monokamera: Filter einzeln stacken")
    if kind == DUAL_HA_OIII:
        return Processing("extract", ("Ha", "OIII"), {"Ha": "R", "OIII": "GB"},
                          "Dualband an Farbkamera: Ha aus den roten, OIII aus den grünen und blauen Pixeln")
    if kind == DUAL_SII_OIII:
        return Processing("extract", ("SII", "OIII"), {"SII": "R", "OIII": "GB"},
                          "Dualband an Farbkamera: SII aus den roten, OIII aus den grünen und blauen Pixeln")
    if kind == NARROW_HA:
        return Processing("extract", ("Ha",), {"Ha": "R"}, "Ha-Filter an Farbkamera: nur rote Pixel")
    if kind == NARROW_SII:
        return Processing("extract", ("SII",), {"SII": "R"}, "SII-Filter an Farbkamera: nur rote Pixel")
    if kind == NARROW_OIII:
        return Processing("extract", ("OIII",), {"OIII": "GB"}, "OIII-Filter an Farbkamera: grüne und blaue Pixel")
    return Processing("debayer", ("RGB",), note="Breitband an Farbkamera: normal debayern")


def is_narrowband(kind: str) -> bool:
    return kind in (DUAL_HA_OIII, DUAL_SII_OIII, NARROW_HA, NARROW_SII, NARROW_OIII)
