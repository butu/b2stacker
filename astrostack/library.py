"""Projekte und Quellordner in der Datenbank (Konzept Abschnitt 8).

Ein Projekt hat einen Namen, eine optionale Beschreibung, beliebig viele Quellordner und genau ein Feld
(Mosaik-Panels kommen später). Der Projektordner unter ``projects/`` wird beim Anlegen erzeugt und beim
Umbenennen mit umbenannt.
"""
from __future__ import annotations

import json
import os

from . import db as dbm
from .workspace import Workspace


class LibraryError(ValueError):
    pass


def _norm_folder(path: str) -> str:
    p = (path or "").strip().strip('"')
    if not p:
        raise LibraryError("Kein Ordner angegeben.")
    p = os.path.abspath(os.path.expanduser(p))
    return p.rstrip("\\/") or p


def create_project(db: dbm.Database, ws: Workspace, name: str, description: str = "",
                   folders: list[str] | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise LibraryError("Bitte einen Projektnamen angeben.")
    if db.one("SELECT 1 FROM projects WHERE name = ?", (name,)):
        raise LibraryError("Ein Projekt mit diesem Namen gibt es schon: " + name)
    taken = {r[0] for r in db.query("SELECT folder FROM projects")}
    folder = ws.unique_folder(name, taken)
    now = dbm.now()
    with db.transaction():
        cur = db.execute("INSERT INTO projects (name, folder, description, created_at, opened_at) "
                         "VALUES (?, ?, ?, ?, ?)", (name, folder, description or "", now, now))
        pid = int(cur.lastrowid)
        db.execute("INSERT INTO fields (project_id, name, created_at) VALUES (?, 'Hauptfeld', ?)", (pid, now))
        for f in folders or []:
            _add_folder(db, pid, f)
        db.event("project_created", pid, json.dumps({"name": name}))
    os.makedirs(ws.project_dir(folder), exist_ok=True)
    return pid


def _add_folder(db: dbm.Database, pid: int, path: str) -> int:
    path = _norm_folder(path)
    row = db.one("SELECT id FROM source_folders WHERE project_id = ? AND path = ?", (pid, path))
    if row:
        return int(row[0])
    cur = db.execute("INSERT INTO source_folders (project_id, path, added_at) VALUES (?, ?, ?)",
                     (pid, path, dbm.now()))
    db.event("folder_added", pid, json.dumps({"path": path}))
    return int(cur.lastrowid)


def add_folders(db: dbm.Database, pid: int, paths: list[str]) -> list[int]:
    get_project(db, pid)
    with db.transaction():
        return [_add_folder(db, pid, p) for p in paths]


def remove_folder(db: dbm.Database, pid: int, folder_id: int) -> None:
    """Quellordner entfernen. Seine Dateien gehören dann nicht mehr zum Projekt; die Dateien selbst bleiben."""
    row = db.one("SELECT path FROM source_folders WHERE id = ? AND project_id = ?", (folder_id, pid))
    if not row:
        raise LibraryError("Ordner gehört nicht zu diesem Projekt.")
    with db.transaction():
        db.execute("DELETE FROM project_files WHERE project_id = ? AND source_folder_id = ?", (pid, folder_id))
        db.execute("DELETE FROM source_folders WHERE id = ?", (folder_id,))
        db.event("folder_removed", pid, json.dumps({"path": row[0]}))


def get_project(db: dbm.Database, pid: int):
    row = db.one("SELECT * FROM projects WHERE id = ?", (pid,))
    if not row:
        raise LibraryError("Projekt nicht gefunden: %s" % pid)
    return row


def find_project(db: dbm.Database, key) -> int:
    """Projekt über id, Name (ohne Groß-/Kleinschreibung) oder Objekt finden."""
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        return int(get_project(db, int(key))["id"])
    k = (key or "").strip()
    for sql in ("SELECT id FROM projects WHERE name = ? COLLATE NOCASE",
                "SELECT id FROM projects WHERE object = ? COLLATE NOCASE"):
        rows = db.query(sql, (k,))
        if len(rows) == 1:
            return int(rows[0][0])
    raise LibraryError("Projekt nicht gefunden: " + k)


def rename_project(db: dbm.Database, ws: Workspace, pid: int, new_name: str) -> str:
    """Projekt umbenennen, Ordner mit. → neuer Ordnername"""
    p = get_project(db, pid)
    new_name = (new_name or "").strip()
    if not new_name:
        raise LibraryError("Bitte einen Projektnamen angeben.")
    if new_name == p["name"]:
        return p["folder"]
    other = db.one("SELECT id FROM projects WHERE name = ? AND id != ?", (new_name, pid))
    if other:
        raise LibraryError("Ein Projekt mit diesem Namen gibt es schon: " + new_name)
    taken = {r[0] for r in db.query("SELECT folder FROM projects WHERE id != ?", (pid,))}
    from .workspace import folder_name
    wanted = folder_name(new_name)
    folder = wanted if wanted.lower() == p["folder"].lower() else ws.unique_folder(new_name, taken)
    ws.rename_folder(p["folder"], folder)                     # zuerst der Ordner: scheitert er, bleibt alles
    with db.transaction():
        db.execute("UPDATE projects SET name = ?, folder = ? WHERE id = ?", (new_name, folder, pid))
        db.event("project_renamed", pid, json.dumps({"old": p["name"], "new": new_name}))
    return folder


def set_description(db: dbm.Database, pid: int, text: str) -> None:
    get_project(db, pid)
    with db.transaction():
        db.execute("UPDATE projects SET description = ? WHERE id = ?", (text or "", pid))


def touch_opened(db: dbm.Database, pid: int) -> None:
    with db.transaction():
        db.execute("UPDATE projects SET opened_at = ? WHERE id = ?", (dbm.now(), pid))


def list_projects(db: dbm.Database) -> list[dict]:
    rows = db.query("""
        SELECT p.id, p.name, p.folder, p.description, p.object, p.created_at, p.opened_at, p.stacked_at,
               (SELECT COUNT(*) FROM source_folders s WHERE s.project_id = p.id) AS folders,
               (SELECT COUNT(*) FROM source_folders s WHERE s.project_id = p.id AND s.online = 0) AS offline
        FROM projects p ORDER BY COALESCE(p.opened_at, p.created_at) DESC, p.id DESC""")
    return [dict(r) for r in rows]
