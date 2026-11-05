"""MCP-Werkzeuge des neuen Servers – direkt auf der Dienst-Schicht ``Core`` (kein Umweg über HTTP).

Wird von ``/mcp`` des neuen Servers benutzt (``web/server.py``); die alte App behält ``astrostack/mcp.py``.
Gleiche Regeln wie dort:
- Kein Werkzeug löscht, verschiebt oder verändert Bilddateien (auch keine Stacks); Frame-Entscheidungen und
  Planänderungen sind umkehrbar.
- Aufträge, die über MCP starten, tragen „· per Claude“ im Titel (``origin="mcp"``).
- Antworten bleiben kompakt (lange Listen gekürzt), damit die KI schnell bleibt.
"""
from __future__ import annotations

import os
import time

from .. import __version__, logbook, mcp

ToolError = mcp.ToolError
MAX_WAIT = mcp.MAX_WAIT

INSTRUCTIONS = """b2stacker sichtet Astro-Rohdaten (FITS/XISF), bewertet jeden Frame und stackt mit PixInsight (WBPP).
Alles Automatische (neue Dateien finden, Header lesen, vermessen, bewerten, Plan) läuft von selbst im Hintergrund;
nur Stacken braucht einen Auftrag. Typischer Ablauf: status → list_projects → project (Läufe, Hinweise, Stacks)
→ frames (Problemfälle) → set_frames / change_plan → stack → job (mit wait_s warten). Neue Daten: create_project
oder add_folders. Verlauf in Klartext: history. Stacks mit Qualitätswerten: stacks. Originaldateien werden nie
verändert, verschoben oder gelöscht. Frage den Menschen vor dem Stacken, wenn der Plan unklar ist."""


def _task(t: dict | None) -> dict | None:
    if not t:
        return None
    keep = ("id", "kind", "title", "state", "step", "done", "total", "eta_s", "message", "error", "project_id",
            "origin", "created", "finished")
    return {k: t.get(k) for k in keep if t.get(k) not in (None, "", 0) or k in ("done", "state")}


def _run(r: dict) -> dict:
    return dict(id=r.get("id"), name=r.get("name"), enabled=r.get("enabled"), filter=r.get("filter"),
                lights=r.get("lights"), hours=r.get("hours"), nights=len(r.get("nights") or []),
                why_off=r.get("why_off") or None, problems=r.get("problems") or None,
                flats=[dict(night=f.get("night"), n=len(f.get("file_ids") or []), verdict=f.get("verdict"),
                            reason=f.get("reason")) for f in r.get("flats") or []],
                settings={s["key"]: s["value"] for s in r.get("settings") or []},
                edited=[s["key"] for s in r.get("settings") or [] if s.get("edited")] or None)


def _stack(s: dict) -> dict:
    q = s.get("quality") or {}
    return dict(id=s["id"], name=s["name"], status=s["status"], created_at=s["created_at"], run=s.get("run_label"),
                lights=s.get("frames"), hours=s.get("hours"), size_bytes=s.get("size_bytes"), note=s.get("note") or None,
                error=s.get("error"), files=s.get("files"), missing=s.get("missing") or None,
                quality={k: q.get(k) for k in ("fwhm", "fwhm_unit", "noise", "stars", "ecc", "gradient_pct", "error")
                         if q.get(k) is not None} or None)


