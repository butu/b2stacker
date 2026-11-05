"""Auswertungen über alle Lights eines Projekts – wie im Bericht aus 0.10, aber aus der Datenbank.

- Nachtübersicht (je Nacht und Filter: Stunden, genutzt, effektiv, FWHM, Mond, Note A/B/C)
- Rig-Hinweise (Schärfe, Nachführung, Fokus, Dithering, Kühlung, Taupunkt, Offset, Belichtung)
- Himmelsbahnen (Objekt, Mond, Sonne je Nacht) und Kennzahlen je Filter
- der ausführliche Bericht als eigenständige HTML-Seite (Vorlage ``report/template.html``)

Die Rechnung dauert bei vielen Nächten ein paar Sekunden (Himmelsbahnen); das Ergebnis wird je Projekt
zwischengespeichert, bis sich Messwerte oder Entscheidungen ändern.
"""
from __future__ import annotations

import datetime
import os
import statistics as st
import threading

from .. import db as dbm
from ..report import assess as legacy_assess
from .assess import _rows
from .headers import meta_from_json
from .measure import THUMBS

_CACHE: dict = {}
_LOCK = threading.Lock()


def _key(db: dbm.Database, pid: int):
    a = db.one("SELECT COUNT(*) n, MAX(m.measured_at) t FROM project_files pf JOIN frame_metrics m "
               "ON m.file_id = pf.file_id WHERE pf.project_id = ?", (pid,))
    b = db.one("SELECT COUNT(*) n, MAX(decided_at) t, SUM(file_id * (CASE status WHEN 'use' THEN 1 WHEN 'skip' "
               "THEN 3 ELSE 7 END)) h FROM file_status WHERE project_id = ?", (pid,))
    return (pid, a["n"], a["t"], b["n"], b["t"], b["h"])


def _overrides(db: dbm.Database, pid: int, rows: list[dict]) -> dict:
    """Die Entscheidungen des Projekts (auch von Hand) gelten auch für die Auswertung. → {pfad: in|out}"""
    st_ = {int(r["file_id"]): r["status"] for r in db.query(
        "SELECT file_id, status FROM file_status WHERE project_id = ?", (pid,))}
    out = {}
    for r in rows:
        s = st_.get(r["id"])
        if s in ("skip", "delete"):
            out[r["path"]] = "out"
        elif s == "use":
            out[r["path"]] = "in"
    return out


def _thumb(ws, row) -> str:
    return os.path.join(ws.previews, THUMBS, (row.get("sha256") or "")[:20] + ".npy")


def _compute(db: dbm.Database, pid: int, ws) -> dict:
    rows, unusable = _rows(db, pid)
    if not rows:
        return dict(rows=[], nights=[], filters=[], kpi=[], diag=None, sky=None, rec=[], meta={}, unusable=len(unusable),
                    diag_note="Noch keine vermessenen Lights.")
    for r in rows:                                 # Felder, die der alte Bericht aus metrics.csv kannte
        r.setdefault("frame", os.path.basename(r["path"]))
        r.setdefault("file", os.path.basename(r["path"]))
        r.setdefault("pier", None)
    filters, ntab = legacy_assess.assess(rows, overrides=_overrides(db, pid, rows))
    rec = legacy_assess.recommendations(rows, ntab, filters)
    scales = [r["pixscale"] for r in rows if r.get("pixscale")]
    scale = st.median(scales) if scales else None
    diag, diag_note = None, None
    try:
        from ..report.diag import rig_diagnostics
        diag = rig_diagnostics(rows, scale, lambda r: _thumb(ws, r))
    except Exception as e:  # noqa: BLE001 – Hinweise dürfen die Übersicht nicht verhindern
        import logging
        logging.getLogger(__name__).exception("Rig-Hinweise")
        diag_note = ("Die Rig-Hinweise ließen sich nicht berechnen (%s: %s). Das ist ein Fehler in b2stacker – bitte "
                     "melden; die übrige Auswertung stimmt." % (type(e).__name__, e))
    if diag is None and diag_note is None:
        no_night = sum(1 for r in rows if not r.get("night"))
        no_tiles = sum(1 for r in rows if not r.get("tiles_fwhm"))
        if no_tiles:
            diag_note = ("Für %d von %d Lights fehlen die Messwerte über das Bildfeld (Schärfe in den Ecken, Sternform) – "
                         "sie stammen aus einer älteren Version. „Neu prüfen“ misst sie nach." % (no_tiles, len(rows)))
        elif no_night:
            diag_note = ("%d von %d Lights haben keine Nacht (Aufnahmezeit oder Standort fehlt im Header). Unter "
                         "Einstellungen einen Ersatz-Standort eintragen und „Neu prüfen“." % (no_night, len(rows)))
        else:
            diag_note = "Zu wenige verwendete Lights für Rig-Hinweise."
    first = db.one("SELECT f.meta_json FROM files f JOIN project_files pf ON pf.file_id = f.id WHERE pf.project_id = ? "
                   "AND f.type = 'light' AND f.meta_json IS NOT NULL AND f.missing_since IS NULL ORDER BY f.date_obs "
                   "LIMIT 1", (pid,))
    meta0 = meta_from_json(first["meta_json"]) if first else None
    sky = None
    if meta0 is not None:
        try:
            from ..report.sky import sky_tracks
            sky = sky_tracks(rows, meta0)
        except Exception:  # noqa: BLE001 – ohne Standort/Koordinaten keine Bahnen
            sky = None
    kpi = []
    for f in filters:
        nf = [n for n in ntab if n["filter"] == f]
        kpi.append(dict(filter=f, used_h=sum(n["used_h"] for n in nf), total_h=sum(n["hours"] for n in nf),
                        eff_h=sum(n["eff_h"] for n in nf), n_used=sum(n["n_used"] for n in nf),
                        n=sum(n["n"] for n in nf), nights=len(nf)))
    nights_sorted = sorted(n for n in {r["night"] for r in rows} if n)
    target = db.value("SELECT name FROM projects WHERE id = ?", (pid,)) or ""
    meta = dict(target=target, generated=datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
                camera=" · ".join(str(x) for x in (
                    meta0 and meta0.camera, meta0 and meta0.telescope,
                    meta0 and meta0.focal_length and "%.0f mm" % meta0.focal_length,
                    meta0 and meta0.gain is not None and "Gain %g" % meta0.gain) if x),
                scale=round(scale, 3) if scale else None,
                first=nights_sorted[0] if nights_sorted else "", last=nights_sorted[-1] if nights_sorted else "")
    return dict(rows=rows, nights=ntab, filters=filters, kpi=kpi, diag=diag, sky=sky, rec=rec, meta=meta,
                unusable=len(unusable), diag_note=diag_note)


