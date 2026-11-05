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
from . import flow, gallery, history, library, logbook, stacking, stackview, tasks, trash
from .flow import insights
from .flow import plan as fplan
from .flow.headers import summarize_new
from .workspace import Workspace


class Core:
    def __init__(self, workdir: str, pixinsight: str = "", workers: int = 2, manager: tasks.TaskManager | None = None,
                 stack_runner=None, backup_dir: str = ""):
        self.ws = Workspace(workdir).ensure()
        self.db = self.ws.open_db()
        self.pixinsight = pixinsight
        self.workers = workers
        self.tasks = manager or tasks.TaskManager()
        self.stack_runner = stack_runner              # Ersatz für handover.run (Tests)
        self.backup_dir = backup_dir or ""            # zusätzlicher, frei wählbarer Sicherungsort
        self.script_runner = None                     # Ersatz für piscript.run (Tests)
        self.result_opener = None                     # Ersatz für subprocess.Popen beim Öffnen in PixInsight (Tests)
        self._lock = threading.Lock()
        self._mirror_backups()

    def close(self) -> None:
        self.tasks.shutdown()
        self.db.close()

    # ------------------------------------------------------------ Hilfen
    def _pid(self, project) -> int:
        try:
            return library.find_project(self.db, project)
        except library.LibraryError as e:
            raise ValueError(str(e)) from None

    @staticmethod
    def _stack_options() -> dict:
        """Einstellungen fürs Stacken (bei jedem Klick neu gelesen): wach halten, Ergebnis öffnen, RAM-Grenze."""
        from .settings import Settings
        try:
            s = Settings.load()
            return dict(keep_awake=bool(s.keep_awake), open_results=bool(s.open_results),
                        min_free_ram_gb=float(s.min_free_ram_gb or 0))
        except Exception:  # noqa: BLE001
            return dict(keep_awake=True, open_results=False, min_free_ram_gb=0.0)

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
                            cover_url=self._cover_url(pid),
                            lights=sum(f["lights"] for f in filters), months=months,
                            first_light=min((r["first"] for r in rows if r["first"]), default=None),
                            last_light=max((r["last"] for r in rows if r["last"]), default=None),
                            new_since_stack=new_lights if last_stack else None,
                            busy=bool(self.tasks.active(pid))))
        return out

    def _cover_url(self, pid: int) -> str | None:
        """Bild für die Kachel: das gewählte Projektbild, sonst die Vorschau des neuesten Stacks."""
        row = self.db.one("SELECT a.id, a.path, a.size, a.mtime_ns, a.owned, a.stack_id, a.missing_since FROM projects p "
                          "JOIN assets a ON a.id = p.cover_asset_id WHERE p.id = ? AND a.hidden = 0", (pid,))
        if row is not None and not row["missing_since"]:
            if row["owned"] and row["stack_id"]:
                q = json.loads(self.db.value("SELECT quality_json FROM stacks WHERE id = ?", (row["stack_id"],))
                               or "null") or {}
                if q.get("preview"):
                    return "/api/stacks/%d/preview?v=%s" % (row["stack_id"], q["preview"])
            else:
                return "/api/assets/%d/thumb?v=%s" % (row["id"], gallery.thumb_version(row))
        for r in self.db.query("SELECT id, quality_json FROM stacks WHERE project_id = ? AND status = 'done' "
                               "ORDER BY created_at DESC, id DESC LIMIT 5", (pid,)):
            q = json.loads(r["quality_json"] or "null") or {}
            if q.get("preview"):
                return "/api/stacks/%d/preview?v=%s" % (r["id"], q["preview"])
        return None

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
        old = self.db.value("SELECT folder FROM projects WHERE id = ?", (pid,))
        try:
            folder = library.rename_project(self.db, self.ws, pid, name)
        except (library.LibraryError, PermissionError) as e:
            raise ValueError(str(e)) from None
        if folder != old:                            # gespeicherte Pfade im Projektordner mitziehen
            gallery.relocate(self.db, pid, self.ws.project_dir(old), self.ws.project_dir(folder))
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
        p["found_stacks"] = self.found_stacks(pid)
        return p

    def found_stacks(self, project) -> list[dict]:
        """masterLight-Dateien in den Quellordnern, die noch kein Stack in der Liste sind (Vorschlag zum Übernehmen)."""
        pid = self._pid(project)
        ignored = set(((fplan.load_edits(self.db, pid).get("options") or {}).get("ignored_stacks") or []))
        known = {os.path.normcase(r[0]) for r in self.db.query(
            "SELECT path FROM assets WHERE project_id = ? AND stack_id IS NOT NULL", (pid,))}
        out = []
        for r in self.db.query("SELECT f.path, f.filter, f.size FROM files f JOIN project_files pf ON pf.file_id = f.id "
                               "WHERE pf.project_id = ? AND f.type = 'light' AND f.is_master = 1 "
                               "AND f.missing_since IS NULL ORDER BY f.path", (pid,)):
            if os.path.normcase(r["path"]) in known or r["path"] in ignored:
                continue
            if "autocrop" in os.path.basename(r["path"]).lower():
                continue
            out.append(dict(path=r["path"], name=os.path.basename(r["path"]), filter=r["filter"], size=r["size"]))
        return out[:50]

    def ignore_found_stacks(self, project, paths: list[str]) -> None:
        pid = self._pid(project)
        cur = list(((fplan.load_edits(self.db, pid).get("options") or {}).get("ignored_stacks") or []))
        fplan.edit_plan(self.db, pid, self.ws.root, options={"ignored_stacks": sorted(set(cur) | set(paths))})
        self._notify("project", id=pid)

    # ------------------------------------------------------------ automatischer Ablauf
    def refresh(self, project, reason: str = "", discover: bool = True) -> tasks.Task:
        """Schritte 1–5 im Hintergrund (doppelte Anstöße werden zusammengelegt)."""
        pid = self._pid(project)
        name = self.db.value("SELECT name FROM projects WHERE id = ?", (pid,))

        def work(task: tasks.Task):
            cancel = lambda: task.cancelled                       # noqa: E731
            res = {}
            new_ids, event = [], None
            if discover:
                res["discover"] = flow.discover_project(self.db, pid, progress=task.progress, cancel=cancel)
                new_ids, event = res["discover"].pop("new_ids"), res["discover"].pop("event")
            res["headers"] = flow.read_headers(self.db, pid, progress=task.progress, cancel=cancel)
            summarize_new(self.db, event, new_ids)              # Verlauf: „20 neue Lights · Ha · 2 h“
            if discover:
                gallery.scan_project_dir(self.db, self.ws, pid)
                gallery.check_missing(self.db, pid)
            res["measure"] = flow.measure_files(self.db, self.ws, pid, progress=task.progress, cancel=cancel,
                                                workers=self.workers)
            task.check_cancel()
            res["assess"] = {k: v for k, v in flow.assess_project(self.db, pid, progress=task.progress).items()
                             if k != "nights"}
            plan = fplan.update_plan(self.db, pid, self.ws.root, progress=task.progress)
            res["plan_ready"] = plan["ready"]
            d, m = res.get("discover") or {}, res.get("measure") or {}
            parts = []
            if d.get("new"):
                parts.append("%d neue Dateien" % d["new"])
            if m.get("measured"):
                parts.append("%d vermessen" % m["measured"])
            if m.get("errors"):
                parts.append("%d nicht lesbar" % m["errors"])
            task.progress(step="Fertig", message=", ".join(parts) or "alles aktuell – nichts Neues")
            self._notify("project", id=pid)
            self.prepare_media(pid)
            insights.forget(pid)
            return res
        return self.tasks.submit("refresh", "%s prüfen%s" % (name, (" (%s)" % reason) if reason else ""), work,
                                 lane="analysis", project_id=pid, key="refresh:%d" % pid)

    def prepare_media(self, project) -> tasks.Task:
        """Große Vorschauen und Sterne für die Großansicht im Hintergrund nachholen (Spur ``misc``)."""
        pid = self._pid(project)
        name = self.db.value("SELECT name FROM projects WHERE id = ?", (pid,))

        def work(task: tasks.Task):
            r = flow.prepare_fast(self.db, self.ws, pid, progress=task.progress, cancel=lambda: task.cancelled,
                                  workers=self.workers)
            if r["done"]:
                self._notify("project", id=pid)
            return r
        return self.tasks.submit("media", "%s: Großansicht vorbereiten" % name, work, lane="misc", project_id=pid,
                                 key="media:%d" % pid)

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

    def frame_info(self, file_id: int) -> dict:
        """Alles zu einem Frame für die Großansicht: Header (Kamera, Gain, Temperatur, Koordinaten …) und Messwerte
        (FWHM, Sterne, Mond, Objekthöhe …)."""
        r = self.db.one("SELECT f.id, f.path, f.size, f.meta_json, m.data_json FROM files f LEFT JOIN frame_metrics m "
                        "ON m.file_id = f.id WHERE f.id = ?", (int(file_id),))
        if not r:
            raise ValueError("Frame nicht gefunden.")
        meta = json.loads(r["meta_json"] or "{}")
        data = json.loads(r["data_json"] or "{}") if r["data_json"] else {}
        keep_m = ("source", "object", "filter", "exposure", "gain", "offset", "readout", "binning", "width", "height",
                  "bayer", "ccd_temp", "set_temp", "date_obs", "night", "camera", "telescope", "focal_length",
                  "pixel_size", "focus_pos", "focus_temp", "rotator", "pier_side", "ra", "dec", "alt", "airmass",
                  "ambient_temp", "dew_point", "humidity", "wind", "software", "hfr_from_name", "site_fallback")
        keep_d = ("moon_illum", "moon_alt", "alt", "snr_proxy", "fwhm", "fwhm_c", "fwhm_corner", "ecc", "nstars",
                  "bg", "noise", "grad", "sat_frac", "pixscale", "fwhm_as", "hotpixels")
        return dict(id=r["id"], size=r["size"], **{k: meta.get(k) for k in keep_m if meta.get(k) not in (None, "")},
                    measured={k: data.get(k) for k in keep_d if data.get(k) is not None})

    def task_log(self, task_id: str, lines: int = 120) -> dict:
        """Die letzten Zeilen des WBPP-Protokolls eines laufenden Stack-Auftrags (Live-Protokoll)."""
        t = self.tasks.get(task_id)
        if t is None:
            raise ValueError("Auftrag nicht gefunden.")
        d = (t.detail or {}).get("logdir")
        if not d or not os.path.isdir(d):
            return dict(lines=[], file=None)
        from . import wbpplog
        log = wbpplog.newest_log(d)
        if not log:
            return dict(lines=[], file=None)
        n = max(10, min(int(lines), 1000))
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            text = fh.read().decode("utf-8", "replace")
        out = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()][-n:]
        return dict(lines=out, file=os.path.basename(log), size=size)

    def insights(self, project) -> dict:
        """Nachtübersicht, Rig-Hinweise (Fokus, Nachführung, Dithering, Taupunkt …), Himmelsbahnen und Kennzahlen –
        dieselben Auswertungen wie der Bericht aus 0.10, aus den gespeicherten Messwerten."""
        return insights.build(self.db, self._pid(project), self.ws)

    def report_html(self, project) -> str:
        """Ausführlicher Bericht (eigene HTML-Seite, wie in 0.10) mit Nächten, Mond, Qualität und Diagrammen."""
        return insights.report_html(self.db, self._pid(project), self.ws)

    def decide(self, project, file_ids: list[int], decision: str, reason: str = "") -> dict:
        pid = self._pid(project)
        n = flow.decide(self.db, pid, [int(x) for x in file_ids], decision, reason)
        if decision == "auto":
            flow.assess_project(self.db, pid)
        plan = fplan.update_plan(self.db, pid, self.ws.root)
        self._notify("project", id=pid)
        return dict(changed=n, plan_ready=plan["ready"])

    # ------------------------------------------------------------ Frames löschen (einzige Schreibaktion auf Rohdaten)
    def deletion_list(self, project) -> dict:
        """Zum Löschen vorgemerkte Frames mit Größe, Papierkorb ja/nein und Hinderungsgrund."""
        pid = self._pid(project)
        rows = self.db.query(
            "SELECT f.id, f.path, f.type, f.filter, f.night, f.size FROM files f JOIN file_status st "
            "ON st.file_id = f.id AND st.project_id = ? WHERE st.status = 'delete' AND f.missing_since IS NULL "
            "ORDER BY f.date_obs, f.path", (pid,))
        out, total, trash_dirs = [], 0, {}
        for r in rows:
            d = dict(r, name=os.path.basename(r["path"]), exists=os.path.isfile(r["path"]))
            folder = os.path.dirname(r["path"])
            if folder not in trash_dirs:
                trash_dirs[folder] = trash.can_trash(r["path"]) if d["exists"] else False
            d["trash"] = trash_dirs[folder]
            other = self.db.query(
                "SELECT p.name FROM project_files pf JOIN projects p ON p.id = pf.project_id "
                "LEFT JOIN file_status st ON st.project_id = pf.project_id AND st.file_id = pf.file_id "
                "WHERE pf.file_id = ? AND pf.project_id != ? AND COALESCE(st.status, 'use') != 'delete'", (r["id"], pid))
            d["blocked"] = ("wird auch im Projekt „%s“ verwendet" % other[0]["name"]) if other else (
                None if d["exists"] else "Datei nicht erreichbar")
            if not d["blocked"]:
                total += r["size"] or 0
            out.append(d)
        ok = [d for d in out if not d["blocked"]]
        return dict(frames=out, count=len(ok), bytes=total, permanent=sum(1 for d in ok if not d["trash"]))

    def delete_frames(self, project, file_ids: list[int], confirm_count: int, allow_permanent: bool = False,
                      origin: str = "ui") -> dict:
        """Vorgemerkte Frames löschen – nur nach Bestätigung in der Oberfläche, nie per MCP.

        ``confirm_count`` muss der Anzahl entsprechen, die die Oberfläche angezeigt hat. Ohne Papierkorb (NAS)
        wird nur mit ``allow_permanent`` gelöscht. Jede Löschung landet im Verlauf und im Protokoll."""
        if origin == "mcp":
            raise ValueError("Frames löschen ist nur in der Oberfläche möglich.")
        pid = self._pid(project)
        if any(t.lane == "stack" for t in self.tasks.active(pid)):
            raise ValueError("Während dieses Projekt gestackt wird, werden keine Frames gelöscht.")
        lst = {d["id"]: d for d in self.deletion_list(pid)["frames"]}
        want = [int(x) for x in file_ids]
        chosen = [lst[i] for i in want if i in lst and not lst[i]["blocked"]]
        if not chosen or len(chosen) != int(confirm_count) or len(chosen) != len(set(want)):
            raise ValueError("Die Auswahl hat sich geändert – bitte die Liste neu öffnen und erneut bestätigen.")
        if any(not d["trash"] for d in chosen) and not allow_permanent:
            raise ValueError("Ein Teil der Frames liegt an einem Ort ohne Papierkorb – endgültiges Löschen muss "
                             "ausdrücklich bestätigt werden.")
        done, failed, size, mode = [], [], 0, {"trash": 0, "permanent": 0}
        now = dbm.now()
        for d in chosen:
            try:
                how = trash.remove(d["path"], permanent=not d["trash"])
            except OSError as e:
                failed.append(dict(id=d["id"], name=d["name"], error=str(e) or type(e).__name__))
                continue
            mode[how] += 1
            size += d["size"] or 0
            done.append(d)
            logbook.event("frame_deleted", path=d["path"], how=how, project=pid)
        with self.db.transaction():
            for d in done:
                self.db.execute("UPDATE files SET missing_since = ? WHERE id = ?", (now, d["id"]))
            if done:
                self.db.event("frames_deleted", pid, json.dumps(dict(
                    n=len(done), bytes=size, trash=mode["trash"], permanent=mode["permanent"],
                    names=[d["name"] for d in done[:50]], failed=len(failed))))
        if done:
            fplan.update_plan(self.db, pid, self.ws.root)
            self._notify("project", id=pid)
            self._notify("projects")
        return dict(deleted=len(done), bytes=size, trash=mode["trash"], permanent=mode["permanent"], failed=failed)

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
        opts = self._stack_options()
        if opts["min_free_ram_gb"]:
            from . import system
            free = system.ram_gb()[0]
            if free is not None and free < opts["min_free_ram_gb"]:
                raise ValueError("Nur %.1f GB Arbeitsspeicher frei – zum Stacken mindestens %.0f GB (Einstellungen). "
                                 "Bitte andere Programme schließen." % (free, opts["min_free_ram_gb"]))

        def work(task: tasks.Task):
            import contextlib

            from . import system
            kw = dict(runner=self.stack_runner) if self.stack_runner else {}
            with (system.KeepAwake() if opts["keep_awake"] else contextlib.nullcontext()):
                res = stacking.stack_project(self.db, self.ws, pid, self.pixinsight, run_ids, progress=task.progress,
                                             cancel=task.cancel_event, origin=origin, **kw)
            self._notify("project", id=pid)
            done = [r for r in res if r["status"] == "done"]
            for i, r in enumerate(done):                         # Qualität + Vorschau gleich messen
                task.progress(step="Qualität messen", done=i, total=len(done), message=r["run"])
                r["quality"] = {k: v for k, v in stackview.measure(self.db, self.ws, r["stack_id"]).items()
                                if k in ("fwhm", "fwhm_unit", "noise", "stars", "ecc", "gradient_pct", "error")}
            if done:
                try:                                             # neue Master → Plan zeigt die Wiederverwendung
                    fplan.update_plan(self.db, pid, self.ws.root)
                except Exception:  # noqa: BLE001
                    pass
                self._notify("project", id=pid)
                self._notify("projects")
                if opts["open_results"]:
                    try:
                        self.open_in_pixinsight(pid, done[0]["stack_id"], popen=self.result_opener)
                    except (ValueError, OSError) as e:
                        logbook.event("open_results_failed", error=str(e))
            return res
        title = "%s stacken" % name + (" · per Claude" if origin == "mcp" else "")
        return self.tasks.submit("stack", title, work, lane="stack", project_id=pid, origin=origin).as_dict()

    def stacks(self, project, measure: bool = True) -> list[dict]:
        """Alle Stacks, neueste zuerst: Dateien (mit „fehlt“), Größe, Qualität, Vorschau, Notiz.
        Fehlen bei fertigen Stacks die Qualitätswerte, werden sie im Hintergrund nachgerechnet."""
        pid = self._pid(project)
        cover = self.db.value("SELECT cover_asset_id FROM projects WHERE id = ?", (pid,))
        out, todo = [], []
        for r in self.db.query("SELECT * FROM stacks WHERE project_id = ? ORDER BY created_at DESC, id DESC", (pid,)):
            if stackview.needs_measure(r):
                todo.append(int(r["id"]))
            d = dict(r)
            d["settings"] = json.loads(d.pop("settings_json") or "{}")
            d["quality"] = json.loads(d.pop("quality_json") or "null")
            files = self.db.query("SELECT id, path, size, owned FROM assets WHERE stack_id = ? AND hidden = 0 "
                                  "ORDER BY owned DESC, id", (r["id"],))
            d["imported"] = bool(d["settings"].get("imported"))
            d["files"] = [a["path"] for a in files if a["owned"] or d["imported"]]
            d["file_info"] = [dict(id=a["id"], path=a["path"], name=os.path.basename(a["path"]), size=a["size"],
                                   owned=bool(a["owned"]), exists=os.path.isfile(a["path"])) for a in files]
            d["missing"] = bool(d["files"]) and not any(f["exists"] for f in d["file_info"]
                                                        if f["owned"] or d["imported"])
            d["hours"] = round((d.get("exposure_s") or 0) / 3600, 2)
            q = d["quality"] or {}
            d["preview_url"] = ("/api/stacks/%d/preview?v=%s" % (r["id"], q["preview"])) if q.get("preview") else None
            master = stackview.light_master(self.db, int(r["id"]))
            d["master_asset_id"] = self.db.value("SELECT id FROM assets WHERE stack_id = ? AND path = ?",
                                                 (r["id"], master)) if master else None
            d["cover"] = bool(cover and d["master_asset_id"] == cover)
            out.append(d)
        if todo and measure:
            self.measure_stacks(pid, todo)
        return out

    def measure_stacks(self, project, stack_ids: list[int] | None = None) -> tasks.Task:
        """Qualität und Vorschau für Stacks ohne Werte im Hintergrund berechnen (Spur ``misc``)."""
        pid = self._pid(project)

        def work(task: tasks.Task):
            ids = stack_ids or [int(r["id"]) for r in self.db.query("SELECT * FROM stacks WHERE project_id = ?", (pid,))
                                if stackview.needs_measure(r)]
            for i, sid in enumerate(ids):
                task.check_cancel()
                task.progress(step="Stack-Qualität messen", done=i, total=len(ids))
                stackview.measure(self.db, self.ws, sid)
            self._notify("project", id=pid)
            self._notify("projects")
            return dict(measured=len(ids))
        name = self.db.value("SELECT name FROM projects WHERE id = ?", (pid,))
        return self.tasks.submit("quality", "%s: Stacks vermessen" % name, work, lane="misc", project_id=pid,
                                 key="quality:%d" % pid)

    def import_stacks(self, project, paths: list[str]) -> dict:
        """Vorhandene Stacks übernehmen (0.10-Projektordner, WBPP-Ausgabe oder einzelne Master-Dateien).
        Die Dateien bleiben, wo sie sind; Vorschau und Qualität werden danach im Hintergrund berechnet."""
        from . import stackimport
        pid = self._pid(project)
        res = stackimport.import_stacks(self.db, pid, paths)
        if res["added"]:
            self.measure_stacks(pid, res["added"])
        self._notify("project", id=pid)
        self._notify("projects")
        return res

    def note(self, project, stack_id: int, text: str) -> None:
        pid = self._pid(project)
        stacking.set_note(self.db, pid, int(stack_id), text)
        self._notify("project", id=pid)

    def delete_stack(self, project, stack_id: int) -> list[str]:
        pid = self._pid(project)
        if any(t.lane == "stack" for t in self.tasks.active(pid)):
            raise ValueError("Während dieses Projekt gestackt wird, kann kein Stack gelöscht werden.")
        removed = stacking.delete_stack(self.db, self.ws, pid, int(stack_id))
        stackview.forget(self.ws, int(stack_id))
        self._notify("project", id=pid)
        self._notify("projects")
        return removed

    def compare(self, project, a: int, b: int) -> dict:
        """Zwei Stacks nebeneinander: Werte (besser/schlechter) und ausgerichtete Vorschauen mit gleichem Strich."""
        pid = self._pid(project)
        try:
            out = stackview.compare(self.db, self.ws, pid, int(a), int(b))
        except stackview.StackViewError as e:
            raise ValueError(str(e)) from None
        imgs = out.pop("images") or None
        if imgs:
            out["image_a"] = "/api/projects/%d/compare/%d/%d/a?v=%s" % (pid, int(a), int(b), imgs["version"])
            out["image_b"] = "/api/projects/%d/compare/%d/%d/b?v=%s" % (pid, int(a), int(b), imgs["version"])
        return out

    def compare_image(self, project, a: int, b: int, side: str) -> tuple[str, str]:
        pid = self._pid(project)
        rows = {r["id"]: json.loads(r["quality_json"] or "null") or {} for r in self.db.query(
            "SELECT id, quality_json FROM stacks WHERE project_id = ? AND id IN (?, ?)", (pid, int(a), int(b)))}
        if int(a) not in rows or int(b) not in rows:
            raise FileNotFoundError("Stack nicht gefunden.")
        pa, pb, meta = stackview.compare_paths(self.ws, int(a), int(b), rows[int(a)], rows[int(b)])
        if not (os.path.isfile(pa) and os.path.isfile(pb)):
            self.compare(pid, a, b)
        return (pa if side == "a" else pb), os.path.basename(meta)[:-5]

    def stack_preview(self, stack_id: int) -> tuple[str, str]:
        q = json.loads(self.db.value("SELECT quality_json FROM stacks WHERE id = ?", (int(stack_id),)) or "null")
        p = stackview.preview_path(self.ws, q)
        if not p:
            raise FileNotFoundError("Für diesen Stack gibt es (noch) keine Vorschau.")
        return p, q["preview"]

    def run_script(self, script: str, args: list[str] | None = None, timeout_min: float | None = None,
                   origin: str = "ui") -> dict:
        """Eigenes PixInsight-Skript aus dem Arbeitsverzeichnis als Auftrag in der Spur ``stack`` (nie parallel
        zum Stacken). Ausgabe zeilenweise als Meldung des Auftrags, vollständig in ``logs/pixinsight-scripts``."""
        from . import piscript
        if not self.pixinsight or not os.path.isfile(self.pixinsight):
            raise ValueError("PixInsight nicht gefunden – bitte in den Einstellungen angeben.")
        try:
            path, argv, notes = piscript.check(script, self.ws.root, args)
        except piscript.ScriptError as e:
            raise ValueError(str(e)) from None
        runner = self.script_runner or piscript.run

        def work(task: tasks.Task):
            task.progress(step="PixInsight-Skript: %s" % os.path.basename(path))
            res = runner(self.pixinsight, path, argv, self.ws.root, log=lambda line: task.progress(message=line),
                         cancel=task.cancel_event, timeout_s=float(timeout_min or 30) * 60)
            res["notes"] = notes
            return res
        title = "Skript %s" % os.path.basename(path) + (" · per Claude" if origin == "mcp" else "")
        return self.tasks.submit("script", title, work, lane="stack", origin=origin).as_dict()

    # ------------------------------------------------------------ Galerie
    def gallery(self, project) -> list[dict]:
        pid = self._pid(project)
        out = gallery.listing(self.db, pid, self.ws.project_dir(self.db.value("SELECT folder FROM projects WHERE id = ?",
                                                                                (pid,))))
        for a in out:
            a["thumb_url"] = "/api/assets/%d/thumb?v=%s" % (a["id"], a["version"])
            a["view_url"] = "/api/assets/%d/view?v=%s" % (a["id"], a["version"])
        # fertige Stacks gehören auch in die Galerie (als Stack gekennzeichnet) – zum direkten Vergleich mit dem
        # bearbeiteten Bild
        for st in self.stacks(pid, measure=False):
            if st["status"] != "done" or not st.get("master_asset_id"):
                continue
            master = next((f for f in st["file_info"] if f["id"] == st["master_asset_id"]), None)
            if master is None:
                continue
            out.append(dict(id=st["master_asset_id"], stack=True, stack_id=st["id"], stack_name=st["name"],
                            name=st["name"], path=master["path"], size=master["size"],
                            ext=os.path.splitext(master["path"])[1].lower().lstrip("."),
                            added_at=st["finished_at"] or st["created_at"], cover=st["cover"],
                            missing=not master["exists"], place="Stack",
                            version=(st.get("quality") or {}).get("preview") or "",
                            thumb_url=st["preview_url"], view_url=st["preview_url"]))
        out.sort(key=lambda a: a.get("added_at") or "", reverse=True)
        return out

    def add_images(self, project, paths: list[str], origin: str = "ui") -> dict:
        pid = self._pid(project)
        res = gallery.add(self.db, self.ws, pid, paths, origin=origin)
        self._notify("project", id=pid)
        return res

    def hide_image(self, project, asset_id: int) -> None:
        pid = self._pid(project)
        gallery.hide(self.db, pid, int(asset_id))
        self._notify("project", id=pid)
        self._notify("projects")

    def set_cover(self, project, asset_id: int | None) -> None:
        pid = self._pid(project)
        gallery.set_cover(self.db, pid, int(asset_id) if asset_id is not None else None)
        self._notify("projects")
        self._notify("project", id=pid)

    def asset_info(self, asset_id: int) -> dict:
        """Bild-Infos für die Galerie: Maße, Bittiefe, Header/EXIF, gemessene Sterne/FWHM/Rauschen."""
        pid = self.db.value("SELECT project_id FROM assets WHERE id = ?", (int(asset_id),))
        scale = None
        if pid:
            vals = []
            for r in self.db.query("SELECT m.data_json FROM frame_metrics m JOIN project_files pf ON pf.file_id = "
                                   "m.file_id WHERE pf.project_id = ? LIMIT 50", (pid,)):
                try:
                    v = json.loads(r["data_json"] or "{}").get("pixscale")
                    if v:
                        vals.append(float(v))
                except (ValueError, TypeError):
                    pass
            scale = sorted(vals)[len(vals) // 2] if vals else None
        return gallery.info(self.db, self.ws, int(asset_id), scale)

    def asset_thumb(self, asset_id: int, large: bool = False) -> tuple[str, str]:
        return gallery.thumb(self.db, self.ws, int(asset_id), gallery.VIEW_W if large else gallery.THUMB_W)

    # ------------------------------------------------------------ History
    def timeline(self, project=None, limit: int = 100) -> list[dict]:
        """Verlauf in Klartext, zusammengefasst (allgemein oder für ein Projekt)."""
        return history.timeline(self.db, self._pid(project) if project is not None else None, limit)

    def history(self, project=None, limit: int = 200) -> list[dict]:
        """Rohe Ereignisse (für Fehlersuche, MCP ``logs``)."""
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
        path = dbm.backup(self.db, self.ws.backups)
        self._mirror_backups()
        return path

    def _mirror_backups(self) -> None:
        """Sicherungen zusätzlich an den gewählten Ort kopieren (dort ebenfalls die letzten 10)."""
        extra = (self.backup_dir or "").strip()
        if not extra or os.path.normcase(os.path.abspath(extra)) == os.path.normcase(self.ws.backups):
            return
        import shutil
        try:
            os.makedirs(extra, exist_ok=True)
            have = {os.path.basename(p) for p in dbm.list_backups(extra)}
            for p in dbm.list_backups(self.ws.backups)[:dbm.store.BACKUP_KEEP]:
                if os.path.basename(p) not in have:
                    shutil.copy2(p, os.path.join(extra, os.path.basename(p) + ".tmp"))
                    os.replace(os.path.join(extra, os.path.basename(p) + ".tmp"), os.path.join(extra, os.path.basename(p)))
            for old in dbm.list_backups(extra)[dbm.store.BACKUP_KEEP:]:
                os.remove(old)
        except OSError as e:                          # Ort nicht erreichbar: die Sicherung in system/ zählt
            from . import logbook
            logbook.event("backup_mirror_failed", target=extra, error=str(e))

    def backups(self) -> list[dict]:
        """Vorhandene Sicherungen (system/backup und der gewählte Ort), neueste zuerst."""
        seen, out = set(), []
        for where, d in (("system", self.ws.backups), ("extra", self.backup_dir)):
            if not d:
                continue
            for p in dbm.list_backups(d):
                name = os.path.basename(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if name in seen:
                    next(o for o in out if o["name"] == name)["places"].append(where)
                    continue
                seen.add(name)
                m = dbm.store._BACKUP_RE.match(name)
                stamp = m.group(1) if m else ""
                out.append(dict(name=name, path=p, size=st.st_size, places=[where], _key=(stamp, name),
                                at="%s-%s-%sT%s:%s:%s" % (stamp[0:4], stamp[4:6], stamp[6:8], stamp[9:11], stamp[11:13],
                                                          stamp[13:15]) if len(stamp) >= 15 else None,
                                label=name[len("b2stacker-") + len(stamp):-3].strip("-") if stamp else ""))
        out.sort(key=lambda o: o["_key"], reverse=True)
        for o in out:
            del o["_key"]
        return out

    def find_backup(self, name: str) -> str:
        for b in self.backups():
            if b["name"] == name:
                return b["path"]
        raise ValueError("Sicherung nicht gefunden: %s" % name)

    # ------------------------------------------------------------ Selbsttest, PixInsight öffnen
    def selftest(self, folder: str = "", origin: str = "ui") -> dict:
        """Selbsttest mit echtem PixInsight als Auftrag in der Spur ``stack`` (nie parallel zum Stacken).
        ``folder``: wo die Testdaten angelegt werden (z. B. ein NAS-Ordner); Standard: Arbeitsverzeichnis."""
        from . import selftest_neu
        folder = (folder or "").strip() or self.ws.root
        if not os.path.isdir(folder):
            raise ValueError("Ordner nicht gefunden: %s" % folder)

        def work(task: tasks.Task):
            kw = dict(runner=self.stack_runner) if self.stack_runner else {}
            res = selftest_neu.run(self.pixinsight, self.ws.root, folder, progress=task.progress,
                                   cancel=task.cancel_event, **kw)
            logbook.event("selftest", ok=res.get("ok"), report=res.get("report"), environment=res.get("environment"),
                          steps=res.get("steps"), ui="neu")
            return res
        title = "Selbsttest" + (" · per Claude" if origin == "mcp" else "")
        return self.tasks.submit("selftest", title, work, lane="stack", origin=origin).as_dict()

    def selftest_reports(self, limit: int = 5) -> list[dict]:
        d = os.path.join(self.ws.root, "selbsttest")
        out = []
        try:
            names = sorted((n for n in os.listdir(d) if n.startswith("bericht-") and n.endswith(".json")), reverse=True)
        except OSError:
            return []
        for n in names[:limit]:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as fh:
                    r = json.load(fh)
            except (OSError, ValueError):
                continue
            out.append(dict(name=n, path=os.path.join(d, n), ok=r.get("ok"), started=r.get("started"),
                            duration_s=r.get("duration_s"), steps=r.get("steps") or [], kind=r.get("kind") or "alt",
                            environment={k: (r.get("environment") or {}).get(k) for k in
                                         ("pixinsight_version", "test_folder", "test_folder_remote")}))
        return out

    def open_stack_folder(self, project, stack_id: int, opener=None) -> str:
        """Ordner eines Stacks (bei Fehlschlag: mit dem WBPP-Protokoll) im Explorer öffnen."""
        import subprocess
        import sys
        pid = self._pid(project)
        row = self.db.one("SELECT s.folder, p.folder AS pfolder FROM stacks s JOIN projects p ON p.id = s.project_id "
                          "WHERE s.id = ? AND s.project_id = ?", (int(stack_id), pid))
        if not row or not row["folder"]:
            raise ValueError("Zu diesem Stack gibt es keinen Ordner.")
        path = os.path.join(self.ws.project_dir(row["pfolder"]), row["folder"])
        if not os.path.isdir(path):
            raise ValueError("Ordner nicht gefunden: %s" % path)
        if opener:
            opener(path)
        elif sys.platform == "win32":
            os.startfile(path)  # noqa: S606 – Explorer öffnen
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path

    def open_in_pixinsight(self, project, stack_id: int | None = None, popen=None) -> dict:
        """Die Master eines Stacks (Standard: neuester fertiger) in PixInsight öffnen."""
        import subprocess
        pid = self._pid(project)
        if not self.pixinsight or not os.path.isfile(self.pixinsight):
            raise ValueError("PixInsight nicht gefunden – bitte in den Einstellungen angeben.")
        if stack_id is None:
            stack_id = self.db.value("SELECT id FROM stacks WHERE project_id = ? AND status = 'done' "
                                     "ORDER BY created_at DESC, id DESC LIMIT 1", (pid,))
        if stack_id is None or not self.db.one("SELECT 1 FROM stacks WHERE id = ? AND project_id = ?", (stack_id, pid)):
            raise ValueError("Kein Stack gefunden – erst stacken.")
        files = [r["path"] for r in self.db.query("SELECT path FROM assets WHERE stack_id = ? AND owned = 1 AND hidden = 0 "
                                                   "ORDER BY id", (int(stack_id),)) if os.path.isfile(r["path"])]
        if not files:
            raise ValueError("Die Dateien dieses Stacks fehlen.")
        (popen or subprocess.Popen)([self.pixinsight] + files)
        logbook.event("open_in_pixinsight", project=pid, stack=stack_id, files=len(files))
        return dict(opened=files, stack_id=int(stack_id))

    # ------------------------------------------------------------ Kalibrier-Bibliothek
    def calibration_library(self) -> list[dict]:
        """Alle gespeicherten Master-Bias/-Darks/-Flats über alle Projekte. Ein Master wird in jedem Projekt
        wiederverwendet, dessen Kalibrierframes genau dieselben Rohdateien sind (gleicher Schlüssel)."""
        out = []
        for r in self.db.query("SELECT m.*, p.name AS project FROM masters m JOIN projects p ON p.id = m.project_id "
                               "ORDER BY m.type, m.created_at DESC"):
            info = json.loads(r["key_json"] or "{}")
            used = self.db.query("SELECT DISTINCT p.id, p.name FROM stack_masters sm JOIN stacks s ON s.id = sm.stack_id "
                                 "JOIN projects p ON p.id = s.project_id WHERE sm.master_id = ? ORDER BY p.name", (r["id"],))
            nstacks = self.db.value("SELECT COUNT(*) FROM stack_masters WHERE master_id = ?", (r["id"],))
            exists = os.path.isfile(r["path"])
            out.append(dict(id=r["id"], type=r["type"], path=r["path"], name=os.path.basename(r["path"]),
                            project_id=r["project_id"], project=r["project"], created_at=r["created_at"],
                            exists=exists, size=os.path.getsize(r["path"]) if exists else None, info=info,
                            stacks=nstacks, projects=[dict(u) for u in used]))
        return out

    def calibration_frames(self) -> list[dict]:
        """Alle rohen Kalibrierframes (Flats, Bias, Darks, Dark-Flats) aus allen Projekten, gruppiert wie sie
        zusammenpassen: Typ, Kamera, Gain/Offset, Belichtung, Binning, Temperatur – Flats zusätzlich je Filter
        und Nacht. Je Gruppe: Anzahl, Nächte, Projekte, Größe, fehlende Dateien."""
        rows = self.db.query(
            "SELECT f.id, f.type, f.filter, f.exposure, f.gain, f.\"offset\", f.binning, f.ccd_temp, f.night, f.size, "
            "f.missing_since, f.width, f.height, f.date_obs, COALESCE(s.camera, '') AS camera, "
            "GROUP_CONCAT(DISTINCT p.name) AS projects "
            "FROM files f LEFT JOIN setups s ON s.id = f.setup_id "
            "LEFT JOIN project_files pf ON pf.file_id = f.id LEFT JOIN projects p ON p.id = pf.project_id "
            "WHERE f.type IN ('flat', 'bias', 'dark', 'darkflat') AND f.is_master = 0 GROUP BY f.id")
        groups: dict = {}
        for r in rows:
            temp = None if r["ccd_temp"] is None else round(r["ccd_temp"])
            exp = None if r["exposure"] is None else round(r["exposure"], 3)
            key = (r["type"], r["camera"], r["gain"], r["offset"], r["binning"], exp,
                   temp if r["type"] in ("dark", "darkflat", "bias") else None,
                   (r["filter"] or "") if r["type"] == "flat" else "", r["night"] if r["type"] == "flat" else "")
            g = groups.get(key)
            if g is None:
                g = groups[key] = dict(type=r["type"], camera=r["camera"], gain=r["gain"], offset=r["offset"],
                                       binning=r["binning"], exposure=exp, temp=temp, filter=r["filter"] or "",
                                       night=r["night"] or "", size=0, count=0, missing=0, nights=set(),
                                       projects=set(), first=r["date_obs"], last=r["date_obs"],
                                       width=r["width"], height=r["height"])
            g["count"] += 1
            g["size"] += r["size"] or 0
            g["missing"] += 1 if r["missing_since"] else 0
            if r["night"]:
                g["nights"].add(r["night"])
            for pn in (r["projects"] or "").split(","):
                if pn:
                    g["projects"].add(pn)
            if r["date_obs"]:
                g["first"] = min(filter(None, (g["first"], r["date_obs"])))
                g["last"] = max(filter(None, (g["last"], r["date_obs"])))
        order = {"flat": 0, "bias": 1, "dark": 2, "darkflat": 3}
        out = []
        for g in groups.values():
            g["nights"] = sorted(g["nights"])
            g["projects"] = sorted(g["projects"])
            out.append(g)
        out.sort(key=lambda g: g["last"] or "", reverse=True)          # neueste zuerst …
        out.sort(key=lambda g: (order.get(g["type"], 9), g["camera"]))   # … innerhalb von Typ und Kamera
        return out

    def delete_master(self, master_id: int) -> dict:
        """Gespeicherten Master vergessen und seine Datei löschen (eine von b2stacker erzeugte Datei, keine Rohdaten).
        Beim nächsten Stack entsteht er bei Bedarf neu aus den Rohframes."""
        if [t for t in self.tasks.active() if t.lane == "stack"]:
            raise ValueError("Während gestackt wird, bleibt die Kalibrier-Bibliothek unverändert.")
        r = self.db.one("SELECT * FROM masters WHERE id = ?", (int(master_id),))
        if not r:
            raise ValueError("Master nicht gefunden.")
        root = os.path.realpath(self.ws.root)
        p = os.path.realpath(r["path"])
        if os.path.isfile(p) and os.path.commonpath([root, p]) == root:   # nur im Arbeitsverzeichnis
            try:
                os.remove(p)
            except PermissionError:
                raise ValueError("Die Datei ist gerade geöffnet – bitte schließen und erneut versuchen.") from None
        with self.db.transaction():
            self.db.execute("DELETE FROM masters WHERE id = ?", (r["id"],))
            self.db.event("master_deleted", r["project_id"], json.dumps(dict(type=r["type"], name=os.path.basename(p))))
        self._notify("library")
        return dict(ok=True)

    def clear_temp(self) -> int:
        if [t for t in self.tasks.active() if t.lane == "stack"]:
            raise ValueError("Während gestackt wird, bleibt der Temp-Ordner unangetastet.")
        return self.ws.clear_temp()


class RestoreError(ValueError):
    """Zurückspielen gescheitert; ``core`` ist der wieder geöffnete bisherige Stand."""

    def __init__(self, msg: str, core: Core):
        super().__init__(msg)
        self.core = core


def restore(core: Core, name: str) -> Core:
    """Datenbank aus einer Sicherung zurückspielen und eine frische ``Core`` liefern.

    Ablauf (robust, ohne Neustart des Programms): Stacken läuft → abgelehnt. Sonst alle Aufträge abbrechen, die
    gewählte Sicherung an einen sicheren Ort kopieren (damit das Aufräumen alter Sicherungen sie nicht erwischt),
    den aktuellen Stand als „vor-wiederherstellen“ sichern, alle Verbindungen schließen, zurückspielen (SQLite-
    Backup-API, auch WAL-sicher) und neu öffnen. Die alte ``Core`` ist danach geschlossen."""
    import shutil
    src = core.find_backup(name)
    if any(t.lane == "stack" for t in core.tasks.active()):
        raise ValueError("Während gestackt wird, kann keine Sicherung zurückgespielt werden.")
    for t in core.tasks.active():
        core.tasks.cancel(t.id)
    if not core.tasks.wait_idle(30):
        raise ValueError("Laufende Aufträge enden nicht – bitte kurz warten und erneut versuchen.")
    tmp = os.path.join(core.ws.system, "restore-source.db")
    shutil.copy2(src, tmp)
    try:
        dbm.backup(core.db, core.ws.backups, label="vor-wiederherstellen")
        args = dict(pixinsight=core.pixinsight, workers=core.workers, stack_runner=core.stack_runner,
                    backup_dir=core.backup_dir)
        root, db_path = core.ws.root, core.ws.db_path
        core.close()
        try:
            dbm.restore_backup(tmp, db_path)
        except Exception as e:  # noqa: BLE001 – z. B. beschädigte Sicherung: bisherigen Stand wieder öffnen
            raise RestoreError("Zurückspielen nicht möglich (%s) – es gilt weiter der bisherige Stand." % e,
                               Core(root, **args)) from e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    new = Core(root, **args)
    with new.db.transaction():
        new.db.event("backup_restored", None, json.dumps(dict(backup=name)))
    return new
