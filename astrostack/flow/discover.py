"""Schritt 1: Entdecken – welche Bilddateien liegen in den Quellordnern eines Projekts?

Öffnet keine Datei: nur Pfad, Größe und Änderungszeit. Neue Dateien werden angelegt, geänderte für die
folgenden Schritte zurückgesetzt, verschwundene als „Datei fehlt“ markiert (nie gelöscht – Entscheidungen
bleiben erhalten). Nicht erreichbare Quellordner (NAS aus, Platte getrennt) werden als offline markiert und
übersprungen; ihre Dateien bleiben unverändert.
"""
from __future__ import annotations

import os

from .. import db as dbm
from .. import discover as legacy_discover

CHUNK = 500


def _scan_options():
    return legacy_discover.ScanOptions()


def _stat(path: str):
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def discover_folder(db: dbm.Database, pid: int, folder_id: int, path: str, progress=None, cancel=None) -> dict:
    res = dict(folder=path, online=True, files=0, new=0, changed=0, missing=0, back=0, new_ids=[])
    if not os.path.isdir(path):
        with db.transaction():
            db.execute("UPDATE source_folders SET online = 0 WHERE id = ?", (folder_id,))
        res["online"] = False
        return res
    found = legacy_discover.find_files([path], _scan_options())
    res["files"] = len(found)
    known = {r["path"]: r for r in db.query(
        "SELECT f.id, f.path, f.size, f.mtime_ns, f.missing_since, f.excluded_dir, pf.file_id IS NOT NULL AS linked "
        "FROM files f LEFT JOIN project_files pf ON pf.file_id = f.id AND pf.project_id = ? "
        "WHERE f.path >= ? AND f.path < ?", (pid, path, path + "￿"))}
    field_id = db.value("SELECT id FROM fields WHERE project_id = ? ORDER BY id LIMIT 1", (pid,))
    seen = set()
    now = dbm.now()
    for start in range(0, len(found), CHUNK):
        if cancel is not None and cancel():
            break
        with db.transaction():
            for p, excluded in found[start:start + CHUNK]:
                p = os.path.realpath(p)
                st = _stat(p)
                if st is None:
                    continue
                size, mtime = st
                seen.add(p)
                row = known.get(p)
                if row is None:
                    row = db.one("SELECT id, size, mtime_ns, missing_since, excluded_dir FROM files WHERE path = ?", (p,))
                if row is None:
                    cur = db.execute("INSERT INTO files (path, size, mtime_ns, discovered_at, excluded_dir) "
                                     "VALUES (?, ?, ?, ?, ?)", (p, size, mtime, now, int(excluded)))
                    fid = int(cur.lastrowid)
                    res["new"] += 1
                    res["new_ids"].append(fid)
                else:
                    fid = int(row["id"])
                    if row["size"] != size or row["mtime_ns"] != mtime:
                        db.execute("UPDATE files SET size = ?, mtime_ns = ?, checksum = NULL, header_version = NULL, "
                                   "missing_since = NULL WHERE id = ?", (size, mtime, fid))
                        db.execute("DELETE FROM frame_metrics WHERE file_id = ?", (fid,))
                        res["changed"] += 1
                    elif row["missing_since"]:
                        db.execute("UPDATE files SET missing_since = NULL WHERE id = ?", (fid,))
                        res["back"] += 1
                    if int(row["excluded_dir"]) != int(excluded):
                        db.execute("UPDATE files SET excluded_dir = ? WHERE id = ?", (int(excluded), fid))
                db.execute("INSERT INTO project_files (project_id, file_id, source_folder_id, field_id) "
                           "VALUES (?, ?, ?, ?) ON CONFLICT (project_id, file_id) DO NOTHING",
                           (pid, fid, folder_id, field_id))
        if progress:
            progress(done=min(start + CHUNK, len(found)))
    else:
        # Nur nach einem vollständigen Durchlauf als fehlend markieren – ein Abbruch darf nichts „verlieren“.
        with db.transaction():
            gone = db.query(
                "SELECT f.id, f.path FROM files f JOIN project_files pf ON pf.file_id = f.id "
                "WHERE pf.project_id = ? AND pf.source_folder_id = ? AND f.missing_since IS NULL", (pid, folder_id))
            for r in gone:
                if r["path"] not in seen and not os.path.exists(r["path"]):
                    db.execute("UPDATE files SET missing_since = ? WHERE id = ?", (now, r["id"]))
                    res["missing"] += 1
            db.execute("UPDATE source_folders SET online = 1, checked_at = ? WHERE id = ?", (now, folder_id))
    return res


def discover_project(db: dbm.Database, pid: int, progress=None, cancel=None) -> dict:
    """Alle Quellordner eines Projekts durchsuchen. → Zusammenfassung

    ``new_ids`` (neu angelegte Dateien) und ``event`` (ID des History-Eintrags ``files_discovered``) braucht
    ``flow.headers.summarize_new``: erst nach dem Header-Lesen sind Typ, Filter, Nacht und Belichtung bekannt."""
    folders = db.query("SELECT id, path FROM source_folders WHERE project_id = ? ORDER BY id", (pid,))
    total = dict(folders=[], new=0, changed=0, missing=0, back=0, offline=0, new_ids=[], event=None)
    for i, f in enumerate(folders):
        if cancel is not None and cancel():
            break
        if progress:
            progress(step="Neue Dateien suchen", done=i, total=len(folders),
                     message=os.path.basename(f["path"]) or f["path"])
        r = discover_folder(db, pid, int(f["id"]), f["path"], cancel=cancel)
        total["new_ids"] += r.pop("new_ids")
        total["folders"].append(r)
        for k in ("new", "changed", "missing", "back"):
            total[k] += r[k]
        total["offline"] += 0 if r["online"] else 1
    if total["new"]:
        with db.transaction():
            import json
            total["event"] = db.event("files_discovered", pid, json.dumps({"new": total["new"]}))
    return total