class CoreTools:
    """Ein Werkzeug je Methode; ``a`` sind die Argumente des Aufrufs (dict)."""

    def __init__(self, get_core):
        self.get_core = get_core                     # Core kann wechseln (Wiederherstellung, Arbeitsverzeichnis)

    @property
    def c(self):
        return self.get_core()

    def _pid(self, name) -> int:
        if not isinstance(name, (str, int)) or not str(name).strip():
            raise ToolError("Projekt fehlt (siehe list_projects).")
        return self.c._pid(name)

    # -------- lesen
    def status(self, a):
        from ..settings import Settings
        c = self.c
        snap = c.tasks.snapshot()
        return dict(version=__version__, workdir=c.ws.root, pixinsight=c.pixinsight,
                    pixinsight_ok=bool(c.pixinsight) and os.path.isfile(c.pixinsight),
                    projects=c.db.value("SELECT COUNT(*) FROM projects"),
                    running=[_task(t) for t in snap["running"]], queued=[_task(t) for t in snap["queued"]],
                    recent=[_task(t) for t in snap["finished"][:5]],
                    settings_file=Settings.path())

    def list_projects(self, a):
        keep = ("id", "name", "object", "description", "hours", "lights", "filters", "first_light", "last_light",
                "stacked_at", "new_since_stack", "offline", "busy")
        return [{k: p.get(k) for k in keep if p.get(k) not in (None, "", [])} for p in self.c.projects()]

    def project(self, a):
        pid = self._pid(a.get("name") or a.get("project"))
        p = self.c.project(pid)
        plan = p.get("plan") or {}
        return dict(id=pid, name=p["name"], description=p.get("description") or None,
                    folders=[dict(path=f["path"], online=bool(f["online"]), files=f["files"]) for f in p["folders"]],
                    counts=p.get("counts"), missing=p.get("missing") or None, decisions=p.get("decisions") or None,
                    ready=plan.get("ready"), runs=[_run(r) for r in plan.get("runs") or []],
                    notes=plan.get("notes") or None, need_gb=plan.get("need_gb"), free_gb=plan.get("free_gb"),
                    stacks=[_stack(s) for s in (p.get("stacks") or [])[:10]] or None,
                    tasks=[_task(t) for t in p.get("tasks") or []] or None)

    def frames(self, a):
        pid = self._pid(a.get("project"))
        typ = a.get("type") or "light"
        status = a.get("status")
        if status not in (None, "", "use", "skip", "delete"):
            raise ToolError("status: use, skip oder leer")
        lim = max(1, min(int(a.get("limit") or 100), 500))
        r = self.c.frames(pid, type_=typ, status=status or None, offset=int(a.get("offset") or 0), limit=lim)
        rows = r["frames"]
        sort = a.get("sort")
        if sort in ("fwhm", "ecc", "noise", "bg"):
            rows.sort(key=lambda f: (f.get(sort) is None, -(f.get(sort) or 0)))
        elif sort == "stars":
            rows.sort(key=lambda f: f.get("nstars") or 0)
        keep = ("id", "name", "path", "type", "filter", "night", "date_obs", "exposure", "status", "origin", "reason",
                "fwhm", "ecc", "nstars", "bg", "noise", "header_error")
        return dict(project=pid, total=r["total"], offset=r["offset"],
                    frames=[{k: f.get(k) for k in keep if f.get(k) not in (None, "")} for f in rows],
                    more=max(0, r["total"] - r["offset"] - len(rows)) or None)

    def stacks(self, a):
        pid = self._pid(a.get("project"))
        return [_stack(s) for s in self.c.stacks(pid)]

    def history(self, a):
        project = a.get("project")
        items = self.c.timeline(self._pid(project) if project else None, limit=min(int(a.get("limit") or 30), 200))
        return [{k: i.get(k) for k in ("at", "project", "title", "detail", "kind", "count") if i.get(k)} for i in items]

    logs = history

    def job(self, a):
        c = self.c
        tid = a.get("id")
        if not tid:
            snap = c.tasks.snapshot()
            cand = snap["running"] or snap["queued"] or snap["finished"]
            if not cand:
                return dict(job=None, note="Noch kein Auftrag seit dem Programmstart.")
            tid = cand[0]["id"]
        t = c.tasks.get(tid)
        if t is None:
            raise ToolError("Auftrag nicht gefunden: %s" % tid)
        end = time.monotonic() + max(0.0, min(float(a.get("wait_s") or 0), MAX_WAIT))
        while t.state in ("queued", "running") and time.monotonic() < end:
            time.sleep(0.2)
        out = _task(t.as_dict())
        if t.state in ("queued", "running"):
            out["hint"] = "Läuft noch – job erneut mit wait_s aufrufen."
        elif t.result is not None:
            out["result"] = mcp._trim(t.result, max_list=15, max_str=600)
        return out

    # -------- ändern / auslösen
    def create_project(self, a):
        name = (a.get("name") or a.get("title") or "").strip()
        r = self.c.create_project(name, a.get("folders") or [], a.get("description") or "")
        return dict(id=r["id"], job=_task(r["task"]), next="job mit wait_s aufrufen, danach project.")

    def add_folders(self, a):
        pid = self._pid(a.get("project"))
        folders = a.get("folders") or []
        if not folders:
            raise ToolError("folders: mindestens ein Ordner")
        r = self.c.add_folders(pid, folders)
        return dict(id=pid, job=_task(r["task"]))

    def analyze(self, a):
        pid = self._pid(a.get("project"))
        return dict(id=pid, job=_task(self.c.refresh(pid, reason="per Claude").as_dict()), next="job mit wait_s.")

    def set_frames(self, a):
        pid = self._pid(a.get("project"))
        decision = {"in": "use", "out": "skip"}.get(a.get("decision"), a.get("decision"))
        if decision not in ("use", "skip", "auto"):
            raise ToolError("decision muss use (in), skip (out) oder auto sein")
        want = a.get("frames") or []
        if isinstance(want, (str, int)):
            want = [want]
        if not want:
            raise ToolError("frames: mindestens eine Frame-ID, ein Pfad oder ein eindeutiger Dateiname")
        db = self.c.db
        rows = db.query("SELECT f.id, f.path FROM files f JOIN project_files pf ON pf.file_id = f.id "
                        "WHERE pf.project_id = ?", (pid,))
        by_id = {r["id"]: r["id"] for r in rows}
        by_path = {r["path"]: r["id"] for r in rows}
        by_name: dict[str, list[int]] = {}
        for r in rows:
            by_name.setdefault(os.path.basename(r["path"]).lower(), []).append(r["id"])
        ids, unknown = [], []
        for w in want:
            if isinstance(w, int) or (isinstance(w, str) and w.isdigit()):
                hit = [by_id[int(w)]] if int(w) in by_id else []
            else:
                hit = [by_path[w]] if w in by_path else by_name.get(os.path.basename(str(w)).lower(), [])
            if len(hit) == 1:
                ids.append(hit[0])
            else:
                unknown.append(str(w) + (" (mehrdeutig)" if hit else ""))
        if unknown:
            raise ToolError("Nicht gefunden: %s – IDs oder Pfade aus frames verwenden. Nichts geändert."
                            % ", ".join(unknown))
        r = self.c.decide(pid, ids, decision, a.get("reason") or "per Claude")
        return dict(project=pid, changed=r["changed"], decision=decision, plan_ready=r["plan_ready"])

    def change_plan(self, a):
        pid = self._pid(a.get("project"))
        opts, runs = a.get("options"), a.get("runs")
        if opts is not None and not isinstance(opts, dict) or runs is not None and not isinstance(runs, dict):
            raise ToolError("options und runs müssen Objekte sein")
        if opts or runs or a.get("reset"):
            plan = self.c.edit_plan(pid, opts or None, runs or None, bool(a.get("reset")))
        else:
            plan = self.c.plan(pid) or {}
        return dict(project=pid, changed=bool(opts or runs or a.get("reset")), ready=plan.get("ready"),
                    runs=[_run(r) for r in plan.get("runs") or []], need_gb=plan.get("need_gb"),
                    free_gb=plan.get("free_gb"))

    def stack(self, a):
        pid = self._pid(a.get("project"))
        runs = a.get("runs") or None
        t = self.c.stack(pid, runs, origin="mcp")
        return dict(project=pid, job=_task(t), next="job mit wait_s aufrufen; Stacken dauert Minuten bis Stunden.")

    def cancel_job(self, a):
        tid = a.get("id")
        if not tid:
            raise ToolError("id fehlt (siehe status/job)")
        t = self.c.tasks.cancel(tid)
        if t is None:
            raise ToolError("Auftrag nicht gefunden: %s" % tid)
        return dict(ok=True, id=tid, state=t.state)

    def stack_note(self, a):
        pid = self._pid(a.get("project"))
        sid = a.get("stack_id")
        if not isinstance(sid, int):
            raise ToolError("stack_id: Zahl aus stacks")
        self.c.note(pid, sid, str(a.get("text") or ""))
        return dict(ok=True, stack_id=sid)

    def compare_stacks(self, a):
        pid = self._pid(a.get("project"))
        r = self.c.compare(pid, int(a.get("a")), int(a.get("b")))
        return dict(a=r["a"], b=r["b"], metrics=r["metrics"], align=r.get("align"))

    def run_pixinsight_script(self, a):
        args = a.get("args") or []
        if not isinstance(args, list):
            raise ToolError("args muss eine Liste sein")
        t = self.c.run_script(a.get("script") or "", [str(x) for x in args], a.get("timeout_min"), origin="mcp")
        return dict(job=_task(t), next="job mit wait_s aufrufen; Ergebnis enthält exit_code, output_tail, logfile.")


