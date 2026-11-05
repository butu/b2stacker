"""Alle WBPP-Einstellungen, die b2stacker setzt – mit Art des Werts und gültigem Bereich.

Warum: Weg B setzt die Eigenschaften der WBPP-Engine direkt (``engine[k] = wert`` in ``pjsr/b2launch.js``).
WBPP prüft die Art erst, wenn es den Wert an einen Prozess weitergibt – mitten im Lauf. Ein ``"2"`` statt ``2``
ließ so beim Ausrichten jeden Frame scheitern (vdB 131, RGB, 05.11.2026), nachdem schon minutenlang kalibriert
worden war. Deshalb gilt hier:

- Jede Einstellung, die an WBPP geht, steht in ``SCHEMA``. Unbekannte Namen sind ein Fehler (Tippfehler oder eine
  neue Einstellung, die erst hier eingetragen werden muss) – nichts geht ungeprüft durch.
- ``normalize`` wandelt um, was eindeutig ist (``"2"`` → 2, ``"0,52"`` → 0.52, ``"true"`` → True) und begrenzt
  Zahlen auf den gültigen Bereich (mit Hinweis). Was sich nicht umwandeln lässt, ist ein Fehler mit Klartext.
- ``b2launch.js`` prüft in PixInsight zusätzlich gegen die echten Voreinstellungen der installierten WBPP-Version,
  bevor gerechnet wird; das nachgebaute PixInsight der Tests prüft genauso streng.
"""
from __future__ import annotations

B, I, F, S = "bool", "int", "float", "str"

# Name → (Art, kleinster Wert, größter Wert) bzw. (Art, None, None). Bereiche wie in PixInsight 1.9 / WBPP 2.7–3.2.
SCHEMA: dict[str, tuple] = {
    # Ablauf
    "autoIntegrationMode": (B, None, None),
    "imageRegistration": (B, None, None),
    "integrate": (B, None, None),
    "platesolve": (B, None, None),
    "generateRejectionMaps": (B, None, None),
    "groupingKeywordsEnabled": (B, None, None),
    "localNormalization": (B, None, None),
    "distortionCorrection": (B, None, None),
    "autocrop": (B, None, None),
    "lightsLargeScaleRejectionHigh": (B, None, None),
    "subframeWeightingEnabled": (B, None, None),
    "subframesWeightsMethod": (I, 0, 3),
    "bestFrameReferenceMethod": (I, 0, 2),
    "referenceImage": (S, None, None),
    # Debayern
    "debayerOutputMethod": (I, 0, 2),
    "recombineRGB": (B, None, None),
    "debayerActiveChannelR": (B, None, None),
    "debayerActiveChannelG": (B, None, None),
    "debayerActiveChannelB": (B, None, None),
    # Sternerkennung (StarAlignment)
    "hotPixelFilterRadius": (I, 0, 2),
    "noiseReductionFilterRadius": (I, 0, 50),
    "structureLayers": (I, 1, 8),
    "minStructureSize": (I, 0, 64),
    "sensitivity": (F, 0.0, 1.0),
    "peakResponse": (F, 0.0, 1.0),
    "maxStarDistortion": (F, 0.0, 1.0),
    # Skripte
    "usePipelineBuilderScript": (B, None, None),
    "pipelineBuilderScriptFile": (S, None, None),
    "usePipelineScript": (B, None, None),
    "pipelineScriptFile": (S, None, None),
}

STAR_DETECTION = ("hotPixelFilterRadius", "noiseReductionFilterRadius", "structureLayers", "minStructureSize",
                  "sensitivity", "peakResponse", "maxStarDistortion")

# Index-Parameter (``engine[name][i]``): Name → (Art, kleinster, größter Wert)
INDEXED: dict[str, tuple] = {"rejection": (I, 0, 5)}
DRIZZLE = {"enabled": (B, None, None), "scale": (I, 1, 4), "dropShrink": (F, 0.0, 1.0)}


