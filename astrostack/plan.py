"""Stacking-Plan: Läufe, Kalibrierdaten, Einstellungen mit Begründung, Speicher-/RAM-Bedarf.

Der Plan ist eine JSON-Datei, die der Nutzer anpassen kann, bevor gestartet wird.
"""
from __future__ import annotations

import datetime
import os
import statistics as st

from . import filters as flt
from . import system


def _med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def build_plan(target: str, groups, rows, ntab, matches, workdir: str, engine: str = "pixinsight",
               low_alt: float = 30.0) -> dict:
    """groups: LightGroups des Ziels; rows: bewertete Frames; matches: calibcheck.match_calibration-Ergebnis."""
    status = {r["path"]: r["status"] for r in rows}
    runs, settings = [], []
    first_debayer = None
    total_frames = 0
    peak_disk = peak_ram = 0.0
    order = sorted(groups, key=lambda g: flt.is_narrowband(g.filter_kind))
    for g in order:
        frames = [f for f in g.frames
                  if ((status[f.path] not in ("rejected", "suggest")) if f.path in status else not f.excluded)]
        if not frames:
            continue
        proc = flt.processing_for(g.filter_kind, g.is_osc)
        m = next((x for x in matches if x["group"]["target"] == g.target and x["group"]["filter"] == g.filter
                  and x["group"]["readout"] == g.settings[3]), None)
        flat = next((c for c in (m or {}).get("flats", []) if c["usable"]), None)
        bias = next((c for c in (m or {}).get("bias", []) if c["usable"]), None)
        dflat = next((c for c in (m or {}).get("darkflats", []) if c["usable"]), None)
        dark = next((c for c in (m or {}).get("darks", []) if c["usable"]), None)
        alt_med = _med([f.alt for f in frames])
        w, h = g.settings[5] or 6000, g.settings[6] or 4000
        # Dualband/Schmalband an OSC wird (wegen des WBPP-Fehlers bei "separated") als Farbbild gestackt
        stack_mode = "debayer" if (g.is_osc and proc.mode in ("debayer", "extract")) else proc.mode
        chans = 3 if stack_mode == "debayer" else 1
        disk = system.estimate_disk_gb(len(frames), w, h, g.is_osc, stack_mode)
        ram = system.estimate_ram_gb(len(frames), w, h, chans)
        peak_disk, peak_ram = max(peak_disk, disk), max(peak_ram, ram)
        total_frames += len(frames)
        if proc.mode == "debayer":
            why = "Breitband an Farbkamera: normal debayern."
            if alt_med is not None and alt_med < low_alt:
                why += " Objekt stand tief (Median %.0f°): Farbsäume in der Bearbeitung (BlurX „Correct only“) korrigieren." % alt_med
        elif proc.mode == "extract":
            why = proc.note + ". WBPP stackt das Farbbild, danach wird aufgeteilt (%s)." % ", ".join(
                "%s = %s" % (k, {"R": "Rot", "GB": "(Grün+Blau)/2", "G": "Grün", "B": "Blau"}.get(v, v)) for k, v in proc.channel_map.items())
        else:
            why = proc.note
        ref = "auto"
        if first_debayer is None and proc.mode in ("debayer", "mono"):
            first_debayer = g.filter or "L"
        elif first_debayer:
            ref = "master:" + first_debayer
        name = (g.filter or "L").replace("/", "_")
        runs.append(dict(
            name=name, filter=g.filter, filter_kind=g.filter_kind, mode=proc.mode, outputs=list(proc.outputs),
            channel_map=proc.channel_map, engine="pixinsight-wbpp", enabled=True,
            reference=ref, lightCount=len(frames), nights=sorted({f.night for f in frames if f.night}),
            lights=[f.path for f in frames],
            flats=[x.path for x in _frames_of(flat)] if flat else [],
            flat_set=flat and flat["set"]["folder"], flat_ok=bool(flat and (not flat.get("check") or flat["check"]["verdict"] != "critical")),
            bias=[x.path for x in _frames_of(bias)] if bias else [],
            darkflats=[x.path for x in _frames_of(dflat)] if dflat else [],
            darks=[x.path for x in _frames_of(dark)] if dark else [],
            gb=round(disk, 1), ram_gb=round(ram, 1), why=why))
    nn = len({n["night"] for n in ntab})
    moon_n = sum(1 for n in ntab if n["moon"])
    any_run = runs[0] if runs else None
    scale = _med([r["pixscale"] for r in rows])
    fw = _med([r["fwhm_as"] for r in rows if r["status"] not in ("rejected", "suggest")])
    samp = (fw / scale) if (fw and scale) else None
    has_darks = any(r["darks"] for r in runs)
    settings += [("Kalibrierung", "Bias%s + Flats je Filter%s" % (" / Dark-Flats" if any(r["darkflats"] for r in runs) else "", " + Darks" if has_darks else ""),
                  "Darks gefunden und zugeordnet." if has_darks else "Keine passenden Darks – Hotpixel entfernt die Rejection dank Dithering.")]
    settings += [("Verarbeitung " + r["name"], {"debayer": "Farbbild", "extract": "Kanäle " + " + ".join(r["outputs"]), "mono": "Mono"}[r["mode"]], r["why"]) for r in runs]
    settings += [
        ("Registrierung", "Verzerrungskorrektur an", "Großes Bildfeld: Sterne am Rand leicht verzerrt."),
        ("Referenz", "bester Frame %s, weitere Läufe auf dessen Master" % (first_debayer or (any_run and any_run["name"]) or ""),
         "Alle Masters liegen pixelgenau übereinander."),
        ("Gewichtung", "PSF Signal Weight", ("%d von %d Nächten mit Mond: schwache Frames zählen automatisch weniger." % (moon_n, nn)) if moon_n else "Gewichtet nach Signal und Schärfe."),
        ("Local Normalization", "an" if nn > 1 else "aus", ("%d Nächte mit unterschiedlichem Hintergrund." % nn) if nn > 1 else "Nur eine Nacht."),
        ("Rejection", "automatisch + Large-Scale (hoch)", "Entfernt Satelliten- und Flugzeugspuren."),
        ("Drizzle", "aus" if (samp is None or samp > 3) else "2× empfohlen (in WBPP von Hand)",
         ("FWHM %.1f\" = %.1f px: überabgetastet." % (fw, samp)) if (samp and samp > 3) else (("FWHM %.1f\" = %.1f px: Drizzle 2× lohnt." % (fw, samp)) if samp else "Keine FWHM-Daten.")),
        ("Autocrop", "aus", "Masters bleiben gleich groß und deckungsgleich; Zuschneiden in der Bearbeitung."),
        ("Zwischendateien", "nach jedem Lauf löschen", "Spart Platz; Masters und Logs bleiben."),
    ]
    free_disk = system.free_disk_gb(workdir)
    free_ram, total_ram = system.ram_gb()
    suggest_n = sum(1 for r in rows if r["status"] == "suggest")
    plan = dict(
        version=1, target=target, created=datetime.datetime.now().isoformat(timespec="seconds"), engine=engine,
        workdir=os.path.join(workdir, "projects", target), runs=runs,
        settings=[list(s) for s in settings], need_gb=round(peak_disk + 2, 1), free_gb=round(free_disk, 1) if free_disk is not None else None,
        need_ram_gb=round(peak_ram, 1), free_ram_gb=round(free_ram, 1) if free_ram else None, total_ram_gb=round(total_ram, 1) if total_ram else None,
        peak_note="Zwischendateien werden nach jedem Lauf gelöscht, daher zählt der größte Lauf.",
        suggest_n=suggest_n, total_frames=total_frames,
        options=dict(local_normalization=nn > 1, drizzle=bool(samp and samp <= 3), autocrop=False,
                     weighting="psf_signal", rejection="auto", large_scale_rejection=True, distortion_correction=True,
                     cleanup=True))
    return plan


def _frames_of(cand):
    """Kandidat aus calibcheck → Frame-Objekte (über die gespeicherte Referenz)."""
    return cand.get("_frames", []) if cand else []