S_PROJECT = {"type": ["string", "integer"], "description": "Projekt: ID, Name oder Objekt (siehe list_projects)"}
RO, RW = mcp.RO, mcp.RW

TOOLS = [
    ("status", "Zustand: Version, Arbeitsverzeichnis, PixInsight gefunden?, laufende/wartende/letzte Aufträge.",
     {}, [], RO),
    ("list_projects", "Alle Projekte mit Belichtungsstunden je Filter, Lights, letztem Stack, neuen Frames seit dem "
     "letzten Stack, Quelle offline, arbeitet gerade.", {}, [], RO),
    ("project", "Überblick über ein Projekt: Quellordner, Zählung, Entscheidungen, Plan (Läufe je Filter mit Flats, "
     "Einstellungen, warum aus), Hinweise, Platzbedarf, letzte Stacks mit Qualität, laufende Aufträge.",
     {"name": S_PROJECT}, ["name"], RO),
    ("frames", "Frames mit Status (use/skip), Grund und Messwerten (FWHM px, Exzentrizität, Sterne, Hintergrund, "
     "Rauschen). Gefiltert und seitenweise.",
     {"project": S_PROJECT, "type": {"type": "string", "enum": ["light", "flat", "bias", "dark", "darkflat", "all"]},
      "status": {"type": "string", "enum": ["use", "skip"]},
      "sort": {"type": "string", "enum": ["fwhm", "ecc", "noise", "bg", "stars"], "description": "schlechteste zuerst"},
      "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}},
     ["project"], RO),
    ("stacks", "Alle Stacks eines Projekts, neueste zuerst: Lauf, Lights, Stunden, Größe, Notiz, Dateien und "
     "Qualität (FWHM, Rauschen, Sterne, Exzentrizität, Verlauf).", {"project": S_PROJECT}, ["project"], RO),
    ("compare_stacks", "Zwei Stacks vergleichen: Qualitätswerte nebeneinander, welcher jeweils besser ist, Versatz.",
     {"project": S_PROJECT, "a": {"type": "integer"}, "b": {"type": "integer"}}, ["project", "a", "b"], RO),
    ("history", "Verlauf in Klartext (neueste zuerst, zusammengefasst): neue Lights je Filter und Nacht, Stacks, "
     "Entscheidungen, Planänderungen. Ohne project: alle Projekte.",
     {"project": S_PROJECT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, [], RO),
    ("logs", "Wie history (Verlauf in Klartext).",
     {"project": S_PROJECT, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, [], RO),
    ("job", "Status eines Auftrags (Prüfen, Stacken, Skript): Schritt, Fortschritt, Restzeit, Fehler, Ergebnis. "
     "Ohne id: der laufende bzw. letzte. Mit wait_s bis zu 120 s auf das Ende warten.",
     {"id": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": MAX_WAIT}}, [], RO),
    ("create_project", "Neues Projekt mit Quellordnern anlegen; das Einlesen und Vermessen startet automatisch.",
     {"name": {"type": "string"}, "folders": {"type": "array", "items": {"type": "string"}},
      "description": {"type": "string"}}, ["name"], RW),
    ("add_folders", "Quellordner zu einem Projekt hinzufügen (neue Nächte, Flats); wird automatisch eingelesen.",
     {"project": S_PROJECT, "folders": {"type": "array", "items": {"type": "string"}}}, ["project", "folders"], RW),
    ("analyze", "Projekt jetzt auf neue oder geänderte Dateien prüfen (sonst automatisch). Unveränderte Frames werden "
     "nicht neu gelesen; Entscheidungen bleiben.", {"project": S_PROJECT}, ["project"], RW),
    ("set_frames", "Frames verwenden (use), weglassen (skip) oder der Automatik überlassen (auto). IDs, Pfade oder "
     "eindeutige Dateinamen aus frames. Plan wird sofort neu berechnet; umkehrbar.",
     {"project": S_PROJECT, "frames": {"type": "array", "items": {"type": ["string", "integer"]}},
      "decision": {"type": "string", "enum": ["use", "skip", "auto", "in", "out"]}, "reason": {"type": "string"}},
     ["project", "frames", "decision"], RW),
    ("change_plan", "Stacking-Plan ändern (ohne Angaben: nur anzeigen). options gelten für alle Läufe, runs je Lauf-ID: "
     "{enabled, rejection, weighting, local_normalization, drizzle, autocrop, large_scale_rejection, "
     "distortion_correction, cosmetic_correction, fast_integration}; null = zurück auf Automatik. reset: alles zurück.",
     {"project": S_PROJECT, "options": {"type": "object"}, "runs": {"type": "object"}, "reset": {"type": "boolean"}},
     ["project"], RW),
    ("stack", "Stacken mit PixInsight einreihen (alle eingeschalteten Läufe oder nur runs = Lauf-IDs). Fortschritt "
     "mit job.", {"project": S_PROJECT, "runs": {"type": "array", "items": {"type": "string"}}}, ["project"], RW),
    ("cancel_job", "Laufenden oder wartenden Auftrag abbrechen (PixInsight wird beendet).",
     {"id": {"type": "string"}}, ["id"], RW),
    ("stack_note", "Notiz zu einem Stack setzen (erscheint in der Liste und im Vergleich).",
     {"project": S_PROJECT, "stack_id": {"type": "integer"}, "text": {"type": "string"}},
     ["project", "stack_id", "text"], RW),
    ("run_pixinsight_script", "Eigenes PixInsight-Skript (.js/.scp) aus dem Arbeitsverzeichnis im Automatik-Modus "
     "ausführen – für Diagnose. Läuft als Auftrag in der Spur des Stackens (nie parallel), Ausgabe im Ergebnis. "
     ".js braucht „#engine v8“ als erste Zeile.",
     {"script": {"type": "string", "description": "Pfad, absolut oder relativ zum Arbeitsverzeichnis"},
      "args": {"type": "array", "items": {"type": "string"}, "description": "ohne Komma"},
      "timeout_min": {"type": "number", "minimum": 1, "maximum": 360}}, ["script"], RW),
]


def tool_list() -> list[dict]:
    out = []
    for name, desc, props, req, ann in TOOLS:
        schema = {"type": "object", "properties": props, "additionalProperties": False}
        if req:
            schema["required"] = req
        out.append({"name": name, "description": desc, "inputSchema": schema,
                    "annotations": dict(ann, title=name.replace("_", " "))})
    return out


class CoreServer(mcp.Server):
    """JSON-RPC wie ``mcp.Server``, aber mit den Werkzeugen auf ``Core``."""

    def __init__(self, tools: CoreTools):
        super().__init__(tools=tools, out=None, forward=None)
        self.known = {t[0] for t in TOOLS}

    def handle(self, msg: dict):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            want = params.get("protocolVersion")
            return self._ok(mid, {"protocolVersion": want if want in mcp.PROTOCOLS else mcp.PROTOCOLS[0],
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "b2stacker", "title": "b2stacker", "version": __version__},
                                  "instructions": INSTRUCTIONS})
        if method == "tools/list":
            return self._ok(mid, {"tools": tool_list()})
        return super().handle(msg)


def handler(get_core):
    """``mcp_http.Handler`` für ``/mcp`` des neuen Servers."""
    from .. import mcp_http
    logbook.event("mcp_core_ready")
    return mcp_http.Handler(CoreServer(CoreTools(get_core)))
