"""Stacken eines Projekts (Konzept Abschnitte 3, 10, 11) – der einzige Schritt, der nur auf Klick läuft.

Je aktivem Plan-Lauf:
1. ``temp/`` von Resten befreien,
2. Auftrag + Skripte schreiben und PixInsight/WBPP starten (``handover``),
3. das Master in einen gut benannten Stack-Ordner im Projekt verschieben (``2026-11-04 Ha 12h30``),
4. den Stack mit Frames, Einstellungen, Versionen und Referenzbild in der Datenbank festhalten,
5. ``temp/`` leeren.

Weitere Läufe werden auf das Master des ersten Laufs ausgerichtet, damit alle Ergebnisse pixelgenau
übereinander liegen. Ein fehlgeschlagener Lauf wird mit Grund gespeichert und stoppt die anderen nicht.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil

from . import db as dbm
from . import handover
from .flow.plan import load_plan
from .workspace import Workspace, folder_name


def stack_name(run: dict, day: datetime.date | None = None) -> str:
    """Ordnername eines Stacks, z. B. „2026-11-04 Ha 12h30“."""
    day = day or datetime.date.today()
    minutes = int(round((run.get("hours") or 0) * 60))
    return folder_name("%s %s %dh%02d" % (day.isoformat(), run.get("name") or "Stack", minutes // 60, minutes % 60))


def _unique_dir(parent: str, name: str) -> str:
    cand, n = name, 2
    while os.path.exists(os.path.join(parent, cand)):
        cand = "%s (%d)" % (name, n)
        n += 1
    return cand


def _size(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def _light_master(masters: list[str]) -> list[str]:
    """Das Light-Master (WBPP legt bei Autocrop eine zweite Variante an – die ohne Zuschnitt zählt)."""
    lights = [m for m in masters if os.path.basename(m).lower().startswith("masterlight")]
    plain = [m for m in lights if "autocrop" not in os.path.basename(m).lower()]
    return plain or lights


def stack_project(db: dbm.Database, ws: Workspace, pid: int, pixinsight: str, run_ids: list[str] | None = None,
                  progress=None, cancel=None, runner=handover.run, origin: str = "ui") -> list[dict]:
    """Alle aktiven (oder die gewählten) Läufe des gespeicherten Plans stacken. → Ergebnis je Lauf"""
    plan = load_plan(db, pid)
    if not plan:
        raise ValueError("Für dieses Projekt gibt es noch keinen Plan.")
    proj = db.one("SELECT name, folder FROM projects WHERE id = ?", (pid,))
    runs = [r for r in plan["runs"] if (r["id"] in run_ids if run_ids else r["enabled"])]
    if not runs:
        raise ValueError("Kein Lauf zum Stacken ausgewählt.")
    pdir = ws.project_dir(proj["folder"])
    os.makedirs(pdir, exist_ok=True)
    results, align_master = [], None
    for i, run in enumerate(runs):
        if cancel is not None and cancel.is_set():
            break
        if progress:
            progress(step="Stacken: %s (%d von %d)" % (run["name"], i + 1, len(runs)), done=0, total=0)
        ws.clear_temp()
        out = os.path.join(ws.temp, "wbpp")
        ref_path = align_master
        if ref_path is None and run.get("reference_id"):
            ref_path = db.value("SELECT path FROM files WHERE id = ?", (run["reference_id"],))
        now = dbm.now()
        with db.transaction():
            cur = db.execute(
                "INSERT INTO stacks (project_id, name, folder, status, created_at, run_label, frames, exposure_s, "
                "nights, settings_json, reference_file_id) VALUES (?, ?, '', 'running', ?, ?, ?, ?, ?, ?, ?)",
                (pid, run["name"], now, run["name"], run["lights"], run["hours"] * 3600, len(run["nights"]),
                 json.dumps(dict(settings=run["settings"], origin=origin, aligned_to_master=bool(align_master))),
                 None if align_master else run.get("reference_id")))
            sid = int(cur.lastrowid)
            db.executemany("INSERT INTO stack_files (stack_id, file_id) VALUES (?, ?)",
                           [(sid, fid) for fid in run["light_ids"]])
        try:
            job = handover.build_job(db, run, out, ref_path)
            launcher, job_path = handover.write_scripts(job, ws.temp, pixinsight)
            res = runner(pixinsight, launcher, job_path, progress=progress, cancel=cancel)
        except Exception as e:  # noqa: BLE001 – Lauf scheitert sichtbar, die anderen laufen weiter
            res = dict(ok=False, error="%s: %s" % (type(e).__name__, e), masters=[])
        masters = _light_master(res.get("masters") or [])
        if res.get("cancelled"):
            status, error = "cancelled", "abgebrochen"
        elif res.get("ok") and masters:
            status, error = "done", None
        else:
            status, error = "failed", res.get("error") or "WBPP hat kein Master erzeugt."
        folder, files = "", []
        if status == "done":
            folder = _unique_dir(pdir, stack_name(run))
            sdir = os.path.join(pdir, folder)
            os.makedirs(sdir)
            for m in masters:
                dst = os.path.join(sdir, os.path.basename(m))
                shutil.move(m, dst)
                files.append(dst)
            logs = os.path.join(out, "logs")
            if os.path.isdir(logs):
                shutil.copytree(logs, os.path.join(sdir, "protokoll"))
            if align_master is None:
                align_master = files[0]
        with db.transaction():
            db.execute("UPDATE stacks SET status = ?, error = ?, folder = ?, finished_at = ?, pixinsight_version = ?, "
                       "wbpp_version = ?, size_bytes = ? WHERE id = ?",
                       (status, error, folder, dbm.now(), res.get("pixinsight"), res.get("wbpp"), _size(files), sid))
            for f in files:
                db.execute("INSERT OR IGNORE INTO assets (project_id, stack_id, path, size, added_at, owned) "
                           "VALUES (?, ?, ?, ?, ?, 1)", (pid, sid, f, _size([f]), dbm.now()))
            if status == "done":
                db.execute("UPDATE projects SET stacked_at = ? WHERE id = ?", (dbm.now(), pid))
            db.event("stack_" + status, pid, json.dumps(dict(stack=sid, run=run["name"], lights=run["lights"],
                                                             hours=run["hours"], error=error)))
        ws.clear_temp()
        results.append(dict(stack_id=sid, run=run["name"], status=status, error=error, folder=folder, files=files))
    return results


def delete_stack(db: dbm.Database, ws: Workspace, pid: int, sid: int) -> list[str]:
    """Stack löschen: nur von b2stacker erzeugte Dateien; fremde Dateien im Stack-Ordner bleiben. → gelöscht"""
    row = db.one("SELECT folder FROM stacks WHERE id = ? AND project_id = ?", (sid, pid))
    if not row:
        raise ValueError("Stack nicht gefunden.")
    folder = db.value("SELECT folder FROM projects WHERE id = ?", (pid,))
    removed = []
    for a in db.query("SELECT path FROM assets WHERE stack_id = ? AND owned = 1", (sid,)):
        try:
            os.remove(a["path"])
            removed.append(a["path"])
        except FileNotFoundError:
            pass
    if row["folder"]:
        sdir = os.path.join(ws.project_dir(folder), row["folder"])
        log = os.path.join(sdir, "protokoll")
        if os.path.isdir(log):
            shutil.rmtree(log)
        try:
            os.rmdir(sdir)                                   # nur, wenn nichts Fremdes darin liegt
        except OSError:
            pass
    with db.transaction():
        db.execute("DELETE FROM assets WHERE stack_id = ? AND owned = 1", (sid,))
        db.execute("UPDATE assets SET stack_id = NULL WHERE stack_id = ?", (sid,))
        db.execute("DELETE FROM stacks WHERE id = ?", (sid,))
        db.event("stack_deleted", pid, json.dumps(dict(stack=sid, files=len(removed))))
    return removed


def set_note(db: dbm.Database, pid: int, sid: int, text: str) -> None:
    with db.transaction():
        cur = db.execute("UPDATE stacks SET note = ? WHERE id = ? AND project_id = ?", (text or "", sid, pid))
        if not cur.rowcount:
            raise ValueError("Stack nicht gefunden.")
