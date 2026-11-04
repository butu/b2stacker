"""Bewertung der vermessenen Frames: Nacht-Statistik, Frame-Status, Gewichte, Empfehlungen."""
from __future__ import annotations

import csv
import os
import re
import statistics as st
from dataclasses import dataclass


@dataclass
class FrameThresholds:
    clouds_star_ratio: float = 0.6   # Sterne < 60 % des Nacht-Medians → Wolken/Dunst
    bg_ratio: float = 1.5            # Hintergrund > 150 % des Nacht-Medians → Dämmerung/Mond/Licht
    fwhm_ratio: float = 1.25         # FWHM > 125 % des Filter-Medians → unscharf
    ecc_max: float = 0.6             # Exzentrizität darüber → Hinweis
    temp_tol: float = 1.0            # |CCD-TEMP − SET-TEMP| > 1 °C → Hinweis
    moon_illum: float = 50           # Mondnacht, wenn > 50 % beleuchtet …
    moon_alt: float = 0              # … und über dem Horizont


NUM = ["exp", "gain", "offset", "temp", "settemp", "alt", "airmass", "hum", "ambtemp", "dewpoint", "wind", "focpos",
       "foctemp", "rot", "pixscale", "hfr_name", "moon_illum", "moon_alt", "bg", "noise", "snr_proxy", "nstars", "fwhm",
       "ecc", "grad", "r_g", "b_g", "p001", "sat_frac", "fwhm_c", "fwhm_corner", "coherence", "coh_angle", "radial"]


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(metrics_csv: str) -> list[dict]:
    rows = list(csv.DictReader(open(metrics_csv, encoding="utf-8")))
    for r in rows:
        for k in NUM:
            r[k] = fnum(r.get(k))
        r["excluded"] = r.get("excluded") in ("1", "True", "true")
        name = os.path.basename(r["path"])
        r["file"] = name
        m = re.search(r"(\d{3,5})(?=\.[A-Za-z]+$)", name)
        r["frame"] = m.group(1) if m else os.path.splitext(name)[0][-12:]
        r["t"] = r.get("date_obs") or ""
        r["fwhm_as"] = round(r["fwhm"] * r["pixscale"], 2) if (r["fwhm"] and r["pixscale"]) else None
    rows.sort(key=lambda r: r["t"])
    return rows