class WbppParamError(ValueError):
    """Einstellungen passen nicht zu WBPP. ``problems`` = Klartext je Einstellung."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Einstellungen passen nicht zu WBPP: " + "; ".join(problems))


def _cast(name: str, value, spec: tuple):
    """→ (Wert, Hinweis oder None). Wirft ValueError mit Klartext."""
    kind, lo, hi = spec
    if kind == B:
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value), None
        t = str(value).strip().lower()
        if t in ("true", "ja", "1", "an", "on", "yes"):
            return True, None
        if t in ("false", "nein", "0", "aus", "off", "no"):
            return False, None
        raise ValueError("%s braucht ja/nein, nicht %r" % (name, value))
    if kind == S:
        if isinstance(value, str):
            return value, None
        raise ValueError("%s braucht einen Text, nicht %r" % (name, value))
    if isinstance(value, bool):
        raise ValueError("%s braucht eine Zahl, nicht %r" % (name, value))
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        try:
            num = float(str(value).strip().replace(",", "."))
        except ValueError:
            raise ValueError("%s braucht eine Zahl, nicht %r" % (name, value)) from None
    if num != num:
        raise ValueError("%s ist keine Zahl" % name)
    if kind == I:
        if num != int(num):
            raise ValueError("%s braucht eine ganze Zahl, nicht %r" % (name, value))
        num = int(num)
    note = None
    if lo is not None and num < lo or hi is not None and num > hi:
        c = min(hi, max(lo, num))
        note = "%s=%s liegt außerhalb %s–%s → %s" % (name, value, lo, hi, c)
        num = c
    return (int(num) if kind == I else float(num)), note


def normalize(params: dict) -> tuple[dict, list[str]]:
    """Alle Parameter prüfen und in die richtige Art bringen. → (Parameter, Hinweise). Wirft WbppParamError."""
    out, notes, problems = {}, [], []
    for k, v in (params or {}).items():
        spec = SCHEMA.get(k)
        if spec is None:
            problems.append("unbekannte Einstellung „%s“" % k)
            continue
        try:
            out[k], note = _cast(k, v, spec)
        except ValueError as e:
            problems.append(str(e))
            continue
        if note:
            notes.append(note)
    if problems:
        raise WbppParamError(problems)
    return out, notes


def normalize_job(job: dict) -> list[str]:
    """Den ganzen Auftrag (params, indexed, drizzle) in place prüfen. → Hinweise. Wirft WbppParamError."""
    params, notes = normalize(job.get("params") or {})
    problems = []
    indexed = {}
    for k, arr in (job.get("indexed") or {}).items():
        spec = INDEXED.get(k)
        if spec is None:
            problems.append("unbekannte Einstellung „%s[]“" % k)
            continue
        vals = []
        for i, v in enumerate(arr):
            if v is None:
                vals.append(None)
                continue
            try:
                c, note = _cast("%s[%d]" % (k, i), v, spec)
                vals.append(c)
                if note:
                    notes.append(note)
            except ValueError as e:
                problems.append(str(e))
        indexed[k] = vals
    if job.get("drizzle"):
        d = {}
        for k, v in job["drizzle"].items():
            spec = DRIZZLE.get(k)
            if spec is None:
                problems.append("unbekannte Drizzle-Einstellung „%s“" % k)
                continue
            try:
                d[k], note = _cast("drizzle." + k, v, spec)
                if note:
                    notes.append(note)
            except ValueError as e:
                problems.append(str(e))
        job["drizzle"] = d
    if problems:
        raise WbppParamError(problems)
    job["params"] = params
    if "indexed" in job:
        job["indexed"] = indexed
    return notes


def star_values(values: dict) -> dict:
    """Von Hand eingegebene Sternerkennungs-Werte prüfen (beim Speichern im Plan) → als Zahlen; Fehler mit Klartext."""
    out, problems = {}, []
    for k, v in (values or {}).items():
        if k not in STAR_DETECTION:
            problems.append("unbekannter Wert der Sternerkennung „%s“" % k)
            continue
        if v in (None, ""):
            out[k] = None                       # zurück auf Automatik
            continue
        try:
            out[k], _ = _cast(k, v, SCHEMA[k])
        except ValueError as e:
            problems.append(str(e))
    if problems:
        raise WbppParamError(problems)
    return out
