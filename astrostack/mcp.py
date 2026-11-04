"""MCP-Server: b2stacker für KI-Assistenten (z. B. Claude) bedienbar machen.

Start: ``b2stacker.exe mcp`` bzw. ``python -m astrostack mcp`` – spricht MCP (JSON-RPC 2.0) über stdin/stdout.
Alle Aufträge laufen über die laufende b2stacker-App (``appclient``): eine Warteschlange, eine PixInsight-Instanz,
und im Programmfenster sieht man live, was die KI anstößt. Läuft die App nicht, wird sie gestartet.

Keine Zusatzpakete: das Protokoll ist schlank genug, und die exe bleibt klein.
Nichts hier löscht oder verschiebt Bilddateien; Frame-Entscheidungen und Plan-Änderungen sind jederzeit umkehrbar.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import __version__
from .appclient import AppError, Client

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_WAIT = 120                       # längstes Warten in einem Werkzeugaufruf (s)

INSTRUCTIONS = """b2stacker sichtet Astro-Rohdaten (FITS/XISF), bewertet jeden Frame und stackt mit PixInsight (WBPP).
Typischer Ablauf: status → list_projects → project (Überblick, Läufe, Hinweise) → frames (Problemfälle ansehen)
→ set_frames / change_plan (Auswahl und Plan anpassen) → stack → job (mit wait_s warten, Fortschritt melden).
Neue Daten: scan_folders (was liegt im Ordner?) → create_project mit den Ordnern (startet die Analyse).
Aufträge laufen nacheinander in der App; job zeigt Schritt, Restzeit und Protokoll. Bei Fehlern: job (error, causes)
und logs(kind="error"). Originaldateien werden nie verändert, verschoben oder gelöscht; Frame-Entscheidungen
(set_frames) und Plan-Änderungen sind umkehrbar. Frage den Menschen vor dem Stacken, wenn der Plan unklar ist."""


class ToolError(Exception):
    pass


# ------------------------------------------------------------------ Hilfen für kompakte Antworten

def _trim(x, depth=0, max_list=40, max_str=1500):
    """Große Antworten kürzen, damit die KI schnell bleibt (lange Listen, Protokolle, Base64)."""
    if depth > 6:
        return "…"
    if isinstance(x, dict):
        return {k: _trim(v, depth + 1, max_list, max_str) for k, v in x.items()
                if not (isinstance(v, str) and v.startswith("data:image"))}
    if isinstance(x, list):
        out = [_trim(v, depth + 1, max_list, max_str) for v in x[:max_list]]
        if len(x) > max_list:
            out.append("… %d weitere" % (len(x) - max_list))
        return out
    if isinstance(x, str) and len(x) > max_str:
        return x[:max_str] + "…"
    return x


def _job(j: dict | None, log_lines: int = 20) -> dict | None:
    if not j:
        return None
    p = j.get("progress") or {}
    keep = ("stage", "step", "done", "total", "eta_s", "step_elapsed_s", "message", "alert", "causes", "failed_step",
            "run", "run_index", "runs_total", "hints", "logdir")
    out = dict(id=j.get("id"), kind=j.get("kind"), title=j.get("title"), state=j.get("state"),
               project=j.get("project"), created=j.get("created"), finished=j.get("finished") or None,
               error=j.get("error") or None,
               progress={k: p[k] for k in keep if p.get(k) not in (None, "", [], {})} or None)
    if log_lines:
        out["log"] = (j.get("log") or [])[-log_lines:]
    if j.get("result") is not None and j.get("state") not in ("queued", "running"):
        out["result"] = _trim(j["result"], max_list=15, max_str=600)
    return out


def _run(r: dict) -> dict:
    name = r.get("name")
    disp = r.get("label") or (r.get("filter") + name[len((r.get("filter") or "").replace("/", "_")):]
                              if r.get("filter") and name and name.startswith((r.get("filter") or "").replace("/", "_"))
                              else name)
    return dict(name=name, display=disp, enabled=r.get("enabled", True), filter=r.get("filter"), mode=r.get("mode"),
                outputs=r.get("outputs"), lights=r.get("lightCount"), hours=r.get("hours"), nights=r.get("nights"),
                flats=len(r.get("flats") or []), flat_set=r.get("flat_set"), flat_ok=r.get("flat_ok"),
                bias=len(r.get("bias") or []), darks=len(r.get("darks") or []), reference=r.get("reference"),
                top_percent=r.get("top_percent"), why=r.get("why"))


def _frame(f: dict) -> dict:
    m = f.get("metrics") or {}
    flags = [x.get("text") if isinstance(x, dict) else x for x in f.get("flags") or []]
    out = dict(file=os.path.basename(f.get("path") or ""), path=f.get("path"), type=f.get("type"),
               night=f.get("night"), filter=f.get("filter"), status=f.get("status"), auto=f.get("auto_status"),
               used=bool(f.get("selected")), decided_by=f.get("selection_source"),
               reasons=[x for x in flags if x] or None, error=f.get("measure_error"),
               fwhm_px=m.get("fwhm"), ecc=m.get("ecc"), stars=m.get("nstars"), bg=m.get("bg"), noise=m.get("noise"),
               exposure=f.get("exposure"))
    return {k: v for k, v in out.items() if v not in (None, [], "")}


# ------------------------------------------------------------------ Werkzeuge

class Tools:
    def __init__(self, client: Client | None = None):
        self.c = client or Client()

    def _pname(self, name) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ToolError("Projektname fehlt (siehe list_projects).")
        return self.c.project_name(name)

    # -------- lesen
    def status(self, a):
        s = self.c.get("api/state")
        st = s.get("settings") or {}
        return dict(app=self.c.url, version=s.get("version"), started_now=self.c.started, workdir=st.get("workdir"),
                    pixinsight=st.get("pixinsight"), pixinsight_ok=s.get("pixinsight_ok"), wbpp_ok=s.get("wbpp_ok"),
                    pixinsight_running=len(s.get("pixinsight_running") or []), disk_free_gb=s.get("disk_free_gb"),
                    ram_free_gb=s.get("ram_free_gb"), ram_total_gb=s.get("ram_total_gb"),
                    settings_problems=s.get("settings_problems") or None,
                    job=_job(s.get("job"), 5), queue=_trim(s.get("jobs") or [], max_list=10),
                    projects=len(s.get("projects") or []))

    def list_projects(self, a):
        keep = ("name", "title", "target", "description", "hours", "filters", "frames", "stacks", "last_night",
                "last_stack", "created")
        return [{k: p.get(k) for k in keep if p.get(k) not in (None, "", [])} for p in self.c.get("api/state")["projects"]]

    def project(self, a):
        name = self._pname(a.get("name"))
        d = self.c.get("api/project/" + self.c.q(name))
        plan = d.get("plan") or {}
        ins = {}
        try:
            ins = self.c.get("api/project/%s/insights" % self.c.q(name))
        except AppError:
            pass
        hist = []
        for r in (d.get("stacks") or [])[-8:]:
            hist.append(dict(id=r.get("id"), date=r.get("date"), run=r.get("run"), name=r.get("name"),
                             outputs=[dict(name=o.get("name"), file=o.get("file"), exists=o.get("exists"),
                                           kpi=o.get("kpi")) for o in r.get("outputs") or []]))
        out = dict(name=name, title=(d.get("meta") or {}).get("name"), description=(d.get("meta") or {}).get("description"),
                   sources=(d.get("sources") or {}).get("roots"), analysis_required=d.get("analysis_required"),
                   stack_ready=not d.get("stack_blockers"), stack_blockers=d.get("stack_blockers") or None,
                   facts=d.get("facts"), runs=[_run(r) for r in plan.get("runs") or []], options=plan.get("options"),
                   need_gb=plan.get("need_gb"), free_gb=plan.get("free_gb"), need_ram_gb=plan.get("need_ram_gb"),
                   manual_decisions=len(d.get("overrides") or {}), stacks=hist or None)
        if ins.get("available"):
            out["frames"] = ins.get("counts")
            out["reject_reasons"] = dict(ins.get("reasons") or [])
            out["rig_checks"] = [dict(level=c.get("level"), area=c.get("area"), title=c.get("title"), text=c.get("text"))
                                 for c in ins.get("diag") or [] if c.get("level") != "good"]
            out["calibration_checks"] = [dict(level=c.get("level"), title=c.get("title"), text=c.get("text"))
                                         for c in ins.get("calib") or [] if c.get("level") not in ("good", "ok")]
            out["recommendations"] = _trim(ins.get("rec") or [], max_list=15)
        return out

    def frames(self, a):
        name = self._pname(a.get("project"))
        d = self.c.get("api/project/%s/frames" % self.c.q(name))
        rows = d.get("frames") or []
        typ = a.get("type", "light")
        if typ and typ != "all":
            rows = [f for f in rows if f.get("type") == typ]
        for key in ("filter", "night"):
            if a.get(key):
                rows = [f for f in rows if str(f.get(key) or "").lower() == str(a[key]).lower()]
        st = a.get("status")
        if st == "used":
            rows = [f for f in rows if f.get("selected")]
        elif st == "unused":
            rows = [f for f in rows if not f.get("selected")]
        elif st == "problems":
            rows = [f for f in rows if f.get("status") not in ("ok",) or not f.get("selected")]
        elif st:
            rows = [f for f in rows if f.get("status") == st]
        sort = a.get("sort")
        if sort in ("fwhm", "ecc", "noise", "bg"):
            rows.sort(key=lambda f: ((f.get("metrics") or {}).get(sort) is None, -((f.get("metrics") or {}).get(sort) or 0)))
        elif sort == "stars":
            rows.sort(key=lambda f: ((f.get("metrics") or {}).get("nstars") or 0))
        off, lim = int(a.get("offset") or 0), min(int(a.get("limit") or 100), 500)
        counts = {}
        for f in rows:
            counts[f.get("status")] = counts.get(f.get("status"), 0) + 1
        return dict(project=name, matching=len(rows), counts=counts, summary=d.get("summary"),
                    plan_stale=d.get("plan_stale") or None, analysis_required=d.get("analysis_required") or None,
                    frames=[_frame(f) for f in rows[off:off + lim]],
                    more=max(0, len(rows) - off - lim) or None)

    def job(self, a):
        jid = a.get("id")
        if not jid:
            s = self.c.get("api/state")
            j = s.get("job")
            if not j:
                return dict(job=None, note="Noch kein Auftrag in dieser Sitzung der App.")
            jid = j["id"]
        wait = max(0.0, min(float(a.get("wait_s") or 0), MAX_WAIT))
        j = self.c.wait_job(jid, wait, log_tail=int(a.get("log_lines") or 20)) if wait else \
            self.c.get("api/job/" + self.c.q(jid), tail=int(a.get("log_lines") or 20))
        out = _job(j, int(a.get("log_lines") or 20))
        if out["state"] in ("queued", "running"):
            out["hint"] = "Läuft noch – job erneut mit wait_s aufrufen."
        return out

    def logs(self, a):
        from . import logbook
        workdir = (self.c.get("api/state").get("settings") or {}).get("workdir")
        ev = logbook.read_events(workdir, min(int(a.get("last") or 30), 200), a.get("kind") or None,
                                 a.get("project") or None)
        if not a.get("trace"):
            ev = [{k: v for k, v in e.items() if k != "trace"} for e in ev]
        return _trim(ev, max_list=200, max_str=3000 if a.get("trace") else 600)

    def settings(self, a):
        ch = a.get("changes")
        if ch:
            if not isinstance(ch, dict):
                raise ToolError("changes muss ein Objekt sein, z. B. {\"analysis_workers\": 4}")
            s = self.c.post("api/settings", ch)
        else:
            s = self.c.get("api/state")
        return dict(settings=s.get("settings"), settings_file=s.get("settings_file"),
                    problems=s.get("settings_problems") or None, pixinsight_ok=s.get("pixinsight_ok"))

    # -------- ändern / auslösen
    def scan_folders(self, a):
        folders = a.get("folders")
        j = self.c.post("api/scan-preview", {"folders": folders})
        j = self.c.wait_job(j["id"], MAX_WAIT, log_tail=10)
        if j.get("state") != "done":
            raise ToolError("Durchsuchen nicht fertig: %s %s" % (j.get("state"), j.get("error") or ""))
        return _trim(j.get("result"), max_list=60)

    def create_project(self, a):
        title = (a.get("title") or "").strip()
        folders = a.get("folders") or []
        d = self.c.post("api/project-create", {"title": title, "roots": folders})
        name = d.get("name")
        out = dict(project=name, title=(d.get("meta") or {}).get("name"), sources=(d.get("sources") or {}).get("roots"))
        if folders and a.get("analyze", True):
            out["job"] = _job(self.c.post("api/project/%s/reanalyze" % self.c.q(name)), 3)
            out["next"] = "job mit wait_s aufrufen, danach project/frames."
        return out

    def add_folders(self, a):
        name = self._pname(a.get("project"))
        d = self.c.get("api/project/" + self.c.q(name))
        roots = list((d.get("sources") or {}).get("roots") or [])
        new = [f for f in a.get("folders") or [] if f not in roots]
        d = self.c.post("api/project/%s/sources" % self.c.q(name), {"roots": roots + new})
        out = dict(project=name, sources=(d.get("sources") or {}).get("roots"), added=new)
        if a.get("analyze", True):
            out["job"] = _job(self.c.post("api/project/%s/reanalyze" % self.c.q(name)), 3)
        return out

    def analyze(self, a):
        name = self._pname(a.get("project"))
        return dict(project=name, job=_job(self.c.post("api/project/%s/reanalyze" % self.c.q(name)), 3),
                    next="job mit wait_s aufrufen.")

    def set_frames(self, a):
        name = self._pname(a.get("project"))
        decision = a.get("decision")
        if decision not in ("in", "out", "auto"):
            raise ToolError("decision muss in, out oder auto sein")
        want = a.get("frames") or []
        if isinstance(want, str):
            want = [want]
        if not want:
            raise ToolError("frames: mindestens ein Pfad oder Dateiname")
        rows = self.c.get("api/project/%s/frames" % self.c.q(name)).get("frames") or []
        by_path = {f["path"]: f for f in rows}
        by_file: dict[str, list] = {}
        for f in rows:
            by_file.setdefault(os.path.basename(f["path"]).lower(), []).append(f["path"])
        paths, unknown, ambiguous = [], [], []
        for w in want:
            if w in by_path:
                paths.append(w)
                continue
            hit = by_file.get(os.path.basename(str(w)).lower(), [])
            if len(hit) == 1:
                paths.append(hit[0])
            elif hit:
                ambiguous.append(w)
            else:
                unknown.append(w)
        if unknown or ambiguous:
            raise ToolError("Nicht gefunden: %s%s – vollständige Pfade aus frames verwenden. Nichts geändert." % (
                ", ".join(unknown) or "–", (" · mehrdeutig: " + ", ".join(ambiguous)) if ambiguous else ""))
        res = self.c.post("api/project/%s/overrides" % self.c.q(name), {"decisions": {p: decision for p in paths}})
        return dict(project=name, changed=len(paths), decision=decision, selection=_trim(res, max_list=10))

    def change_plan(self, a):
        name = self._pname(a.get("project"))
        body = {k: a[k] for k in ("options", "runs") if a.get(k)}
        if body:
            plan = self.c.post("api/project/%s/plan" % self.c.q(name), body)
        else:
            plan = (self.c.get("api/project/" + self.c.q(name)).get("plan") or {})
        return dict(project=name, changed=bool(body), runs=[_run(r) for r in plan.get("runs") or []],
                    options=plan.get("options"), total_frames=plan.get("total_frames"), need_gb=plan.get("need_gb"))

    def stack(self, a):
        name = self._pname(a.get("project"))
        d = self.c.get("api/project/" + self.c.q(name))
        if d.get("stack_blockers"):
            raise ToolError("Stacken nicht möglich: " + " ".join(d["stack_blockers"]))
        runs = a.get("runs")
        j = self.c.post("api/project/%s/stack" % self.c.q(name), {"only": runs} if runs else {})
        plan = d.get("plan") or {}
        chosen = [_run(r) for r in plan.get("runs") or [] if (r["name"] in runs if runs else r.get("enabled", True))]
        return dict(project=name, job=_job(j, 3),
                    stacking=[{k: r[k] for k in ("name", "display", "lights", "hours", "outputs")} for r in chosen],
                    need_gb=plan.get("need_gb"), free_gb=plan.get("free_gb"),
                    next="job mit wait_s aufrufen; Stacking dauert je nach Datenmenge Minuten bis Stunden.")

    def cancel_job(self, a):
        jid = a.get("id")
        if not jid:
            raise ToolError("id fehlt (siehe status/job)")
        r = self.c.post("api/job/%s/cancel" % self.c.q(jid))
        return dict(ok=bool(r.get("ok")), id=jid)

    def selftest(self, a):
        folder = a.get("folder") or (self.c.get("api/state").get("settings") or {}).get("workdir")
        return dict(job=_job(self.c.post("api/selftest", {"folder": folder}), 3), folder=folder,
                    next="job mit wait_s aufrufen (einige Minuten).")

    def run_pixinsight_script(self, a):
        args = a.get("args") or []
        if not isinstance(args, list):
            raise ToolError("args muss eine Liste sein")
        j = self.c.post("api/pi-script", {"script": a.get("script", ""), "args": args,
                                           "timeout_min": a.get("timeout_min")})
        return dict(job=_job(j, 3), next="job mit wait_s aufrufen; Ergebnis enthält exit_code, output_tail, logfile.")

    def open_in_pixinsight(self, a):
        name = self._pname(a.get("project"))
        d = self.c.get("api/project/" + self.c.q(name))
        hist = d.get("stacks") or []
        rec = next((r for r in hist if r.get("id") == a.get("stack_id")), None) if a.get("stack_id") else \
            (hist[-1] if hist else None)
        if not rec:
            raise ToolError("Kein Stack gefunden" + (" mit dieser id" if a.get("stack_id") else " – erst stacken."))
        files = [o["file"] for o in rec.get("outputs") or [] if o.get("file") and o.get("exists", True)]
        if not files:
            raise ToolError("Die Dateien dieses Stacks fehlen.")
        self.c.post("api/open-pixinsight", {"paths": files})
        return dict(opened=files)


S_PROJECT = {"type": "string", "description": "Projekt: Name, Titel oder Objekt (siehe list_projects)"}
RO = {"readOnlyHint": True, "openWorldHint": False}
RW = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

TOOLS = [
    ("status", "Zustand von b2stacker: Version, Arbeitsverzeichnis, PixInsight gefunden?, freier Platz/RAM, "
     "laufender Auftrag und Warteschlange. Startet b2stacker, falls es nicht läuft.", {}, [], RO),
    ("list_projects", "Alle Projekte mit Belichtungsstunden, Filtern, Nächten und Anzahl Stacks.", {}, [], RO),
    ("project", "Überblick über ein Projekt: Quellordner, Läufe je Filter (Lights, Stunden, Flats/Bias, Begründung), "
     "Optionen, Platzbedarf, ob Stacken möglich ist (stack_blockers), Frame-Zählung mit Aussortiergründen, "
     "Rig-Diagnose (Fokus, Nachführung, Dithering …), Kalibrier-Hinweise und Stack-Historie mit Kennzahlen.",
     {"name": S_PROJECT}, ["name"], RO),
    ("frames", "Frames eines Projekts mit Bewertung (status ok/warn/suggest/rejected/error), ob verwendet, Gründen "
     "und Messwerten (FWHM in px, Exzentrizität, Sterne, Hintergrund, Rauschen). Gefiltert und seitenweise.",
     {"project": S_PROJECT,
      "type": {"type": "string", "enum": ["light", "flat", "bias", "dark", "all"], "description": "Standard: light"},
      "status": {"type": "string", "enum": ["ok", "warn", "suggest", "rejected", "error", "used", "unused", "problems"],
                 "description": "problems = alles, was nicht ok oder nicht verwendet ist"},
      "filter": {"type": "string"}, "night": {"type": "string", "description": "JJJJ-MM-TT"},
      "sort": {"type": "string", "enum": ["fwhm", "ecc", "noise", "bg", "stars"], "description": "schlechteste zuerst"},
      "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "offset": {"type": "integer", "minimum": 0}},
     ["project"], RO),
    ("job", "Status eines Auftrags (Analyse, Stacking, Selbsttest …): Schritt, Fortschritt, Restzeit, Fehlerursache, "
     "Protokoll-Ende, Ergebnis. Ohne id: der aktuelle bzw. letzte Auftrag. Mit wait_s bis zu 120 s auf das Ende warten.",
     {"id": {"type": "string"}, "wait_s": {"type": "number", "minimum": 0, "maximum": MAX_WAIT},
      "log_lines": {"type": "integer", "minimum": 0, "maximum": 300}}, [], RO),
    ("logs", "Protokoll-Ereignisse (Analysen, Stacking-Schritte, PixInsight-Exit-Codes, Fehler).",
     {"kind": {"type": "string", "description": "z. B. error, job_end, stack_step, analysis, mcp_call"},
      "project": {"type": "string"}, "last": {"type": "integer", "minimum": 1, "maximum": 200},
      "trace": {"type": "boolean", "description": "Tracebacks mitliefern"}}, [], RO),
    ("settings", "Einstellungen anzeigen oder ändern (z. B. workdir, pixinsight, analysis_workers, site_lat/site_lon).",
     {"changes": {"type": "object", "description": "nur die zu ändernden Werte"}}, [], RW),
    ("scan_folders", "Ordner durchsuchen (nur Header lesen): welche Objekte, Filter, Nächte, Kalibrierdaten liegen dort? "
     "Ändert nichts.", {"folders": {"type": "array", "items": {"type": "string"}}}, ["folders"], RO),
    ("create_project", "Neues Projekt anlegen, optional mit Quellordnern; startet dann gleich die Analyse.",
     {"title": {"type": "string"}, "folders": {"type": "array", "items": {"type": "string"}},
      "analyze": {"type": "boolean", "description": "Standard: true"}}, ["title"], RW),
    ("add_folders", "Quellordner zu einem Projekt hinzufügen (neue Nächte, Flats) und neu analysieren.",
     {"project": S_PROJECT, "folders": {"type": "array", "items": {"type": "string"}},
      "analyze": {"type": "boolean", "description": "Standard: true"}}, ["project", "folders"], RW),
    ("analyze", "Projekt neu analysieren (neue Dateien finden, vermessen, Plan neu berechnen). Unveränderte Frames "
     "kommen aus dem Cache; manuelle Entscheidungen bleiben.", {"project": S_PROJECT}, ["project"], RW),
    ("set_frames", "Frames manuell verwenden (in), weglassen (out) oder der Automatik überlassen (auto). Pfade oder "
     "eindeutige Dateinamen aus frames. Plan wird sofort neu berechnet; umkehrbar.",
     {"project": S_PROJECT, "frames": {"type": "array", "items": {"type": "string"}},
      "decision": {"type": "string", "enum": ["in", "out", "auto"]}}, ["project", "frames", "decision"], RW),
    ("change_plan", "Stacking-Plan ändern (ohne Angaben: nur anzeigen). options: fix_fringes, local_normalization, "
     "drizzle, autocrop, large_scale_rejection, distortion_correction, hot_pixels, cleanup (true/false), weighting "
     "(psf_signal|psf_snr|snr), top_percent (1–100). runs: {Laufname: {enabled, reference (auto|master:<Lauf>), "
     "top_percent, label, calib: {flats|bias|darks|darkflats: auto|none|<id>}, wbpp: {Parameter: Wert}}}.",
     {"project": S_PROJECT, "options": {"type": "object"}, "runs": {"type": "object"}}, ["project"], RW),
    ("stack", "Stacking mit PixInsight einreihen (alle eingeschalteten Läufe oder nur runs). Läuft in der App im "
     "Hintergrund; Fortschritt mit job.", {"project": S_PROJECT,
                                             "runs": {"type": "array", "items": {"type": "string"},
                                                      "description": "nur diese Läufe (einmalig)"}}, ["project"], RW),
    ("cancel_job", "Laufenden oder wartenden Auftrag abbrechen (PixInsight wird sauber beendet).",
     {"id": {"type": "string"}}, ["id"], RW),
    ("selftest", "Selbsttest mit echtem PixInsight: kleine Testdaten anlegen, analysieren, stacken, Bericht. "
     "Ordner Standard: Arbeitsverzeichnis.", {"folder": {"type": "string"}}, [], RW),
    ("run_pixinsight_script", "Eigenes PixInsight-Skript (.js/.scp) aus dem Arbeitsverzeichnis im Automatik-Modus "
     "ausführen – für Diagnose und Entwicklung. Läuft als Auftrag in der Warteschlange (nie parallel zum Stacken), "
     "Ausgabe von PixInsight im Protokoll. .js braucht „#engine v8“ als erste Zeile.",
     {"script": {"type": "string", "description": "Pfad, absolut oder relativ zum Arbeitsverzeichnis"},
      "args": {"type": "array", "items": {"type": "string"},
               "description": "Argumente (landen in Runtime.jsArguments); ohne Komma"},
      "timeout_min": {"type": "number", "minimum": 1, "maximum": 360, "description": "Standard 30"}},
     ["script"], RW),
    ("open_in_pixinsight", "Ergebnis eines Stacks in PixInsight öffnen (Standard: neuester Stack).",
     {"project": S_PROJECT, "stack_id": {"type": "string"}}, ["project"], RW),
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


# ------------------------------------------------------------------ Protokoll

def app_forwarder(client: Client, timeout: float = MAX_WAIT + 30):
    """Weiterleitung an ``/mcp`` der laufenden App (gleicher Schlüssel aus den Einstellungen). → Funktion oder
    None. Gibt ``None`` zurück, wenn die App den Endpunkt nicht kennt (404) oder der Schlüssel nicht passt."""
    import urllib.error
    import urllib.request

    def forward(msg: dict):
        from .mcp_http import ensure_token
        from .settings import Settings
        tok = ensure_token(Settings.load())
        req = urllib.request.Request(client.base().rstrip("/") + "/mcp", data=json.dumps(msg).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": "Bearer " + tok})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 405):
                return None
            raise
        return json.loads(raw) if raw else None
    return forward


class Server:
    def __init__(self, tools: Tools | None = None, out=None, forward=None):
        self.tools = tools or Tools()
        self.forward = forward
        self.out = out
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp")
        self.known = {t[0] for t in TOOLS}

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def call_tool(self, name: str, args: dict) -> dict:
        from . import logbook
        t0 = time.time()
        try:
            res = getattr(self.tools, name)(args or {})
            text = json.dumps(res, ensure_ascii=False, default=str, separators=(",", ":"))
            ok, err = True, None
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except (ToolError, AppError, ValueError) as e:
            ok, err = False, str(e)
            result = {"content": [{"type": "text", "text": "Fehler: %s" % e}], "isError": True}
        except Exception as e:  # noqa: BLE001 – nie den Server sterben lassen
            ok, err = False, "%s: %s" % (type(e).__name__, e)
            try:
                logbook.error("MCP-Werkzeug %s" % name)
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
            result = {"content": [{"type": "text", "text": "Interner Fehler: %s (Details: logs kind=error)" % err}],
                      "isError": True}
        try:
            logbook.event("mcp_call", tool=name, ok=ok, ms=int((time.time() - t0) * 1000), error=err,
                          project=(args or {}).get("project") or (args or {}).get("name"))
        except Exception:  # noqa: BLE001
            pass
        return result

    def _forward(self, msg: dict):
        """Werkzeugliste und Werkzeugaufrufe an den MCP-Endpunkt der laufenden App weiterreichen. → Antwort oder
        None (App ohne Endpunkt, z. B. ältere Version: dann gilt die eingebaute Liste)."""
        fwd = getattr(self, "forward", None)
        if fwd is None:
            return None
        try:
            return fwd(msg)
        except Exception:  # noqa: BLE001 – Weiterleitung ist ein Zusatz; ohne sie geht es lokal weiter
            return None

    def handle(self, msg: dict) -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method in ("tools/list", "tools/call") and mid is not None and getattr(self, "forward", None):
            if method == "tools/list":
                r = self._forward(msg)
                if r is not None:
                    return r
            else:
                self.pool.submit(self._answer_forwarded, msg)
                return None
        if method is None:                    # Antwort des Clients – wir stellen keine Anfragen
            return None
        if method == "initialize":
            want = params.get("protocolVersion")
            return self._ok(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                                  "capabilities": {"tools": {"listChanged": False}},
                                  "serverInfo": {"name": "b2stacker", "title": "b2stacker", "version": __version__},
                                  "instructions": INSTRUCTIONS})
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": tool_list()})
        if method == "resources/list":
            return self._ok(mid, {"resources": []})
        if method == "prompts/list":
            return self._ok(mid, {"prompts": []})
        if method == "tools/call":
            name = params.get("name")
            if name not in self.known:
                return self._err(mid, -32602, "Unbekanntes Werkzeug: %s" % name)
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return self._err(mid, -32602, "arguments muss ein Objekt sein")
            self.pool.submit(self._answer, mid, name, args)
            return None
        return self._err(mid, -32601, "Methode nicht unterstützt: %s" % method) if mid is not None else None

    def _answer(self, mid, name, args) -> None:
        """Jede Anfrage bekommt garantiert eine Antwort – sonst wartet der Client ewig."""
        try:
            res = self.call_tool(name, args)
        except BaseException as e:  # noqa: BLE001
            res = {"content": [{"type": "text", "text": "Interner Fehler: %s: %s" % (type(e).__name__, e)}],
                   "isError": True}
        try:
            self.send(self._ok(mid, res))
        except Exception:  # noqa: BLE001 – stdout geschlossen: Client ist weg
            traceback.print_exc(file=sys.stderr)

    def _answer_forwarded(self, msg: dict) -> None:
        r = self._forward(msg)
        if r is None:                         # App ohne Endpunkt → lokal beantworten
            p = msg.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            if name not in self.known:
                r = self._err(msg["id"], -32602, "Unbekanntes Werkzeug: %s" % name)
            else:
                return self._answer(msg["id"], name, args if isinstance(args, dict) else {})
        try:
            self.send(r)
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def serve(self, inp) -> None:
        for raw in iter(inp.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                self.send(self._err(None, -32700, "Ungültiges JSON"))
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                if not isinstance(m, dict):
                    self.send(self._err(None, -32600, "Ungültige Anfrage"))
                    continue
                try:
                    r = self.handle(m)
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc(file=sys.stderr)
                    r = self._err(m.get("id"), -32603, str(e))
                if r is not None:
                    self.send(r)
        self.pool.shutdown(wait=True)        # stdin zu: laufende Antworten noch schicken, dann Ende


def main() -> None:
    """stdin/stdout gehören dem Protokoll; alles andere geht nach stderr bzw. ins Protokoll."""
    from . import logbook
    from .settings import Settings
    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    sys.stdout = sys.stderr                   # versehentliche print()-Ausgaben dürfen das Protokoll nicht stören
    try:
        logbook.setup(Settings.load().workdir)
        logbook.event("mcp_start", version=__version__, pid=os.getpid())
    except Exception:  # noqa: BLE001
        pass
    tools = Tools()
    Server(tools=tools, out=out, forward=app_forwarder(tools.c)).serve(inp)
