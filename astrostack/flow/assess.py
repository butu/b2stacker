"""Schritt 4 (Teil Lights): bewerten und automatisch aussortieren – jede Entscheidung mit Grund in Klartext.

Nutzt die erprobte Bewertung ``report.assess`` (Wolken, Hintergrund, Unschärfe je Nacht und Filter, Duplikate
über die Prüfsumme). Ergebnis landet in ``file_status`` mit ``origin = 'auto'``. Manuelle Entscheidungen
(``origin = 'manual'``) werden nie überschrieben. Läuft nur auf der Datenbank, ohne eine Datei zu lesen.
"""
from __future__ import annotations

import json

from .. import db as dbm
from ..report import assess as legacy_assess

ASSESS_VERSION = 1


def _rows(db: dbm.Database, pid: int) -> tuple[list[dict], list]:
    sql = ("SELECT f.id, f.path, f.checksum, f.excluded_dir, f.type, f.header_error, f.night, f.filter, "
           "f.exposure, m.data_json FROM files f JOIN project_files pf ON pf.file_id = f.id "
           "LEFT JOIN frame_metrics m ON m.file_id = f.id "
           "WHERE pf.project_id = ? AND f.missing_since IS NULL AND f.type = 'light'")
    rows, unusable = [], []
    for r in db.query(sql, (pid,)):
        if r["header_error"] or not r["data_json"]:
            unusable.append(r)
            continue
        d = json.loads(r["data_json"])
        for k in legacy_assess.NUM:
            d[k] = legacy_assess.fnum(d.get(k))
        d.update(id=int(r["id"]), path=r["path"], sha256=r["checksum"] or "", excluded=bool(r["excluded_dir"]),
                 night=d.get("night") or r["night"], filter=d.get("filter") or r["filter"],
                 t=d.get("date_obs") or "")
        d["fwhm_as"] = round(d["fwhm"] * d["pixscale"], 2) if (d.get("fwhm") and d.get("pixscale")) else None
        rows.append(d)
    rows.sort(key=lambda x: x["t"])
    return rows, unusable


def assess_project(db: dbm.Database, pid: int, progress=None) -> dict:
    """Lights bewerten und automatische Entscheidungen speichern. → Zähler und Nacht-Tabelle"""
    if progress:
        progress(step="Frames bewerten")
    rows, unusable = _rows(db, pid)
    filters, ntab = legacy_assess.assess(rows) if rows else ([], [])
    manual = {int(r[0]) for r in db.query(
        "SELECT file_id FROM file_status WHERE project_id = ? AND origin = 'manual'", (pid,))}
    now = dbm.now()
    res = dict(use=0, skip=0, warn=0, unusable=len(unusable), manual=len(manual))
    with db.transaction():
        for r in rows:
            if r["id"] in manual:
                continue
            rejects = [t for k, t in r["flags"] if k == "reject"]
            warns = [t for k, t in r["flags"] if k == "warn"]
            if r["status"] in ("rejected", "suggest"):
                status = "skip"
                reason = "; ".join(rejects) or "liegt in einem Ordner für aussortierte Frames"
                res["skip"] += 1
            else:
                status, reason = "use", "; ".join(warns)
                res["use"] += 1
                res["warn"] += 1 if warns else 0
            _put(db, pid, r["id"], status, reason, now)
        for r in unusable:
            if int(r["id"]) in manual:
                continue
            reason = r["header_error"] or "noch nicht vermessen"
            _put(db, pid, int(r["id"]), "skip", "nicht verwendbar: " + reason, now)
    res["nights"] = [dict(n, alt=list(n["alt"])) for n in ntab]
    res["filters"] = filters
    return res


def _put(db: dbm.Database, pid: int, fid: int, status: str, reason: str, now: str) -> None:
    db.execute("INSERT INTO file_status (project_id, file_id, status, origin, reason, decided_at) "
               "VALUES (?, ?, ?, 'auto', ?, ?) ON CONFLICT (project_id, file_id) DO UPDATE SET "
               "status = excluded.status, reason = excluded.reason, decided_at = CASE "
               "WHEN file_status.status != excluded.status OR file_status.reason != excluded.reason "
               "THEN excluded.decided_at ELSE file_status.decided_at END "
               "WHERE file_status.origin = 'auto'", (pid, fid, status, reason, now))


def decide(db: dbm.Database, pid: int, file_ids: list[int], decision: str, reason: str = "") -> int:
    """Manuelle Entscheidung: ``use``, ``skip`` oder ``auto`` (zurück zur Automatik). → Anzahl geänderter"""
    if decision not in ("use", "skip", "auto"):
        raise ValueError("Entscheidung muss use, skip oder auto sein")
    n = 0
    now = dbm.now()
    with db.transaction():
        for fid in file_ids:
            if not db.one("SELECT 1 FROM project_files WHERE project_id = ? AND file_id = ?", (pid, fid)):
                continue
            if decision == "auto":
                cur = db.execute("DELETE FROM file_status WHERE project_id = ? AND file_id = ? AND origin = 'manual'",
                                 (pid, fid))
            else:
                text = reason or ("manuell verwendet" if decision == "use" else "manuell aussortiert")
                cur = db.execute(
                    "INSERT INTO file_status (project_id, file_id, status, origin, reason, decided_at) "
                    "VALUES (?, ?, ?, 'manual', ?, ?) ON CONFLICT (project_id, file_id) DO UPDATE SET "
                    "status = excluded.status, origin = 'manual', reason = excluded.reason, "
                    "decided_at = excluded.decided_at", (pid, fid, decision, text, now))
            n += cur.rowcount
        if n:
            db.event("frames_decided", pid, json.dumps({"n": n, "decision": decision}))
    return n
