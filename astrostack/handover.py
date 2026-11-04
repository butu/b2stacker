"""Übergabe an PixInsight/WBPP ohne Linken und Kopieren (Konzept Abschnitt 10, Weg B).

1. ``build_job`` schreibt aus einem Plan-Lauf eine JSON-Auftragsdatei: echte Pfade, Nacht-Schlüssel für die
   Flat-Zuordnung, Referenzbild, alle WBPP-Einstellungen, Ausgabeordner in ``temp/``.
2. ``write_scripts`` legt das Startskript (``pjsr/b2launch.js``, WBPP per ``#include``) und das Event-Script
   in ``temp/`` ab. PixInsight bekommt auf der Kommandozeile nur noch Skript und Auftrag.
3. ``run`` startet PixInsight, liest den Fortschritt aus der Statusdatei des Event-Scripts und am Ende die
   Ergebnisdatei des Startskripts.

Status: Prototyp. Auf Benjamins Rechner noch nicht mit echtem PixInsight geprüft (siehe docs/NEUBAU.md).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from . import db as dbm
from .flow.plan import wbpp_values

SESSION_KEYWORD = "B2SESSION"
LAUNCHER = os.path.join(os.path.dirname(__file__), "pjsr", "b2launch.js")

EVENT_SCRIPT = r"""// b2stacker – Event-Script (von WBPP kompiliert, daher ohne #engine): Fortschritt als JSON in eine Datei.
var B2_STATUS = "__STATUS__";
function b2write( state ) { try { File.writeTextFile( B2_STATUS, JSON.stringify( state.b2 ) ); } catch ( x ) {} }
function onPipelineStart( env, state )
{
   state.b2 = { started: ( new Date ).toISOString(), total: env.operationsCount, index: -1, name: "", done: 0, failed: [] };
   b2write( state );
}
function onOperationStart( env, state )
{
   state.b2.index = env.operationIndex; state.b2.name = String( env.name ); state.b2.t = ( new Date ).toISOString();
   b2write( state );
}
function onOperationDone( env, state )
{
   state.b2.done = env.operationIndex + 1;
   if ( String( env.status ).toLowerCase().indexOf( "fail" ) >= 0 )
      state.b2.failed.push( { name: String( env.name ), message: String( env.statusMessage ?? "" ) } );
   b2write( state );
}
function onPipelineEnd( env, state )
{
   state.b2.finished = ( new Date ).toISOString();
   b2write( state );
}
"""


class HandoverError(RuntimeError):
    pass


def pi_path(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


def wbpp_main(pixinsight_exe: str) -> str:
    """BPP-main.js der PixInsight-Installation, zu der die exe gehört."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(pixinsight_exe)))
    return os.path.join(root, "src", "scripts", "BatchPreprocessing", "BPP-main.js")


