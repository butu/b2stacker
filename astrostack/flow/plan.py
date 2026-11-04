"""Schritt 5: Plan aktualisieren – Läufe, WBPP-Einstellungen mit Begründung, Platz- und Speicherbedarf.

Grundsatz (Konzept Abschnitt 10): b2stacker gibt WBPP **alle** Einstellungen explizit mit, jede mit einem Grund
in Klartext. Gespeicherte WBPP-Dialogeinstellungen gelten im Automatik-Modus ohnehin nicht.

Der Plan liegt in ``plans.plan_json``. Änderungen des Nutzers stehen getrennt in ``plans.edits_json``
(``{"options": {...}, "runs": {"<run-id>": {"enabled": false, ...}}}``) und werden nach jeder Neuberechnung
wieder angewendet – eine Neuberechnung überschreibt nie, was der Nutzer eingestellt hat.
"""
from __future__ import annotations

import json
import statistics as st

from .. import db as dbm
from .. import filters as flt
from .. import system
from ..plan import light_stats, star_detection
from .calib import match_project

PLAN_VERSION = 1
MIN_LIGHTS = 3                 # darunter entfernt keine Rejection Satelliten und Hotpixel
DRIZZLE_MAX_SAMPLING = 2.0     # FWHM in Pixeln: darunter ist das Bild unterabgetastet → Drizzle lohnt
FEW_STARS = 80                 # Median der Sterne darunter → Verzerrungskorrektur aus (zu wenig Stützpunkte)
HOT_PIXELS_CC = 200            # Hotpixel je Frame darüber → Cosmetic Correction, wenn keine Darks da sind

# WBPP-Nummern (BPP-automation.js, WBPP 3.2.0)
REJECTION = {"percentile": 0, "winsorized": 1, "linear_fit": 2, "esd": 3, "rcr": 4, "auto": 5}
WEIGHTS = {"psf_signal": 0, "psf_snr": 1, "snr": 2}

DEFAULT_OPTIONS = dict(fast_integration=False, local_normalization=None, distortion_correction=None,
                       cosmetic_correction=None, drizzle=None, weighting="psf_signal", rejection=None,
                       large_scale_rejection=True, autocrop=False)


def _med(vals):
    v = [x for x in vals if x is not None]
    return st.median(v) if v else None


def rejection_for(n: int) -> tuple[str, str]:
    """Pixel-Rejection passend zur Frameanzahl (wie WBPP 3.1.1 automatisch, aber ausdrücklich gesetzt)."""
    if n <= 6:
        return "percentile", "%d Frames: Percentile Clipping (für sehr wenige Frames)" % n
    if n <= 14:
        return "winsorized", "%d Frames: Winsorized Sigma Clipping" % n
    return "esd", "%d Frames: Generalized ESD (am genauesten bei vielen Frames)" % n


