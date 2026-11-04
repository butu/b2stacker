"""Dienst-Schicht des Neubaus: eine Stelle für Oberfläche, CLI und MCP.

``Core`` hält Arbeitsverzeichnis, Datenbank und Aufträge zusammen und setzt die Auslöser aus dem Konzept
(Abschnitt 5) um: App-Start (alle Projekte), Projekt geöffnet, Ordner hinzugefügt, neue Programmversion –
jeweils läuft der automatische Ablauf im Hintergrund. Nur Stacken braucht einen Klick.

Alle Methoden geben einfache dicts/listen zurück (JSON-tauglich) und werfen ``ValueError`` mit Klartext.
"""
from __future__ import annotations

import json
import os
import threading

from . import db as dbm
from . import flow, library, stacking, tasks
from .flow import plan as fplan
from .workspace import Workspace


class Core:
    def __init__(self, workdir: str, pixinsight: str = "", workers: int = 2, manager: tasks.TaskManager | None = None,
                 stack_runner=None):
        self.ws = Workspace(workdir).ensure()
        self.db = self.ws.open_db()
        self.pixinsight = pixinsight
        self.workers = workers
        self.tasks = manager or tasks.TaskManager()
        self.stack_runner = stack_runner              # Ersatz für handover.run (Tests)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.tasks.shutdown()
        self.db.close()

    # ------------------------------------------------------------ Hilfen
    def _pid(self, project) -> int:
        try:
            return library.find_project(self.db, project)
        except library.LibraryError as e:
            raise ValueError(str(e)) from None

    def _notify(self, kind: str, **data) -> None:
        self.tasks.hub.publish(kind, data)

    # ------------------------------------------------------------ Projekte
    def projects(self) -> list[dict]:
        """Startseite: alle Projekte mit Kennzahlen, sofort aus der Datenbank."""
        out = []
        for p in library.list_projects(self.db):
            pid = p["id"]
            rows = self.db.query(
                "SELECT f.filter, COUNT(*) AS n, SUM(f.exposure) AS s, MIN(f.date_obs) AS first, MAX(f.date_obs) AS last "
                "FROM files f JOIN project_files pf ON pf.file_id = f.id "
                "LEFT JOIN file_status st ON st.file_id = f.id AND st.project_id = pf.project_id "
                "WHERE pf.project_id = ? AND f.type = 'light' AND f.missing_since IS NULL "
                "AND COALESCE(st.status, 'use') = 'use' GROUP BY f.filter", (pid,))
            filters = [dict(filter=r["filter"] or "?", lights=r["n"], hours=round((r["s"] or 0) / 3600, 2)) for r in rows]
            months = sorted({m[0] for m in self.db.query(
                "SELECT DISTINCT substr(f.night, 1, 7) FROM files f JOIN project_files pf ON pf.file_id = f.id "
                "WHERE pf.project_id = ? AND f.type = 'light' AND f.night IS NOT NULL", (pid,))})
            last_stack = self.db.value("SELECT MAX(created_at) FROM stacks WHERE project_id = ? AND status = 'done'", (pid,))
            new_lights = self.db.value(
                "SELECT COUNT(*) FROM files f JOIN project_files pf ON pf.file_id = f.id WHERE pf.project_id = ? "
                "AND f.type = 'light' AND f.discovered_at > COALESCE(?, '')", (pid, last_stack))
            out.append(dict(p, filters=filters, hours=round(sum(f["hours"] for f in filters), 2),
                            lights=sum(f["lights"] for f in filters), months=months,
                            first_light=min((r["first"] for r in rows if r["first"]), default=None),
                            last_light=max((r["last"] for r in rows if r["last"]), default=None),
                            new_since_stack=new_lights if last_stack else None,
                            busy=bool(self.tasks.active(pid))))
        return out

    def create_project(self, name: str, folders: list[str] | None = None, description: str = "") -> dict:
        try:
            pid = library.create_project(self.db, self.ws, name, description, folders)
        except library.LibraryError as e:
            raise ValueError(str(e)) from None
        self._notify("projects")
        task = self.refresh(pid, reason="Projekt angelegt")
        return dict(id=pid, task=task.as_dict())

    def add_folders(self, project, folders: list[str]) -> dict:
        pid = self._pid(project)
        library.add_folders(self.db, pid, folders)
        return dict(id=pid, task=self.refresh(pid, reason="Ordner hinzugefügt").as_dict())

    def remove_folder(self, project, folder_id: int) -> None:
        pid = self._pid(project)
        library.remove_folder(self.db, pid, folder_id)
        self.refresh(pid, reason="Ordner entfernt", discover=False)

    def rename(self, project, name: str) -> dict:
        pid = self._pid(project)
        try:
            folder = library.rename_project(self.db, self.ws, pid, name)
        except (library.LibraryError, PermissionError) as e:
            raise ValueError(str(e)) from None
        self._notify("projects")
        return dict(id=pid, folder=folder)

    def describe(self, project, text: str) -> None:
        library.set_description(self.db, self._pid(project), text)
        self._notify("projects")

    def open_project(self, project) -> dict:
        """Projekt geöffnet: Vorrang in der Warteschlange und Prüfung auf Neues im Hintergrund."""
        pid = self._pid(project)
        library.touch_opened(self.db, pid)
        self.tasks.prefer_project(pid)
        self.refresh(pid, reason="Projekt geöffnet")
        return self.project(pid)

    def project(self, project) -> dict:
        pid = self._pid(project)
        p = dict(library.get_project(self.db, pid))
        p["folders"] = [dict(r) for r in self.db.query(
            "SELECT id, path, online, checked_at, (SELECT COUNT(*) FROM project_files pf WHERE pf.source_folder_id = "
            "s.id) AS files FROM source_folders s WHERE project_id = ? ORDER BY id", (pid,))]
        counts = dict(self.db.query(
            "SELECT COALESCE(f.type, 'neu'), COUNT(*) FROM files f JOIN project_files pf ON pf.file_id = f.id "
            "WHERE pf.project_id = ? AND f.missing_since IS NULL GROUP BY f.type", (pid,)))
        p["counts"] = counts
        p["missing"] = self.db.value("SELECT COUNT(*) FROM files f JOIN project_files pf ON pf.file_id = f.id "
                                     "WHERE pf.project_id = ? AND f.missing_since IS NOT NULL", (pid,))
        p["decisions"] = dict(self.db.query("SELECT status, COUNT(*) FROM file_status WHERE project_id = ? "
                                            "GROUP BY status", (pid,)))
        p["plan"] = fplan.load_plan(self.db, pid)
        p["stacks"] = self.stacks(pid)
        p["tasks"] = [t.as_dict() for t in self.tasks.active(pid)]
        p["usage"] = self.ws.usage(p["folder"])
        return p

    # ------------------------------------------------------------ automatischer Ablauf
    def refresh(self, project, reason: str = "", discover: bool = True) -> tasks.Task:
        """Schritte 1–5 im Hintergrund (doppelte Anstöße werden zusammengelegt)."""
        pid = self._pid(project)
        name = self.db.value("SELECT name FROM projects WHERE id = ?", (pid,))

        def work(task: tasks.Task):
            cancel = lambda: task.cancelled                       # noqa: E731
            res = {}
            if discover:
                res["discover"] = flow.discover_project(self.db, pid, progress=task.progress, cancel=cancel)
            res["headers"] = flow.read_headers(self.db, pid, progress=task.progress, cancel=cancel)
            res["measure"] = flow.measure_files(self.db, self.ws, pid, progress=task.progress, cancel=cancel,
                                                workers=self.workers)
            task.check_cancel()
            res["assess"] = {k: v for k, v in flow.assess_project(self.db, pid, progress=task.progress).items()
                             if k != "nights"}
            plan = fplan.update_plan(self.db, pid, self.ws.root, progress=task.progress)
            res["plan_ready"] = plan["ready"]
            self._notify("project", id=pid)
            return res
        return self.tasks.submit("refresh", "%s prüfen%s" % (name, (" (%s)" % reason) if reason else ""), work,
                                 lane="analysis", project_id=pid, key="refresh:%d" % pid)

    def refresh_all(self) -> list[dict]:
        """App-Start: alle Projekte im Hintergrund prüfen (das geöffnete hat Vorrang, siehe open_project)."""
        return [self.refresh(p["id"], reason="Programmstart").as_dict() for p in library.list_projects(self.db)]

    # ------------------------------------------------------------ Frames
    def frames(self, project, type_: str = "light", status: str | None = None, offset: int = 0,
               limit: int = 200) -> dict:
        pid = self._pid(project)
        where = ["pf.project_id = ?", "f.missing_since IS NULL"]
        args: list = [pid]
        if type_ != "all":
            where.append("f.type = ?")
            args.append(type_)
        if status:
            where.append("COALESCE(st.status, 'use') = ?")
            args.append(status)
        sql = (" FROM files f JOIN project_files pf ON pf.file_id = f.id LEFT JOIN frame_metrics m ON m.file_id = f.id "
               "LEFT JOIN file_status st ON st.file_id = f.id AND st.project_id = pf.project_id WHERE " + " AND ".join(where))
        total = self.db.value("SELECT COUNT(*)" + sql, args)
        rows = self.db.query("SELECT f.id, f.path, f.type, f.filter, f.night, f.date_obs, f.exposure, f.header_error, "
                             "m.nstars, m.fwhm, m.ecc, m.bg, m.noise, m.thumb, COALESCE(st.status, 'use') AS status, "
                             "st.origin, st.reason" + sql + " ORDER BY f.date_obs, f.path LIMIT ? OFFSET ?",
                             args + [max(1, min(int(limit), 5000)), max(0, int(offset))])
        return dict(total=total, offset=offset, frames=[dict(r, name=os.path.basename(r["path"])) for r in rows])

    def decide(self, project, file_ids: list[int], decision: str, reason: str = "") -> dict:
        pid = self._pid(project)
        n = flow.decide(self.db, pid, [int(x) for x in file_ids], decision, reason)
        if decision == "auto":
            flow.assess_project(self.db, pid)
        plan = fplan.update_plan(self.db, pid, self.ws.root)
        self._notify("project", id=pid)
        return dict(changed=n, plan_ready=plan["ready"])

    # ------------------------------------------------------------ Plan
    def plan(self, project) -> dict | None:
        return fplan.load_plan(self.db, self._pid(project))

    def edit_plan(self, project, options: dict | None = None, runs: dict | None = None, reset: bool = False) -> dict:
        pid = self._pid(project)
        plan = fplan.edit_plan(self.db, pid, self.ws.root, options, runs, reset)
        self._notify("project", id=pid)
        return plan

    # ------------------------------------------------------------ Stacken
    def stack(self, project, run_ids: list[str] | None = None, origin: str = "ui") -> dict:
        pid = self._pid(project)
        if not self.pixinsight or not os.path.isfile(self.pixinsight):
            raise ValueError("PixInsight nicht gefunden – bitte in den Einstellungen angeben.")
        busy = [t for t in self.tasks.active(pid) if t.lane == "analysis"]
        if busy:
            raise ValueError("Bitte kurz warten: %s läuft noch." % busy[0].title)
        plan = fplan.load_plan(self.db, pid)
        if not plan or not (run_ids or plan.get("ready")):
            raise ValueError("Der Plan hat keinen aktiven Lauf.")
        name = self.db.value("SELECT name FROM projects WHERE id = ?", (pid,))

        def work(task: tasks.Task):
            kw = dict(runner=self.stack_runner) if self.stack_runner else {}
            res = stacking.stack_project(self.db, self.ws, pid, self.pixinsight, run_ids, progress=task.progress,
                                         cancel=task.cancel_event, origin=origin, **kw)
            self._notify("project", id=pid)
            return res
        title = "%s stacken" % name + (" · per Claude" if origin == "mcp" else "")
        return self.tasks.submit("stack", title, work, lane="stack", project_id=pid, origin=origin).as_dict()

    def stacks(self, project) -> list[dict]:
        pid = self._pid(project)
        out = []
        for r in self.db.query("SELECT * FROM stacks WHERE project_id = ? ORDER BY created_at DESC, id DESC", (pid,)):
            d = dict(r)
            d["settings"] = json.loads(d.pop("settings_json") or "{}")
            d["quality"] = json.loads(d.pop("quality_json") or "null")
            d["files"] = [a[0] for a in self.db.query("SELECT path FROM assets WHERE stack_id = ?", (r["id"],))]
            out.append(d)
        return out

    def note(self, project, stack_id: int, text: str) -> None:
        stacking.set_note(self.db, self._pid(project), int(stack_id), text)

    def delete_stack(self, project, stack_id: int) -> list[str]:
        pid = self._pid(project)
        removed = stacking.delete_stack(self.db, self.ws, pid, int(stack_id))
        self._notify("project", id=pid)
        return removed

    # ------------------------------------------------------------ History
    def history(self, project=None, limit: int = 200) -> list[dict]:
        sql = ("SELECT e.id, e.at, e.kind, e.data_json, e.project_id, p.name AS project FROM events e "
               "LEFT JOIN projects p ON p.id = e.project_id")
        args: list = []
        if project is not None:
            sql += " WHERE e.project_id = ?"
            args.append(self._pid(project))
        rows = self.db.query(sql + " ORDER BY e.at DESC, e.id DESC LIMIT ?", args + [max(1, min(int(limit), 2000))])
        return [dict(r, data=json.loads(r["data_json"] or "{}")) for r in rows]

    # ------------------------------------------------------------ Wartung
    def backup(self) -> str:
        return dbm.backup(self.db, self.ws.backups)

    def clear_temp(self) -> int:
        if [t for t in self.tasks.active() if t.lane == "stack"]:
            raise ValueError("Während gestackt wird, bleibt der Temp-Ordner unangetastet.")
        return self.ws.clear_temp()
