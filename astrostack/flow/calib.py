"""Schritt 4 (Teil Kalibrierung): Läufe bilden und Kalibrierframes zuordnen – nur über Header, nie über Ordnernamen.

Ein **Lauf** sind alle verwendeten Lights mit gleichem Filter und gleichen Kamera-Einstellungen (Setup, Gain,
Offset, Auslesemodus, Binning, Bildgröße, Bayer-Muster). Pro Lauf und Nacht wird ein Flat-Satz gewählt,
pro Lauf ein Bias- und ggf. ein Dark-Satz. Jede Wahl und jedes Fehlen hat einen Grund in Klartext.

Regeln (aus der Praxis, siehe docs/ERKENNTNISSE.md):
- Flats: gleicher Filter (RGB-Lights nur mit RGB-Flats), gleiche Einstellungen, möglichst dieselbe Nacht,
  sonst die zeitlich nächste. Der Abstand ist kein Befund (über ``FLAT_WARN_DAYS`` nur eine leise Zeile).
  Qualität aus den gespeicherten Messwerten, mit Befund in Klartext.
- Bias: gleiche Einstellungen, der neueste Satz.
- Darks: gleiche Einstellungen, Belichtung ±1 s, Temperatur ±2 °C.
- Master-Dateien zählen als vollständiger Satz.
"""
from __future__ import annotations

import datetime
import json
from collections import defaultdict

from .. import calibcheck
from .. import db as dbm
from ..fmt import dm

FLAT_WARN_DAYS = 14               # bis 14 Tage Abstand nichts; darüber höchstens ein Hinweis, nie ein Fehler
DARK_EXP_TOL = 1.0
DARK_TEMP_TOL = 2.0
MIN_FRAMES = 3                     # WBPP braucht mindestens 3 Rohframes für einen Master

SETTINGS = ("setup_id", "gain", "offset", "readout", "binning", "width", "height")


def _key(r, cols=SETTINGS) -> tuple:
    out = []
    for c in cols:
        v = r[c]
        out.append((v or "").strip().lower() if c == "readout" else v)
    return tuple(out)


def _same_settings(a, b) -> list[str]:
    """Unterschiede in Klartext (leer = passt). Unbekannte Werte (None) gelten als passend."""
    names = dict(setup_id="Kamera/Teleskop", gain="Gain", offset="Offset", readout="Auslesemodus",
                 binning="Binning", width="Bildgröße", height="Bildgröße")
    diff = []
    for c in SETTINGS:
        va, vb = a[c], b[c]
        if c == "readout":
            va, vb = (va or "").strip().lower() or None, (vb or "").strip().lower() or None
        if va is not None and vb is not None and va != vb and names[c] not in diff:
            diff.append(names[c])
    return diff


def _day(s: str | None):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _flat_sets(rows) -> dict:
    """Flats nach (Filter, Einstellungen, Nacht) gruppieren."""
    sets = defaultdict(list)
    for r in rows:
        sets[((r["filter"] or "").lower(), _key(r), r["night"] or "")].append(r)
    return sets


def _flat_quality(frames, th) -> dict:
    stats = {}
    for f in frames:
        if f["data_json"]:
            d = json.loads(f["data_json"])
            d["path"] = f["path"]
            stats[f["path"]] = d
    if not stats:
        return dict(verdict="unknown", findings=[])

    class _Set:
        pass

    cs = _Set()
    cs.frames = [type("F", (), dict(path=f["path"], bayer=f["bayer"], excluded=False))() for f in frames
                 if f["path"] in stats]
    q = calibcheck.check_flat_set(cs, th, stats_cache=stats)
    q.pop("frames", None)
    return q


def _label(frames) -> str:
    masters = sum(1 for f in frames if f["is_master"])
    if masters:
        return "Master"
    return "%d Frames" % len(frames)


