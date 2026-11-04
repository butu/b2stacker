"""Plan view for the selected engine: shows what the run actually applies (shared with the Siril run)."""
from __future__ import annotations

# Plan-Optionen ohne Wirkung in Siril → Begründung aus siril.NOT_MAPPED (Schlüssel = WBPP-Name)
_OPTION_NOT_MAPPED = {"local_normalization": "localNormalization",
                      "distortion_correction": "distortionCorrection",
                      "large_scale_rejection": "lightsLargeScaleRejectionHigh"}

from .siril import INTERP, REG_TRANSFORM, REJECTION

SIRIL_REJECTION = REJECTION + " (winsorisiert, Sigma 3/3)"
SIRIL_REGISTRATION = ("Homografie" if REG_TRANSFORM == "-transf=homography" else REG_TRANSFORM) + \
    ", " + ("Lanczos4" if INTERP == "lanczos4" else INTERP)


def _unsupported() -> dict:
    from .siril import NOT_MAPPED
    out = {k: "in Siril nicht verfügbar: " + NOT_MAPPED[wk] for k, wk in _OPTION_NOT_MAPPED.items()}
    return out


def _settings(plan: dict, opt: dict, weight: str, findstar: list[str]) -> list:
    unsup = _unsupported()
    over = {
        "Gewichtung": ("Siril -weight=" + weight,
                       "Siril gewichtet nach Frame-FWHM; die PSF-Signal-Gewichtung von WBPP gibt es nur dort."
                       if weight == "wfwhm" else "Siril gewichtet nach Rauschen (Näherung für SNR)."),
        "Local Normalization": ("in Siril nicht verfügbar", unsup["local_normalization"]),
        "Rejection": (SIRIL_REJECTION, unsup["large_scale_rejection"] if opt.get("large_scale_rejection")
                      else "Ausreißer werden pro Pixel abgelehnt."),
        "Registrierung": (SIRIL_REGISTRATION, unsup["distortion_correction"] if opt.get("distortion_correction")
                          else "Ausrichtung über die Sterne."),
        "Autocrop": (("Siril -framing=min" if opt.get("autocrop") else "aus"),
                     "Nur die gemeinsame Fläche bleibt (wie WBPP)." if opt.get("autocrop")
                     else "Siril behält die volle Größe; Ränder einzelner Frames bleiben."),
        "Drizzle": ("nicht in Siril automatisiert", "Drizzle nur im Stacking-Programm von Hand, falls gewünscht."),
    }
    rows = []
    for row in plan.get("settings") or []:
        label, value, why = (list(row) + ["", "", ""])[:3]
        if label in over:
            value, why = over[label]
        rows.append([label, value, why])
        if label == "Rejection" and findstar:
            rows.append(["Sternerkennung", "; ".join(findstar), "Siril misst Sterne mit setfindstar; Werte aus der Analyse."])
    return rows


def apply(plan: dict, engine: str) -> dict:
    """Kopie des Plans mit der Anzeige der gewählten Engine. PixInsight: unverändert."""
    if engine != "siril":
        return plan
    from .siril import map_wbpp
    opt = plan.get("options") or {}
    runs, findstar = [], []
    for r in plan.get("runs") or []:
        mp = map_wbpp(plan, r)
        fs = " ".join("-%s=%s" % kv for kv in sorted(mp["findstar"].items()))
        line = ("setfindstar " + fs) if fs else "Siril-Standard"
        if fs:
            findstar.append(line)
        runs.append(dict(r, siril=dict(weight=mp["weight"], findstar=line, notes=mp["notes"])))
    weight = map_wbpp(plan, {})["weight"]
    return dict(plan, runs=runs, settings=_settings(plan, opt, weight, findstar),
                engine_view=dict(engine="siril", label="Siril", weight=weight, unsupported=_unsupported()))
