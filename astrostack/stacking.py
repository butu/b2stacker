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


def _keep_masters(db: dbm.Database, pid: int, pdir: str, sid: int, job: dict, masters: list[str]) -> list[dict]:
    """Neue Master-Bias/-Flats aus temp/ ins Projekt übernehmen (``masters/``) und mit dem Stack verknüpfen;
    wiederverwendete Master nur verknüpfen. → [{kind, key, path, reused}]"""
    keys, reused = job.get("masterKeys") or {}, job.get("reusedMasters") or {}
    out = []
    mdir = os.path.join(pdir, "masters")
    darks = [m for m in masters if os.path.basename(m).lower().startswith("masterdark")]
    for m in masters:
        name = os.path.basename(m).lower()
        if name.startswith("masterbias"):
            k = "bias"
        elif name.startswith("masterdark") and len(darks) == 1:      # mehrere Belichtungen → nicht eindeutig
            k = "dark"
        elif name.startswith("masterflat") and "b2session-" in name:
            k = name.split("b2session-")[1].split("_")[0].split(".")[0].upper()
        else:
            continue
        key = keys.get(k)
        if not key or k in reused:
            continue
        os.makedirs(mdir, exist_ok=True)
        kind = k if k in ("bias", "dark") else "flat"
        dst = os.path.join(mdir, "%s_%s.xisf" % (kind, key))
        shutil.move(m, dst)
        db.execute("INSERT INTO masters (project_id, type, path, key, key_json, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                   "ON CONFLICT (project_id, key) DO UPDATE SET path = excluded.path",
                   (pid, kind, dst, key, json.dumps(dict((job.get("masterInfo") or {}).get(k) or {}, kind=kind, set=k, stack=sid)), dbm.now()))
        out.append(dict(kind=kind, key=key, path=dst, reused=False))
    for k, path in reused.items():
        out.append(dict(kind=k if k in ("bias", "dark") else "flat", key=keys.get(k), path=path, reused=True))
    for o in out:                                    # auch Master aus anderen Projekten (Kalibrier-Bibliothek)
        mid = db.value("SELECT id FROM masters WHERE key = ? AND path = ?", (o["key"], o["path"])) or \
            db.value("SELECT id FROM masters WHERE key = ? ORDER BY project_id != ?, id DESC", (o["key"], pid))
        if mid:
            db.execute("INSERT OR IGNORE INTO stack_masters (stack_id, master_id) VALUES (?, ?)", (sid, mid))
    return out


def explain_failure(res: dict, out_dir: str) -> str:
    """Warum hat WBPP kein Master geliefert? → Klartext aus Ergebnisdatei und WBPP-Protokoll (wichtigstes zuerst)."""
    from . import wbpplog
    parts = []
    found = wbpplog.diagnose(out_dir)
    causes = [c["text"] for c in found if c.get("level") == "error"] + \
        [c["text"] for c in found if c.get("level") != "error"]
    not_added = res.get("notAdded") or []
    if not_added:
        parts.append("WBPP hat %d Datei%s nicht angenommen (z. B. %s: %s)." % (
            len(not_added), "" if len(not_added) == 1 else "en", os.path.basename(not_added[0].get("path") or ""),
            (not_added[0].get("message") or "").strip()[:160]))
    groups = res.get("groups") or []
    if groups and not any(g.get("mode") == "post" for g in groups):
        parts.append("WBPP hat keine Light-Gruppe zum Integrieren gebildet.")
    parts += causes
    err = (res.get("error") or "").strip()
    if err and err not in parts:
        parts.append(("Fehler in PixInsight: " + err) if res.get("phase") == "run" else err)
    if res.get("exit_code") not in (0, None) and not parts:
        parts.append("PixInsight wurde mit Code %s beendet." % res.get("exit_code"))
    if not parts:
        parts.append("WBPP lief durch, hat aber kein Light-Master geschrieben – Details im WBPP-Protokoll.")
    return " ".join(parts)


