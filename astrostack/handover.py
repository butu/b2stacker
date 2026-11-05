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
   state.b2 = { started: ( new Date ).toISOString(), total: env.operationsCount, index: -1, name: "", done: 0, failed: [],
                ops: [] };
   b2write( state );
}
function onOperationStart( env, state )
{
   state.b2.index = env.operationIndex; state.b2.name = String( env.name ); state.b2.t = ( new Date ).toISOString();
   if ( state.b2.ops.length < 300 )
      state.b2.ops.push( { n: String( env.name ), s: state.b2.t } );
   b2write( state );
}
function onOperationDone( env, state )
{
   state.b2.done = env.operationIndex + 1;
   var st = String( env.status ).toLowerCase();
   if ( st.indexOf( "fail" ) >= 0 )
      state.b2.failed.push( { name: String( env.name ), message: String( env.statusMessage ?? "" ) } );
   if ( state.b2.ops.length > 0 )
   {
      var o = state.b2.ops[state.b2.ops.length - 1];
      o.e = ( new Date ).toISOString(); o.st = st.indexOf( "fail" ) >= 0 ? "failed" : "done";
   }
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


def master_key(db: dbm.Database, kind: str, file_ids, depends_on: str = "") -> str | None:
    """Schlüssel eines Masters: Art + Prüfsummen der Rohframes (+ Schlüssel des Masters, mit dem kalibriert wird).
    Gleiche Rohframes bedeuten gleiche Kamera-Einstellungen; kommt ein Frame dazu, ändert sich der Schlüssel.
    ``None``, solange eine Prüfsumme fehlt (noch nicht vermessen)."""
    ids = sorted(set(file_ids or []))
    if not ids:
        return None
    rows = db.query("SELECT checksum FROM files WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
    sums = sorted(r[0] or "" for r in rows)
    if len(sums) != len(ids) or not all(sums):
        return None
    import hashlib
    h = hashlib.sha256(("%s|%s|%s" % (kind, depends_on, ",".join(sums))).encode()).hexdigest()
    return h[:24]


def master_info(db: dbm.Database, file_ids) -> dict:
    """Lesbare Eckdaten eines Masters für die Kalibrier-Bibliothek (aus den Headern der Rohframes)."""
    ids = sorted(set(file_ids or []))
    if not ids:
        return {}
    rows = db.query("SELECT f.filter, f.exposure, f.gain, f.offset, f.binning, f.ccd_temp, f.night, f.width, f.height, "
                    "s.camera, s.telescope FROM files f LEFT JOIN setups s ON s.id = f.setup_id WHERE f.id IN (%s)"
                    % ",".join("?" * len(ids)), ids)
    if not rows:
        return {}
    r = rows[0]
    temps = [x["ccd_temp"] for x in rows if x["ccd_temp"] is not None]
    nights = sorted({x["night"] for x in rows if x["night"]})
    return dict(frames=len(rows), filter=r["filter"], exposure=r["exposure"], gain=r["gain"], offset=r["offset"],
                binning=r["binning"], temp=round(sum(temps) / len(temps), 1) if temps else None,
                nights=nights[:20], camera=r["camera"], telescope=r["telescope"],
                size="%sx%s" % (r["width"], r["height"]) if r["width"] else None)


def _master(db: dbm.Database, pid: int | None, key: str | None) -> str | None:
    """Gespeicherter Master zu diesem Schlüssel – zuerst aus dem eigenen Projekt, sonst aus der Kalibrier-Bibliothek
    (jedes andere Projekt: gleicher Schlüssel = genau dieselben Rohframes, also derselbe Master)."""
    if pid is None or key is None:
        return None
    for (path,) in db.query("SELECT path FROM masters WHERE key = ? ORDER BY project_id != ?, id DESC", (key, pid)):
        if path and os.path.isfile(path):
            return path
    return None


def masters_plan(db: dbm.Database, pid: int, run: dict) -> list[dict]:
    """Für den Plan: welche Kalibrier-Master beim Stacken wiederverwendet werden – und warum nicht.
    Dieselbe Rechnung wie in ``build_job`` (gleicher Schlüssel = genau dieselben Rohframes)."""
    out = []

    def source(path):
        r = db.one("SELECT m.created_at, p.name FROM masters m JOIN projects p ON p.id = m.project_id WHERE m.path = ? "
                   "ORDER BY m.id DESC LIMIT 1", (path,))
        return ("aus %s, %s" % (r["name"], (r["created_at"] or "")[:10])) if r else ""

    def entry(kind, label, ids, key):
        if not ids:
            return None
        m = _master(db, pid, key)
        if m:
            return dict(kind=kind, label=label, reused=True, path=m, frames=len(ids),
                        why="wird wiederverwendet (%s) – spart das Neuberechnen" % source(m))
        if key is None:
            why = "wird neu berechnet – Prüfsummen der Rohframes fehlen noch (Vermessen läuft)"
        elif db.value("SELECT COUNT(*) FROM masters WHERE type = ?", (kind,)):
            why = ("wird neu berechnet – die gespeicherten %s stammen aus anderen Rohframes (andere Auswahl, "
                   "neue oder fehlende Frames)" % {"bias": "Master-Bias", "dark": "Master-Darks",
                                                    "flat": "Master-Flats"}[kind])
        else:
            why = "wird neu berechnet und danach in der Kalibrier-Bibliothek aufgehoben"
        return dict(kind=kind, label=label, reused=False, frames=len(ids), why=why)
    bias_ids = (run.get("bias") or {}).get("file_ids") or []
    bias_key = master_key(db, "bias", bias_ids)
    for e in (entry("bias", "Bias", bias_ids, bias_key),
              entry("dark", "Darks", (run.get("darks") or {}).get("file_ids") or [],
                    master_key(db, "dark", (run.get("darks") or {}).get("file_ids") or []))):
        if e:
            out.append(e)
    seen = {}
    for fl in run.get("flats") or []:
        ids = tuple(sorted(fl.get("file_ids") or []))
        if not ids:
            continue
        if ids in seen:
            seen[ids]["label"] += ", " + (fl.get("night") or "?")[5:].replace("-", ".")
            continue
        e = entry("flat", "Flats für %s" % (fl.get("night") or "?")[5:].replace("-", "."), list(ids),
                  master_key(db, "flat", ids, bias_key or ""))
        if e:
            seen[ids] = e
            out.append(e)
    return out


def build_job(db: dbm.Database, run: dict, out_dir: str, reference_path: str | None = None,
              project_id: int | None = None) -> dict:
    """Auftrag für einen Plan-Lauf. Lights und ihre Flats bekommen denselben Nacht-Schlüssel; Flats, die für
    mehrere Nächte gelten, nur einmal. Mit ``project_id`` werden passende gespeicherte Master statt der
    Rohframes übergeben (``masterKeys`` im Auftrag sagt, welcher Master zu welchem Schlüssel gehört)."""
    if not run.get("light_ids"):
        raise HandoverError("Der Lauf %s hat keine Lights." % run.get("name"))
    lights = _paths(db, run["light_ids"])
    nights = {int(r[0]): r[1] for r in db.query(
        "SELECT id, night FROM files WHERE id IN (%s)" % ",".join("?" * len(lights)), list(lights))}
    keys, reused, info = {}, {}, {}
    bias_ids = (run.get("bias") or {}).get("file_ids") or []
    keys["bias"] = master_key(db, "bias", bias_ids)
    info["bias"] = master_info(db, bias_ids)
    files, session_of_set, night_session = [], {}, {}
    bias_master = _master(db, project_id, keys["bias"])
    if bias_master:
        files.append(dict(path=pi_path(bias_master), type="bias"))
        reused["bias"] = bias_master
    else:
        files += [dict(path=pi_path(p), type="bias") for p in _paths(db, bias_ids).values()]
    for fl in run.get("flats") or []:
        key = tuple(sorted(fl["file_ids"]))
        if not key:
            continue
        if key not in session_of_set:                # Flat-Satz, der für mehrere Nächte gilt, nur einmal
            sess = session_of_set[key] = "S%d" % (len(session_of_set) + 1)
            keys[sess] = master_key(db, "flat", key, keys["bias"] or "")
            info[sess] = master_info(db, key)
            m = _master(db, project_id, keys[sess])
            if m:
                files.append(dict(path=pi_path(m), type="flat", session=sess))
                reused[sess] = m
            else:
                files += [dict(path=pi_path(p), type="flat", session=sess) for p in _paths(db, list(key)).values()]
        night_session[fl["night"]] = session_of_set[key]
    for fid, p in sorted(lights.items(), key=lambda kv: kv[1]):
        files.append(dict(path=pi_path(p), type="light", session=night_session.get(nights.get(fid))))
    dark_ids = (run.get("darks") or {}).get("file_ids") or []
    keys["dark"] = master_key(db, "dark", dark_ids)
    info["dark"] = master_info(db, dark_ids)
    dark_master = _master(db, project_id, keys["dark"])
    if dark_master:
        files.append(dict(path=pi_path(dark_master), type="dark"))
        reused["dark"] = dark_master
    else:
        files += [dict(path=pi_path(p), type="dark") for p in _paths(db, dark_ids).values()]
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
               params=params, indexed=indexed, files=files, masterKeys=keys, reusedMasters=reused,
               masterInfo=info)
    if any(s["key"] == "cosmetic_correction" and s["value"] for s in run.get("settings") or []) and not dark_ids:
        job["cosmetic"] = True
    if any(s["key"] == "drizzle" and s["value"] for s in run.get("settings") or []):
        job["drizzle"] = dict(enabled=True, scale=2, dropShrink=0.9)       # je Light-Gruppe (WBPP 3.2.0 geprüft)
    _check(job)
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
    if job.get("cosmetic"):                       # Hotpixel ohne Darks: CosmeticCorrection (Auto Detect) in WBPP
        from .engines.pixinsight import CC_COLD_SIGMA, CC_HOT_SIGMA
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pjsr", "cc_builder.js")
        with open(src, encoding="utf-8") as fh:
            cc = fh.read().replace("__HOT__", "%.2f" % CC_HOT_SIGMA).replace("__COLD__", "%.2f" % CC_COLD_SIGMA)
        cc_path = os.path.join(temp_dir, "b2cc.js")
        with open(cc_path, "w", encoding="utf-8") as fh:
            fh.write(cc)
        job["params"] = dict(job.get("params") or {}, usePipelineBuilderScript=True,
                             pipelineBuilderScriptFile=pi_path(cc_path))
    _check(job)                                    # letzte Kontrolle: genau so geht der Auftrag an WBPP
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=1, ensure_ascii=False)
    return launcher, job_path


def _check(job: dict) -> None:
    """Alle WBPP-Einstellungen gegen ``wbppschema`` prüfen und in die richtige Art bringen (in place).
    Ein Fehler hier stoppt den Lauf, bevor PixInsight startet – mit Klartext statt „kein Master“ nach Minuten."""
    from . import wbppschema
    try:
        notes = wbppschema.normalize_job(job)
    except wbppschema.WbppParamError as e:
        raise HandoverError(str(e)) from None
    if notes:
        job["paramNotes"] = notes


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
                     message=st.get("name") or "",
                     detail=dict(ops=st.get("ops") or [], started=st.get("started"), current=st.get("name") or ""))
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