def match_project(db: dbm.Database, pid: int, th: calibcheck.Thresholds | None = None) -> list[dict]:
    """Läufe des Projekts mit zugeordneten Kalibriersätzen. Liest keine Datei."""
    th = th or calibcheck.Thresholds()
    base = ("SELECT f.id, f.path, f.type, f.is_master, f.filter, f.filter_kind, f.exposure, f.ccd_temp, f.night, f.date_obs, "
            "f.bayer, " + ", ".join("f.\"%s\"" % c for c in SETTINGS) + ", m.data_json, "
            "COALESCE(s.status, 'use') AS status FROM files f JOIN project_files pf ON pf.file_id = f.id "
            "LEFT JOIN frame_metrics m ON m.file_id = f.id "
            "LEFT JOIN file_status s ON s.file_id = f.id AND s.project_id = pf.project_id "
            "WHERE pf.project_id = ? AND f.missing_since IS NULL AND f.header_error IS NULL ")
    rows = db.query(base + "ORDER BY f.date_obs, f.path", (pid,))
    lights = [r for r in rows if r["type"] == "light" and r["status"] == "use"]
    calib = [r for r in rows if r["type"] in ("flat", "bias", "dark", "darkflat") and r["status"] == "use"]
    flats = _flat_sets([r for r in calib if r["type"] == "flat"])
    runs = defaultdict(list)
    for r in lights:
        runs[((r["filter"] or "").lower(), _key(r), r["bayer"] or "")].append(r)
    out = []
    for (flt, settings, _bayer), frames in sorted(runs.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        ref = frames[0]
        nights = defaultdict(list)
        for f in frames:
            nights[f["night"] or ""].append(f)
        run = dict(filter=ref["filter"], filter_kind=ref["filter_kind"], bayer=ref["bayer"],
                   settings=dict(zip(SETTINGS, settings)), lights=len(frames),
                   exposure_s=sum(f["exposure"] or 0 for f in frames), nights=[], light_ids=[f["id"] for f in frames],
                   problems=[])
        for night, nf in sorted(nights.items()):
            run["nights"].append(dict(night=night, lights=len(nf), flats=_pick_flats(flt, settings, night, ref,
                                                                                    flats, th)))
        run["flat_options"] = flat_options(flats, ref, th)
        run["bias"] = _pick_simple(calib, "bias", ref, None)
        run["darks"] = _pick_simple(calib, "dark", ref, frames)
        for n in run["nights"]:
            if not n["flats"]["file_ids"]:
                run["problems"].append("Nacht %s: %s" % (dm(n["night"]), n["flats"]["reason"]))
        if not run["bias"]["file_ids"] and not run["darks"]["file_ids"]:
            run["problems"].append(run["bias"]["reason"])
        out.append(run)
    return out


def _pick_flats(flt, settings, night, ref, flats, th) -> dict:
    exact = flats.get((flt, settings, night))
    if exact:
        return _flat_choice(exact, "Flats derselben Nacht", th)
    cands = []
    nd = _day(night)
    for (f2, s2, n2), frames in flats.items():
        if f2 != flt:
            continue
        if _same_settings(ref, frames[0]):
            continue
        d2 = _day(n2)
        if nd and d2:
            cands.append((abs((d2 - nd).days), n2, frames))
    if not cands:
        other = [k for k in flats if k[0] != flt]
        reason = "keine Flats mit Filter %s" % (ref["filter"] or "–")
        if any(k[0] == flt for k in flats):
            reason = "Flats mit Filter %s haben andere Kamera-Einstellungen" % (ref["filter"] or "–")
        elif other:
            reason += " (andere Filter gibt es, passen aber nicht)"
        return dict(file_ids=[], night=None, reason=reason, quality=None)
    days, n2, frames = min(cands, key=lambda c: (c[0], c[1]))
    ch = _flat_choice(frames, "Flats vom %s (%d Tag%s entfernt)" % (dm(n2), days, "" if days == 1 else "e"), th)
    ch["days"] = days
    # Abstand ist kein Befund (Feedback 0.13.1: „Ruhe, wenn alles passt“, nichts abhaken müssen). Über
    # FLAT_WARN_DAYS steht nur eine leise Zeile in der Kalibrierung, nie eine Warnung oder ein Knopf.
    ch["far"] = days > FLAT_WARN_DAYS
    return ch


def _flat_choice(frames, why, th) -> dict:
    q = _flat_quality(frames, th)
    usable = len(frames) >= MIN_FRAMES or any(f["is_master"] for f in frames)
    reason = why + ", " + _label(frames)
    hints = [dict(level=f.get("level"), title=f.get("title"), text=f.get("text")) for f in (q or {}).get("findings") or []
             if f.get("level") in ("warning", "critical")]
    if not usable:
        reason += " – zu wenige (WBPP braucht mindestens %d)" % MIN_FRAMES
    return dict(file_ids=[f["id"] for f in frames] if usable else [], night=frames[0]["night"], reason=reason,
                quality=q, frames=len(frames), hints=hints)


def flat_options(flats: dict, ref, th=None) -> list[dict]:
    """Alle Flat-Sätze, die man für diesen Lauf von Hand wählen kann (gleiche Kamera-Einstellungen), passender
    Filter zuerst – mit Qualität, damit auch eine Wahl von Hand ihre Befunde (als Hinweis) zeigt."""
    out = []
    for (f2, _s2, n2), frames in flats.items():
        if _same_settings(ref, frames[0]):
            continue
        q = _flat_quality(frames, th or calibcheck.Thresholds())
        out.append(dict(key="%s|%s" % (f2, n2), filter=frames[0]["filter"], night=n2, n=len(frames),
                        ids=[f["id"] for f in frames], master=any(f["is_master"] for f in frames),
                        findings=[dict(level=f.get("level"), title=f.get("title"), text=f.get("text"))
                                  for f in (q or {}).get("findings") or [] if f.get("level") in ("warning", "critical")]))
    flt = (ref["filter"] or "").lower()
    out.sort(key=lambda o: ((o["filter"] or "").lower() != flt, o["night"]), reverse=False)
    return out


def _pick_simple(calib, kind, ref, lights) -> dict:
    name = {"bias": "Bias", "dark": "Darks"}[kind]
    cands = [r for r in calib if r["type"] == kind and not _same_settings(ref, r)]
    if kind == "dark" and lights is not None:
        exp = lights[0]["exposure"] or 0
        temp = lights[0]["ccd_temp"]
        cands = [r for r in cands if abs((r["exposure"] or 0) - exp) <= DARK_EXP_TOL and
                 (temp is None or r["ccd_temp"] is None or abs(r["ccd_temp"] - temp) <= DARK_TEMP_TOL)]
    if not cands:
        if kind == "dark":
            return dict(file_ids=[], reason="keine passenden Darks – wird ohne Darks gestackt")
        return dict(file_ids=[], reason="keine Bias mit passenden Einstellungen")
    masters = [r for r in cands if r["is_master"]]
    if masters:
        newest = max(masters, key=lambda r: r["date_obs"] or "")
        return dict(file_ids=[newest["id"]], reason="Master-%s %s" % (name, dm(newest["night"]) if newest["night"] else ""))
    by_night = defaultdict(list)
    for r in cands:
        by_night[r["night"] or ""].append(r)
    night, frames = max(by_night.items(), key=lambda kv: (len(kv[1]) >= MIN_FRAMES, kv[0]))
    if len(frames) < MIN_FRAMES:
        return dict(file_ids=[], reason="nur %d %s – WBPP braucht mindestens %d" % (len(frames), name, MIN_FRAMES))
    return dict(file_ids=[r["id"] for r in frames], reason="%s vom %s, %d Frames" % (name, dm(night), len(frames)))