def _keep_diagnostics(pdir: str, name: str, out_dir: str, temp: str) -> str:
    """Protokolle eines fehlgeschlagenen Laufs aufheben (temp/ wird gleich geleert). → Ordnername im Projekt"""
    folder = _unique_dir(pdir, name + " – fehlgeschlagen")
    dst = os.path.join(pdir, folder, "protokoll")
    os.makedirs(dst, exist_ok=True)
    logs = os.path.join(out_dir, "logs")
    if os.path.isdir(logs):
        shutil.copytree(logs, dst, dirs_exist_ok=True)
    for f in (os.path.join(out_dir, "b2-result.json"), os.path.join(out_dir, "b2-status.json"),
              os.path.join(temp, "b2job.json")):
        if os.path.isfile(f):
            shutil.copy2(f, dst)
    return folder


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
            progress(step="Stacken: %s (%d von %d)" % (run["name"], i + 1, len(runs)), done=0, total=0,
                     detail=dict(run=run["name"], run_index=i + 1, runs=len(runs), ops=[], current="",
                                 lights=run["lights"], logdir=os.path.join(ws.temp, "wbpp", "logs")))
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
        job = {}
        try:
            job = handover.build_job(db, run, out, ref_path, project_id=pid)
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
            status, error = "failed", explain_failure(res, out)
        folder, files = "", []
        try:
            from . import logbook
            logbook.event("stack_result", project=pid, run=run["name"], status=status, error=error,
                          **{k: res.get(k) for k in ("phase", "exit_code", "added", "notAdded", "groups", "wbpp",
                                                     "pixinsight", "interrupted", "logFile", "error")})
        except Exception:  # noqa: BLE001
            pass
        if status == "failed":
            try:
                folder = _keep_diagnostics(pdir, stack_name(run), out, ws.temp)
            except OSError:
                folder = ""
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
        kept = []
        with db.transaction():
            if status == "done":
                kept = _keep_masters(db, pid, pdir, sid, job, res.get("masters") or [])
            db.execute("UPDATE stacks SET status = ?, error = ?, folder = ?, finished_at = ?, pixinsight_version = ?, "
                       "wbpp_version = ?, size_bytes = ? WHERE id = ?",
                       (status, error, folder, dbm.now(), res.get("pixinsight"), res.get("wbpp"), _size(files), sid))
            for f in files:
                # Upsert: der Watcher könnte die Datei schon als fremdes Bild gesehen haben – sie gehört b2stacker
                db.execute("INSERT INTO assets (project_id, stack_id, path, size, added_at, owned) "
                           "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT (project_id, path) DO UPDATE SET owned = 1, "
                           "stack_id = excluded.stack_id, size = excluded.size, hidden = 0, missing_since = NULL",
                           (pid, sid, f, _size([f]), dbm.now()))
            if status == "done":
                db.execute("UPDATE projects SET stacked_at = ? WHERE id = ?", (dbm.now(), pid))
            db.event("stack_" + status, pid, json.dumps(dict(stack=sid, run=run["name"], lights=run["lights"],
                                                             hours=run["hours"], error=error)))
        ws.clear_temp()
        results.append(dict(stack_id=sid, run=run["name"], status=status, error=error, folder=folder, files=files,
                            masters=kept))
    return results


def delete_stack(db: dbm.Database, ws: Workspace, pid: int, sid: int) -> list[str]:
    """Stack löschen: nur von b2stacker erzeugte Dateien; fremde Dateien im Stack-Ordner bleiben. → gelöscht"""
    row = db.one("SELECT folder, name, settings_json FROM stacks WHERE id = ? AND project_id = ?", (sid, pid))
    if not row:
        raise ValueError("Stack nicht gefunden.")
    folder = db.value("SELECT folder FROM projects WHERE id = ?", (pid,))
    removed, locked = [], []
    for a in db.query("SELECT id, path FROM assets WHERE stack_id = ? AND owned = 1", (sid,)):
        try:
            os.remove(a["path"])
            removed.append(a["path"])
        except FileNotFoundError:
            pass
        except PermissionError:                              # Windows: Datei gerade geöffnet (z. B. in PixInsight)
            locked.append(a)
    if locked:
        with db.transaction():                               # was schon weg ist, vergessen; der Stack bleibt
            db.execute("DELETE FROM assets WHERE stack_id = ? AND owned = 1 AND id NOT IN (%s)"
                       % ",".join("?" * len(locked)), (sid, *[a["id"] for a in locked]))
        raise ValueError("Die Datei ist gerade geöffnet und kann nicht gelöscht werden – bitte schließen und erneut "
                         "versuchen: %s" % locked[0]["path"])
    imported = bool(json.loads(row["settings_json"] or "{}").get("imported"))
    if row["folder"] and not imported:                       # übernommene Stacks: fremder Ordner, nie anfassen
        sdir = os.path.join(ws.project_dir(folder), row["folder"])
        log = os.path.join(sdir, "protokoll")
        if os.path.isdir(log):
            shutil.rmtree(log, ignore_errors=True)
        try:
            os.rmdir(sdir)                                   # nur, wenn nichts Fremdes darin liegt
        except OSError:
            pass
    with db.transaction():
        db.execute("UPDATE projects SET cover_asset_id = NULL WHERE id = ? AND cover_asset_id IN "
                   "(SELECT id FROM assets WHERE stack_id = ? AND owned = 1)", (pid, sid))
        db.execute("DELETE FROM assets WHERE stack_id = ? AND owned = 1", (sid,))
        db.execute("UPDATE assets SET stack_id = NULL WHERE stack_id = ?", (sid,))
        db.execute("DELETE FROM stacks WHERE id = ?", (sid,))
        db.event("stack_deleted", pid, json.dumps(dict(stack=sid, name=row["name"], files=len(removed))))
    return removed


def set_note(db: dbm.Database, pid: int, sid: int, text: str) -> None:
    with db.transaction():
        cur = db.execute("UPDATE stacks SET note = ? WHERE id = ? AND project_id = ?", (text or "", sid, pid))
        if not cur.rowcount:
            raise ValueError("Stack nicht gefunden.")