def _med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def assess(rows: list[dict], th: FrameThresholds | None = None, overrides: dict | None = None):
    """overrides: {pfad: "in"|"out"} – manuelle Entscheidungen aus der Oberfläche."""
    th = th or FrameThresholds()
    overrides = overrides or {}
    filters = []
    for r in rows:
        if r["filter"] not in filters:
            filters.append(r["filter"])
    nights: dict = {}
    for r in rows:
        nights.setdefault((r["night"], r["filter"]), []).append(r)
    nstat = {}
    for k, v in nights.items():
        nstat[k] = dict(nstars=_med([x["nstars"] for x in v]), bg=_med([x["bg"] for x in v]),
                        noise=_med([x["noise"] for x in v]), fwhm=_med([x["fwhm"] for x in v]),
                        fwhm_as=_med([x["fwhm_as"] for x in v]), ecc=_med([x["ecc"] for x in v]),
                        moon_illum=_med([x["moon_illum"] for x in v]),
                        moon_alt=max([x["moon_alt"] for x in v if x["moon_alt"] is not None], default=None),
                        alt_min=min([x["alt"] for x in v if x["alt"] is not None], default=None),
                        alt_max=max([x["alt"] for x in v if x["alt"] is not None], default=None),
                        temp=_med([x["temp"] for x in v]))
    fmed = {f: _med([r["fwhm"] for r in rows if r["filter"] == f]) for f in filters}
    noise_best = {f: min([nstat[k]["noise"] for k in nstat if k[1] == f and nstat[k]["noise"]], default=None) for f in filters}
    dup_of = {}                                       # gleicher Inhalt (SHA256) → nur die erste Datei zählt
    first = {}
    for r in sorted(rows, key=lambda x: x["path"]):
        sha = r.get("sha256") or ""
        if sha:
            if sha in first:
                dup_of[r["path"]] = first[sha]
            else:
                first[sha] = r["path"]
    for r in rows:
        ns = nstat[(r["night"], r["filter"])]
        flags = []
        if r["path"] in dup_of:
            other = os.path.normpath(dup_of[r["path"]]).split(os.sep)
            flags.append(("reject", "Duplikat: gleicher Inhalt wie …%s%s" % (os.sep, os.sep.join(other[-3:]))))
        if r["nstars"] is not None and ns["nstars"] and r["nstars"] < th.clouds_star_ratio * ns["nstars"]:
            flags.append(("reject", "Wolken/Dunst (wenige Sterne)"))
        if r["bg"] is not None and ns["bg"] and r["bg"] > th.bg_ratio * ns["bg"]:
            flags.append(("reject", "Hintergrund erhöht (Dämmerung/Mond/Licht)"))
        if r["fwhm"] and fmed[r["filter"]] and r["fwhm"] > th.fwhm_ratio * fmed[r["filter"]]:
            flags.append(("reject", "unscharf (FWHM)"))
        if r["ecc"] and r["ecc"] > th.ecc_max:
            flags.append(("warn", "elongierte Sterne"))
        if r["temp"] is not None and r["settemp"] is not None and abs(r["temp"] - r["settemp"]) > th.temp_tol:
            flags.append(("warn", "Sensortemperatur weicht ab (%.1f °C)" % r["temp"]))
        r["flags"] = flags
        if r["excluded"]:
            r["status"] = "rejected"
        elif any(f[0] == "reject" for f in flags):
            r["status"] = "suggest"
        elif flags:
            r["status"] = "warn"
        else:
            r["status"] = "ok"
        r["auto_status"] = r["status"]
        ov = overrides.get(r["path"])
        if ov == "out":
            r["status"] = "rejected"
            r["flags"] = flags + [("reject", "manuell ausgeschlossen")]
        elif ov == "in" and r["status"] in ("rejected", "suggest"):
            r["status"] = "warn" if flags else "ok"
            r["flags"] = flags + [("warn", "manuell aufgenommen")]
        r["override"] = ov
        nb = noise_best[r["filter"]]
        r["weight"] = min(1.0, (nb / r["noise"]) ** 2) if (r["noise"] and nb) else 0
    ntab = []
    for (night, f), v in sorted(nights.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        ns = nstat[(night, f)]
        used = [x for x in v if x["status"] not in ("rejected", "suggest")]
        hours = sum(x["exp"] or 0 for x in v) / 3600
        used_h = sum(x["exp"] or 0 for x in used) / 3600
        eff_h = sum((x["exp"] or 0) * x["weight"] for x in used) / 3600
        moon = ns["moon_illum"] is not None and ns["moon_illum"] > th.moon_illum and (ns["moon_alt"] if ns["moon_alt"] is not None else -90) > th.moon_alt
        ntab.append(dict(night=night, filter=f, n=len(v), n_used=len(used), hours=hours, used_h=used_h, eff_h=eff_h,
                         fwhm=ns["fwhm"], fwhm_as=round(ns["fwhm_as"], 2) if ns["fwhm_as"] else None,
                         noise=ns["noise"], bg=ns["bg"], nstars=ns["nstars"], moon_illum=ns["moon_illum"],
                         moon_alt=ns["moon_alt"], moon=moon, alt=(ns["alt_min"], ns["alt_max"]), temp=ns["temp"]))
    for f in filters:   # Qualität relativ zur besten Nacht desselben Filters
        nf = [n for n in ntab if n["filter"] == f]
        best = max([(n["eff_h"] / n["used_h"]) if n["used_h"] else 0 for n in nf], default=1) or 1
        for n in nf:
            q = (n["eff_h"] / n["used_h"] / best) if n["used_h"] else 0
            n["quality"] = q
            n["grade"] = "A" if q > 0.6 else "B" if q > 0.25 else "C"
    return filters, ntab


def recommendations(rows, ntab, filters) -> list[str]:
    rec = []
    sug = [r for r in rows if r["status"] == "suggest"]
    if sug:
        rec.append("%d Frames zum Aussortieren vorgeschlagen (Wolken, Dämmerung oder Unschärfe) – in der Galerie prüfen." % len(sug))
    for f in filters:
        nf = [n for n in ntab if n["filter"] == f]
        eff = sum(n["eff_h"] for n in nf)
        moon = [n for n in nf if n["moon"]]
        if moon and eff:
            share = sum(n["eff_h"] for n in moon) / eff
            rec.append("%s: Mondnächte %s liefern %.0f %% des effektiven Signals bei %.1f h Belichtung – die Gewichtung regelt das." % (
                f, ", ".join((n["night"] or "?")[5:] for n in moon), share * 100, sum(n["used_h"] for n in moon)))
        if nf:
            best = max(nf, key=lambda n: n["quality"])
            rec.append("%s: beste Nacht %s (%.1f h, FWHM %s\"). Weitere dunkle Nächte bringen am meisten." % (
                f, best["night"], best["used_h"], best["fwhm_as"]))
    return rec