def _lights(db: dbm.Database, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    out = []
    for r in db.query("SELECT f.id, f.night, f.width, f.height, f.exposure, m.nstars, m.fwhm, m.ecc, m.data_json "
                      "FROM files f LEFT JOIN frame_metrics m ON m.file_id = f.id WHERE f.id IN (%s)" % q, ids):
        d = json.loads(r["data_json"]) if r["data_json"] else {}
        out.append(dict(id=r["id"], night=r["night"], width=r["width"], height=r["height"], exp=r["exposure"],
                        nstars=r["nstars"], fwhm=r["fwhm"], ecc=r["ecc"], pixscale=d.get("pixscale"),
                        hot=d.get("hotpixels")))
    return out


def best_reference(lights: list[dict]) -> tuple[int | None, str]:
    """Referenzbild: unter den 25 % mit den meisten Sternen der schärfste Frame."""
    cand = [x for x in lights if x["nstars"] and x["fwhm"]]
    if not cand:
        return None, "keine Messwerte – WBPP wählt selbst"
    cand.sort(key=lambda x: -x["nstars"])
    top = cand[:max(1, len(cand) // 4)]
    best = min(top, key=lambda x: x["fwhm"])
    return best["id"], "schärfster Frame unter denen mit den meisten Sternen (%d Sterne, FWHM %.2f px)" % (
        best["nstars"], best["fwhm"])


def _run_id(run: dict) -> str:
    s = run["settings"]
    return "%s|%s|%s|%s|%s" % ((run["filter"] or "?").lower(), s.get("setup_id"), s.get("gain"), s.get("offset"),
                               (s.get("readout") or "").lower())


def build_plan(db: dbm.Database, pid: int, workdir: str, edits: dict | None = None) -> dict:
    edits = edits or {}
    opt_edit = edits.get("options") or {}
    runs_in = match_project(db, pid)
    runs, notes = [], []
    peak_disk = peak_ram = 0.0
    for run in sorted(runs_in, key=lambda r: flt.is_narrowband(r.get("filter_kind") or "")):
        rid = _run_id(run)
        lights = _lights(db, run["light_ids"])
        is_osc = bool(run.get("bayer"))
        proc = flt.processing_for(run.get("filter_kind") or flt.UNKNOWN, is_osc)
        # Dualband/Schmalband an Farbkamera: WBPP 3.2.0 trennt die Kanäle selbst und registriert jeden einzeln
        # (getrenntes Debayern; in 2.7.8 war das im Automatik-Modus defekt, siehe ERKENNTNISSE.md).
        mode = "separated" if (is_osc and proc.mode == "extract") else \
            "debayer" if (is_osc and proc.mode == "debayer") else proc.mode
        w = run["settings"].get("width") or 6000
        h = run["settings"].get("height") or 4000
        n = len(lights)
        nights = sorted({x["night"] for x in lights if x["night"]})
        stats = light_stats(lights)
        ref_id, ref_why = best_reference(lights)
        has_darks = bool(run["darks"]["file_ids"])
        fwhm_med = stats["fwhm_med"] if stats else None
        stars_med = stats["nstars_med"] if stats else None
        hot_med = _med([x["hot"] for x in lights])
        rej, rej_why = rejection_for(n)
        s = []                                   # (Schlüssel, Wert, Grund)
        s.append(("fast_integration", False, "Volle Integration in bester Qualität. WBPP würde Gruppen ab 200 "
                  "Lights sonst still auf Fast Integration umschalten."))
        s.append(("rejection", rej, rej_why))
        s.append(("large_scale_rejection", True, "Entfernt Satelliten- und Flugzeugspuren."))
        s.append(("weighting", "psf_signal", "PSF Signal Weight: Frames mit mehr Signal und schärferen Sternen "
                  "zählen mehr; Mondnächte werden so automatisch schwächer gewichtet."))
        ln = len(nights) > 1
        s.append(("local_normalization", ln, ("%d Nächte mit unterschiedlichem Hintergrund." % len(nights)) if ln
                  else "Nur eine Nacht – kein Ausgleich nötig."))
        dc = stars_med is None or stars_med >= FEW_STARS
        s.append(("distortion_correction", dc,
                  "Großes Bildfeld: Sterne am Rand leicht verzerrt." if dc else
                  "Nur ca. %d Sterne je Frame – zu wenige Stützpunkte für eine Verzerrungskorrektur." % stars_med))
        cc = (not has_darks) and (hot_med is None or hot_med >= HOT_PIXELS_CC)
        s.append(("cosmetic_correction", cc,
                  "Keine Darks: Hotpixel werden automatisch erkannt und entfernt (sonst richtet PixInsight "
                  "an Hotpixeln statt an Sternen aus)." if cc else
                  ("Darks vorhanden – sie entfernen die Hotpixel." if has_darks else
                   "Kaum Hotpixel (%d je Frame)." % (hot_med or 0))))
        driz = bool(fwhm_med and fwhm_med <= DRIZZLE_MAX_SAMPLING)
        s.append(("drizzle", driz, ("FWHM %.1f px: unterabgetastet, Drizzle 2× holt Details heraus." % fwhm_med)
                  if driz else ("FWHM %.1f px: ausreichend abgetastet, Drizzle bringt nichts." % fwhm_med)
                  if fwhm_med else "Keine FWHM-Daten."))
        s.append(("autocrop", False, "Masters bleiben gleich groß und deckungsgleich; Zuschneiden in der Bearbeitung."))
        sd = star_detection(stats)
        if sd:
            s.append(("star_detection", sd["params"], "Sternerkennung angepasst: " + sd["why"] + "."))
        run_edit = (edits.get("runs") or {}).get(rid) or {}
        settings = []
        for key, value, why in s:
            user = run_edit.get(key, opt_edit.get(key))
            changed = user is not None and user != value
            settings.append(dict(key=key, value=user if changed else value, auto=value, why=why, edited=changed))
        disk = system.estimate_disk_gb(n, w, h, is_osc, mode)
        ram = system.estimate_ram_gb(n, w, h, 3 if mode == "debayer" else 1)
        enabled = run_edit.get("enabled", n >= MIN_LIGHTS and not run["problems"])
        why_off = None
        if n < MIN_LIGHTS:
            why_off = "Nur %d Light%s – mindestens %d nötig." % (n, "" if n == 1 else "s", MIN_LIGHTS)
        elif run["problems"]:
            why_off = "; ".join(run["problems"])
        if enabled:
            peak_disk, peak_ram = max(peak_disk, disk), max(peak_ram, ram)
        runs.append(dict(
            id=rid, name=run["filter"] or "?", filter=run["filter"], filter_kind=run.get("filter_kind"),
            mode=proc.mode, stack_mode=mode, outputs=list(proc.outputs), channel_map=proc.channel_map,
            processing=proc.note, lights=n, light_ids=run["light_ids"], hours=round(run["exposure_s"] / 3600, 2),
            nights=nights, reference_id=ref_id, reference_why=ref_why,
            flats=[dict(night=x["night"], file_ids=x["flats"]["file_ids"], reason=x["flats"]["reason"],
                        verdict=(x["flats"].get("quality") or {}).get("verdict")) for x in run["nights"]],
            bias=run["bias"], darks=run["darks"], problems=run["problems"], settings=settings,
            enabled=bool(enabled), why_off=why_off, disk_gb=round(disk, 1), ram_gb=round(ram, 1),
            light_stats=stats))
    names = [r["name"] for r in runs]
    for r in runs:                                              # gleicher Filter, andere Einstellungen
        if names.count(r["name"]) > 1:
            r["name"] = "%s (Gain %s)" % (r["name"], r["id"].split("|")[2])
    first = next((r for r in runs if r["enabled"] and r["mode"] in ("debayer", "mono")), None)
    for r in runs:
        r["align_to"] = None if (first is None or r is first) else first["id"]
    if first and len([r for r in runs if r["enabled"]]) > 1:
        notes.append("Alle Läufe werden auf das Master von %s ausgerichtet – die Ergebnisse liegen pixelgenau "
                     "übereinander." % first["name"])
    free_disk = system.free_disk_gb(workdir)
    free_ram, total_ram = system.ram_gb()
    return dict(version=PLAN_VERSION, created=dbm.now(), runs=runs, notes=notes,
                need_gb=round(peak_disk + 2, 1) if runs else 0, free_gb=None if free_disk is None else round(free_disk, 1),
                need_ram_gb=round(peak_ram, 1), free_ram_gb=None if not free_ram else round(free_ram, 1),
                total_ram_gb=None if not total_ram else round(total_ram, 1),
                ready=any(r["enabled"] for r in runs))


def update_plan(db: dbm.Database, pid: int, workdir: str, progress=None) -> dict:
    """Plan neu berechnen (mit den gespeicherten Änderungen) und speichern."""
    if progress:
        progress(step="Plan aktualisieren")
    edits = load_edits(db, pid)
    plan = build_plan(db, pid, workdir, edits)
    with db.transaction():
        db.execute("INSERT INTO plans (project_id, version, created_at, plan_json) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (project_id) DO UPDATE SET version = excluded.version, created_at = excluded.created_at, "
                   "plan_json = excluded.plan_json", (pid, PLAN_VERSION, plan["created"], json.dumps(plan)))
    return plan


def load_plan(db: dbm.Database, pid: int) -> dict | None:
    t = db.value("SELECT plan_json FROM plans WHERE project_id = ?", (pid,))
    return json.loads(t) if t else None


def load_edits(db: dbm.Database, pid: int) -> dict:
    t = db.value("SELECT edits_json FROM plans WHERE project_id = ?", (pid,))
    return json.loads(t) if t else {}


def edit_plan(db: dbm.Database, pid: int, workdir: str, options: dict | None = None,
              runs: dict | None = None, reset: bool = False) -> dict:
    """Änderungen des Nutzers speichern (``None`` als Wert = zurück zur Automatik) und den Plan neu berechnen."""
    known = set(DEFAULT_OPTIONS) | {"star_detection", "enabled"}
    edits = {} if reset else load_edits(db, pid)
    for scope, data in (("options", options), ("runs", runs)):
        if not data:
            continue
        target = edits.setdefault(scope, {})
        items = data.items() if scope == "options" else ((rid, ch) for rid, ch in data.items())
        for k, v in items:
            if scope == "options":
                if k not in known:
                    raise ValueError("unbekannte Einstellung: %s" % k)
                if v is None:
                    target.pop(k, None)
                else:
                    target[k] = v
            else:
                run = target.setdefault(k, {})
                for kk, vv in (v or {}).items():
                    if kk not in known:
                        raise ValueError("unbekannte Einstellung: %s" % kk)
                    if vv is None:
                        run.pop(kk, None)
                    else:
                        run[kk] = vv
                if not run:
                    target.pop(k)
    now = dbm.now()
    with db.transaction():
        db.execute("INSERT INTO plans (project_id, version, created_at, plan_json, edits_json) VALUES (?, ?, ?, '{}', ?) "
                   "ON CONFLICT (project_id) DO UPDATE SET edits_json = excluded.edits_json",
                   (pid, PLAN_VERSION, now, json.dumps(edits)))
        db.event("plan_edited", pid, json.dumps(edits))
    return update_plan(db, pid, workdir)


def channels(run: dict) -> set[str]:
    """Welche Bayer-Kanäle ein getrennt debayerter Lauf braucht (z. B. HaO3: R, G, B; Ha-Filter: nur R)."""
    out = set()
    for src in (run.get("channel_map") or {}).values():
        out |= {c for c in src if c in "RGB"}
    return out or {"R", "G", "B"}


def wbpp_values(run: dict) -> dict:
    """Die Einstellungen eines Laufs als WBPP-Parameter (Namen und Werte wie in BPP-automation.js)."""
    v = {s["key"]: s["value"] for s in run["settings"]}
    p = {
        "autoIntegrationMode": False,
        "localNormalization": bool(v.get("local_normalization")),
        "distortionCorrection": bool(v.get("distortion_correction")),
        "autocrop": bool(v.get("autocrop")),
        "lightsLargeScaleRejectionHigh": bool(v.get("large_scale_rejection", True)),
        "subframeWeightingEnabled": True,
        "subframesWeightsMethod": WEIGHTS.get(v.get("weighting", "psf_signal"), 0),
        "rejection_4": REJECTION.get(v.get("rejection") or "auto", 5),
        "imageRegistration": True,
        "integrate": True,
        "platesolve": False,
        "generateRejectionMaps": True,
        "debayerOutputMethod": 0,
    }
    if run.get("stack_mode") == "separated":
        ch = channels(run)
        p.update(debayerOutputMethod=1, recombineRGB=False, debayerActiveChannelR="R" in ch,
                 debayerActiveChannelG="G" in ch, debayerActiveChannelB="B" in ch)
    if v.get("fast_integration"):
        p["autoIntegrationMode"] = True              # bewusste Wahl für einen schnellen Probe-Stack
    for k, val in (v.get("star_detection") or {}).items():
        p[k] = val
    return p


def explain(plan: dict) -> list[str]:
    """Kurzfassung in Klartext (für Bericht, CLI und KI)."""
    out = []
    for r in plan.get("runs", []):
        state = "an" if r["enabled"] else "aus (%s)" % (r["why_off"] or "abgeschaltet")
        out.append("%s: %d Lights, %.1f h, %d Nächte – %s" % (r["name"], r["lights"], r["hours"], len(r["nights"]),
                                                              state))
    if plan.get("free_gb") is not None and plan["need_gb"] > plan["free_gb"]:
        out.append("Zu wenig Platz: %.1f GB nötig, %.1f GB frei." % (plan["need_gb"], plan["free_gb"]))
    return out