def _cached(db: dbm.Database, pid: int, ws) -> dict:
    key = _key(db, pid)
    with _LOCK:
        hit = _CACHE.get(pid)
        if hit and hit[0] == key:
            return hit[1]
    data = _compute(db, pid, ws)
    with _LOCK:
        _CACHE[pid] = (key, data)
    return data


def _night_status(n: dict) -> dict:
    """Bewertung einer Nacht in Klartext: was, warum, was tun."""
    used = n["n_used"]
    if not used:
        why, todo = "Alle Frames dieser Nacht sind aussortiert.", "Nichts zu tun – die Nacht fließt nicht ein."
    elif n["grade"] == "A":
        why, todo = "Viel effektives Signal je Stunde – gehört zu den besten Nächten.", "Nichts zu tun."
    elif n["grade"] == "B":
        why = "Mittleres Signal je Stunde" + (" – Mond am Himmel" if n["moon"] else "") + "."
        todo = "Bleibt drin; die Gewichtung gibt ihr weniger Einfluss."
    else:
        why = "Wenig Signal je Stunde" + (" – Mond am Himmel" if n["moon"] else " – Dunst, Wolken oder heller Himmel") + "."
        todo = "Bleibt drin, zählt aber kaum. Im Frames-Tab nach dieser Nacht filtern und prüfen."
    return dict(why=why, todo=todo)


def build(db: dbm.Database, pid: int, ws) -> dict:
    d = _cached(db, pid, ws)
    nights = [dict({k: v for k, v in n.items() if k != "alt"}, alt_min=(n.get("alt") or (None, None))[0],
                   alt_max=(n.get("alt") or (None, None))[1], **_night_status(n)) for n in d["nights"]]
    diag = d["diag"] or {}
    return dict(meta=d["meta"], kpi=d["kpi"], nights=nights, checks=diag.get("checks") or [], rec=d["rec"],
                sky=d["sky"] or [], focus=diag.get("focus"), dither=diag.get("dither"), unusable=d["unusable"],
                diag_note=d.get("diag_note"),
                frames=len(d["rows"]))


def report_html(db: dbm.Database, pid: int, ws) -> str:
    from ..report import build as rb
    d = _cached(db, pid, ws)
    frames = []
    for r in d["rows"]:
        img = ""
        p = _thumb(ws, r)
        if os.path.exists(p):
            try:
                img = rb.thumb_b64(os.path.dirname(os.path.dirname(p)), r["path"], width=320, npy=p)
            except Exception:  # noqa: BLE001
                img = ""
        frames.append({k: r.get(k) for k in ("night", "filter", "t", "exp", "fwhm", "fwhm_as", "ecc", "nstars", "bg",
                                             "noise", "alt", "moon_illum", "moon_alt", "temp", "status", "auto_status",
                                             "weight", "path")}
                      | dict(file=os.path.basename(r["path"]), frame=os.path.basename(r["path"]), id=r["id"],
                             override=None, img=img, flags=[f[1] for f in r.get("flags") or []]))
    data = dict(meta=d["meta"], filters=d["filters"], kpi=d["kpi"], nights=d["nights"], checks=[], rec=d["rec"],
                plan=None, sky=d["sky"], diag=d["diag"], frames=frames)
    return rb._render(data, d["meta"].get("target") or "Projekt")


def forget(pid: int | None = None) -> None:
    with _LOCK:
        if pid is None:
            _CACHE.clear()
        else:
            _CACHE.pop(pid, None)