def _paths(db: dbm.Database, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {int(r[0]): r[1] for r in db.query("SELECT id, path FROM files WHERE id IN (%s)" % q, list(ids))}


def build_job(db: dbm.Database, run: dict, out_dir: str, reference_path: str | None = None) -> dict:
    """Auftrag für einen Plan-Lauf. Lights und ihre Flats bekommen denselben Nacht-Schlüssel; Flats, die für
    mehrere Nächte gelten, nur einmal."""
    if not run.get("light_ids"):
        raise HandoverError("Der Lauf %s hat keine Lights." % run.get("name"))
    lights = _paths(db, run["light_ids"])
    nights = {int(r[0]): r[1] for r in db.query(
        "SELECT id, night FROM files WHERE id IN (%s)" % ",".join("?" * len(lights)), list(lights))}
    files, session_of_set, night_session = [], {}, {}
    for fl in run.get("flats") or []:
        key = tuple(sorted(fl["file_ids"]))
        if not key:
            continue
        if key not in session_of_set:                # Flat-Satz, der für mehrere Nächte gilt, nur einmal
            session_of_set[key] = "S%d" % (len(session_of_set) + 1)
            files += [dict(path=pi_path(p), type="flat", session=session_of_set[key])
                      for p in _paths(db, list(key)).values()]
        night_session[fl["night"]] = session_of_set[key]
    for fid, p in sorted(lights.items(), key=lambda kv: kv[1]):
        files.append(dict(path=pi_path(p), type="light", session=night_session.get(nights.get(fid))))
    for kind, typ in (("bias", "bias"), ("darks", "dark")):
        files += [dict(path=pi_path(p), type=typ) for p in _paths(db, (run.get(kind) or {}).get("file_ids") or []).values()]
    v = wbpp_values(run)
    indexed = {"rejection": [None, None, None, v.pop("rejection_4")]}
    params = dict(v, groupingKeywordsEnabled=True)
    if reference_path:
        params.update(bestFrameReferenceMethod=0, referenceImage=pi_path(reference_path))
    else:
        params["bestFrameReferenceMethod"] = 1
    os.makedirs(out_dir, exist_ok=True)
    job = dict(run=run.get("id"), name=run.get("name"), outputDirectory=pi_path(out_dir),
               result=pi_path(os.path.join(out_dir, "b2-result.json")),
               status=pi_path(os.path.join(out_dir, "b2-status.json")), sessionKeyword=SESSION_KEYWORD,
               params=params, indexed=indexed, files=files)
    if any(s["key"] == "drizzle" and s["value"] for s in run.get("settings") or []):
        job["drizzle"] = dict(enabled=True, scale=2, dropShrink=0.9)       # je Light-Gruppe (WBPP 3.2.0 geprüft)
    return job


def write_scripts(job: dict, temp_dir: str, pixinsight_exe: str) -> tuple[str, str]:
    """Startskript, Event-Script und Auftrag schreiben. → (Startskript, Auftrag)"""
    main = wbpp_main(pixinsight_exe)
    with open(LAUNCHER, encoding="utf-8") as fh:
        code = fh.read().replace("__WBPP_MAIN__", pi_path(main))
    launcher = os.path.join(temp_dir, "b2launch.js")
    events = os.path.join(temp_dir, "b2events.js")
    job_path = os.path.join(temp_dir, "b2job.json")
    with open(launcher, "w", encoding="utf-8") as fh:
        fh.write(code)
    with open(events, "w", encoding="utf-8") as fh:
        fh.write(EVENT_SCRIPT.replace("__STATUS__", job["status"]))
    job = dict(job, eventScript=pi_path(events))
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=1, ensure_ascii=False)
    return launcher, job_path


def command(exe: str, launcher: str, job_path: str) -> list[str]:
    for p in (launcher, job_path):
        if "," in pi_path(p):
            raise HandoverError("Der Temp-Ordner darf kein Komma enthalten: " + p)
    return [exe, "-n", "--automation-mode", '-r=%s,job=%s' % (pi_path(launcher), pi_path(job_path)), "--force-exit"]


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def run(exe: str, launcher: str, job_path: str, progress=None, cancel: threading.Event | None = None,
        popen=subprocess.Popen, poll_s: float = 1.0) -> dict:
    """PixInsight mit dem Auftrag starten und bis zum Ende begleiten. → Ergebnis des Startskripts"""
    job = _read_json(job_path) or {}
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000
    proc = popen(command(exe, launcher, job_path), **kw)
    last = None
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            _kill(proc)
            break
        st = _read_json(job.get("status", "")) if job.get("status") else None
        if st and progress and st != last:
            last = st
            progress(step="Stacken mit PixInsight", done=int(st.get("done") or 0), total=int(st.get("total") or 0),
                     message=st.get("name") or "")
        time.sleep(poll_s)
    proc.wait()
    res = _read_json(job.get("result", "")) or {}
    res["exit_code"] = proc.returncode
    res["cancelled"] = bool(cancel is not None and cancel.is_set())
    res["status"] = _read_json(job.get("status", "")) if job.get("status") else None
    if not res.get("ok") and not res.get("error") and not res["cancelled"]:
        res["error"] = ("PixInsight hat das Startskript nicht ausgeführt (keine Ergebnisdatei)."
                        if "phase" not in res else "Abbruch in Phase „%s“." % res["phase"])
    masters = os.path.join(os.path.dirname(job.get("result", "")), "master")
    res["masters"] = sorted(os.path.join(masters, f) for f in os.listdir(masters)) if os.path.isdir(masters) else []
    return res


def _kill(proc) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=0x08000000)
    else:
        proc.kill()
