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
               low_alt: float = 30.0, flat_groups: dict | None = None) -> dict:
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
        runs.append(_merge_flat_groups(dict(
            name=name, filter=g.filter, filter_kind=g.filter_kind, mode=proc.mode, outputs=list(proc.outputs),
            channel_map=proc.channel_map, engine="pixinsight-wbpp", enabled=True,
            reference=ref, lightCount=len(frames), nights=sorted({f.night for f in frames if f.night}),
            hours=round(sum(f.exposure or 0 for f in frames) / 3600, 2), is_osc=g.is_osc,
            light_dirs=sorted({os.path.dirname(f.path) for f in frames}),
            bias_set=bias and bias["set"]["folder"], dark_set=dark and dark["set"]["folder"],
            flat_session=flat and flat["set"].get("session"), flat_sky=bool(flat and flat["set"].get("sky")),
            flat_reasons=flat["reasons"] if flat else [],
            settings_key=list(g.settings),
            calib_choices={k: _choices((m or {}).get(k, []), k) for k in ("flats", "bias", "darks", "darkflats")},
            darkflat_set=dflat and dflat["set"]["folder"],
            **_flat_group_fields(flat_groups, g, frames, flat),
            lights=[f.path for f in frames],
            flats=[x.path for x in _frames_of(flat)] if flat else [],
            flat_set=flat and flat["set"]["folder"], flat_ok=bool(flat and (not flat.get("check") or flat["check"]["verdict"] != "critical")),
            bias=[x.path for x in _frames_of(bias)] if bias else [],
            darkflats=[x.path for x in _frames_of(dflat)] if dflat else [],
            darks=[x.path for x in _frames_of(dark)] if dark else [],
            gb=round(disk, 1), ram_gb=round(ram, 1), why=why)))
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
        ("Farbsäume", "nach dem Stacken korrigieren",
         "Rot und Blau werden an den Sternen auf Grün ausgerichtet (atmosphärische Dispersion, v. a. bei tiefem Objekt)."),
        ("Dateinamen", "Ziel_Filter_Datum_Stunden.xisf", "Jeder Stack bekommt einen eigenen Namen – nichts wird überschrieben."),
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
        pixscale=round(scale, 4) if scale else None,
        options=dict(fix_fringes=True, local_normalization=nn > 1, drizzle=bool(samp and samp <= 3), autocrop=False,
                     weighting="psf_signal", rejection="auto", large_scale_rejection=True, distortion_correction=True,
                     cleanup=True))
    return plan


def _merge_flat_groups(run: dict) -> dict:
    fg = run.get("flat_groups")
    if fg and len(fg) > 1:
        allf = []
        for x in fg:
            allf += [p for p in x["flats"] if p not in allf]
        run["flats"] = allf
        run["flat_set"] = " + ".join(sorted({x["flat_set"] for x in fg if x["flat_set"]})) or None
        run["flat_ok"] = all(x["flat_ok"] for x in fg)
        run["why"] += " Die Kamera wurde zwischen den Nächten gedreht: %d Nachtgruppen mit eigenen Flats." % len(fg)
    return run


def _flat_group_fields(flat_groups, g, frames, flat) -> dict:
    """Drehungs-Info je Nacht und – falls nötig – eigene Flats je Nachtgruppe."""
    fg = (flat_groups or {}).get((g.filter, tuple(g.settings)))
    if not fg:
        return {}
    used_nights = {f.night for f in frames}
    groups = [x for x in fg["groups"] if set(x["nights"]) & used_nights]
    rot = dict(per_night={n: dict(rotator=fg["info"]["header"].get(n),
                                  sky=(fg["info"]["measured"].get(n) or {}).get("angle"),
                                  conf=(fg["info"]["measured"].get(n) or {}).get("conf"))
                          for n in sorted(used_nights)},
               groups=len(groups), source=groups[0]["source"] if groups else None)
    out = dict(rotation=rot)
    if len(groups) < 2:
        return out
    lst, notes = [], []
    for x in groups:
        m = x.get("match") or {}
        best = next((c for c in m.get("flats", []) if c["usable"]), None)
        gl = [f.path for f in frames if f.night in x["nights"]]
        lst.append(dict(key=x["key"], nights=x["nights"], angle=x["angle"], source=x["source"], lights=gl,
                        flats=[f.path for f in best.get("_frames", [])] if best else [],
                        flat_set=best and best["set"]["folder"], flat_session=best and best["set"].get("session"),
                        flat_sky=bool(best and best["set"].get("sky")), flat_reasons=best["reasons"] if best else [],
                        flat_ok=bool(best and (best.get("check") or {}).get("verdict") != "critical")))
    ids = {x["flat_set"] + "|" + str(x["flat_session"]) for x in lst if x["flat_set"]}
    if len(ids) < len(lst):
        notes.append("Die Kamera stand in %d Stellungen, aber es gibt nicht für jede eigene Flats – "
                     "für manche Nächte passen Staub und Vignettierung nicht genau." % len(lst))
    out.update(flat_groups=lst, flat_group_notes=notes)
    return out


CHOICE_MAX = 15


def _label(cand: dict, kind: str) -> str:
    st = cand["set"]
    parts = [st["folder"].replace("\\", "/").split("/")[-2] + "/" + st["folder"].replace("\\", "/").split("/")[-1]]
    if kind == "flats":
        t = (st.get("session") or "")[11:16]
        er = st.get("exposure_range")
        exp = ("%g–%g s" % tuple(er)) if (er and er[1] > er[0] * 1.15) else ("%g s" % st["exposure"] if st.get("exposure") else "")
        parts.append(" ".join(x for x in (((st.get("date") or [""])[0] or "")[:10], t, exp) if x))
        if st.get("sky"):
            parts.append("Himmelsflats")
    elif kind in ("darks", "darkflats") and st.get("exposure"):
        parts.append("%g s" % st["exposure"])
    return " · ".join(p for p in parts if p)


def _choices(cands: list, kind: str) -> list:
    """Auswahlliste für die Oberfläche: brauchbare zuerst, dann einige unbrauchbare (mit Grund)."""
    out = []
    for c in sorted(cands, key=lambda x: (not x["usable"], -x["score"]))[:CHOICE_MAX]:
        st = c["set"]
        out.append(dict(id=st.get("id") or st["folder"], label=_label(c, kind), folder=st["folder"], frames=st["frames"],
                        usable=c["usable"], score=c["score"], reasons=c["reasons"],
                        verdict=(c.get("check") or {}).get("verdict"), sky=st.get("sky", False), session=st.get("session"),
                        paths=[f.path for f in c.get("_frames", [])]))
    return out


def _frames_of(cand):
    """Kandidat aus calibcheck → Frame-Objekte (über die gespeicherte Referenz)."""
    return cand.get("_frames", []) if cand else []
