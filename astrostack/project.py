"""Ablauf für ein Ziel: Scan → Kalibrier-Prüfung → Vermessung → Bericht + Plan."""
from __future__ import annotations

import json
import os
import re
import threading
from types import SimpleNamespace as NS

from . import analyze, calibcheck, discover, logbook
from .report import build as report_build
from .settings import Settings


_STATE_LOCK = threading.RLock()   # sources.json/overrides.json read-modify-write between UI threads and analyses


def safe_name(target: str) -> str:
    return re.sub(r"[^\w\- ]+", "_", target).strip() or "unbenannt"


def project_dir(settings: Settings, target: str) -> str:
    return os.path.join(settings.workdir, "projects", safe_name(target))


def catalog_dir(settings: Settings) -> str:
    """Keep reusable measurements independent of project-specific selection state."""
    return os.path.join(settings.workdir, "cache", "catalog")


def scan(roots, settings: Settings, progress=None, target_map: dict | None = None):
    opt = discover.ScanOptions(custom_filters=settings.custom_filters, target_map=dict(target_map or {}),
                               catalog_dir=catalog_dir(settings))
    return discover.scan(roots, opt, progress)


def _sep_deg(ra1, de1, ra2, de2) -> float:
    import math
    r = math.radians
    c = math.sin(r(de1)) * math.sin(r(de2)) + math.cos(r(de1)) * math.cos(r(de2)) * math.cos(r(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def preview_targets(roots, settings: Settings, progress=None) -> dict:
    """Vor dem Anlegen: welche Ziele stecken in den Ordnern? Mit Hinweisen auf vermutlich gleiche Ziele
    (Koordinaten < 0,5°) und auf bestehende Projekte desselben Ziels."""
    import statistics as st
    roots = [os.path.abspath(r) for r in roots]
    inv = scan(roots, settings, progress)
    tg: dict[str, dict] = {}
    for g in inv.lights:
        k = discover.target_key(g.target)
        t = tg.setdefault(k, dict(key=k, name=g.target, names={}, frames=0, excluded=0, hours=0.0, filters=[], nights=set(),
                                  folders=set(), ra=[], dec=[]))
        for n, c in g.names.items():
            t["names"][n] = t["names"].get(n, 0) + c
        used = [f for f in g.frames if not f.excluded]
        t["frames"] += len(used)
        t["excluded"] += len(g.frames) - len(used)
        t["hours"] += sum(f.exposure or 0 for f in used) / 3600
        if g.filter not in t["filters"]:
            t["filters"].append(g.filter)
        t["nights"] |= {f.night for f in used if f.night}
        t["folders"] |= {os.path.dirname(os.path.dirname(f.path)) for f in g.frames[:50]}
        t["ra"] += [f.ra for f in used if f.ra is not None]
        t["dec"] += [f.dec for f in used if f.dec is not None]
    existing = {}
    for p in list_projects(settings):
        existing[discover.target_key(p["target"])] = p
    out = []
    for k, t in tg.items():
        ra = st.median(t["ra"]) if t["ra"] else None
        de = st.median(t["dec"]) if t["dec"] else None
        ex = existing.get(k)
        out.append(dict(key=k, name=t["name"], names=t["names"], frames=t["frames"], excluded=t["excluded"],
                        hours=round(t["hours"], 2), filters=t["filters"], nights=sorted(t["nights"]),
                        folders=sorted(t["folders"])[:5], ra=ra, dec=de,
                        existing=ex and dict(name=ex["name"], title=ex["title"], target=ex["target"])))
    out.sort(key=lambda t: -t["hours"])
    near = []
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if None not in (a["ra"], a["dec"], b["ra"], b["dec"]):
                d = _sep_deg(a["ra"], a["dec"], b["ra"], b["dec"])
                if d < 0.5:
                    near.append(dict(a=a["key"], b=b["key"], deg=round(d, 2)))
    calib = {}
    for c in inv.calib:
        calib[c.type] = calib.get(c.type, 0) + len(c.frames)
    return dict(roots=roots, targets=out, near=near, calib=calib, errors=len(inv.errors), unknown=len(inv.unknown))


def targets(inv) -> list[str]:
    seen = []
    for g in inv.lights:
        if g.target not in seen:
            seen.append(g.target)
    return seen


def run_report(roots, target: str | None, settings: Settings, log=print, progress=None, cancel=None,
               assign: dict | None = None, title: str | None = None, into: str | None = None) -> list[dict]:
    """Kompletter Durchlauf für ein Ziel (oder alle Ziele, wenn target=None).

    progress(stage=…, done=…, total=…) wird laufend aufgerufen; cancel ist ein threading.Event.
    """
    import time
    logbook.setup(settings.workdir)
    progress = progress or (lambda **kw: None)
    roots = _canon_roots(roots)
    t_all = time.time()
    timings = {}
    log("Scanne %d Ordner …" % len(roots))
    progress(stage="Ordner durchsuchen", step="scan", done=0, total=0)
    # assign: {Ziel-Schlüssel: Projektname} – vom Benutzer bestätigte Zuordnung (zusammenlegen / bestehendes Projekt)
    #         Wert None = Ziel nicht anlegen
    assign = dict(assign or {})
    skip_keys = {k for k, v in assign.items() if not v}
    tmap = {k: v for k, v in assign.items() if v}
    if not tmap:                                       # ohne Rückfrage: bestehende Projekte desselben Ziels verwenden
        for p in list_projects(settings):
            tmap.setdefault(discover.target_key(p["target"]), p["target"])
    t0 = time.time()
    inv = scan(roots, settings, lambda i, n: progress(stage="Ordner durchsuchen", step="scan", done=i, total=n), tmap)
    if skip_keys:
        inv.lights = [g for g in inv.lights if discover.target_key(g.target) not in skip_keys]
    if title and not assign:                           # ein Titel für alles Gefundene (nur bei einem Ziel eindeutig)
        tks = {discover.target_key(g.target) for g in inv.lights}
        if len(tks) == 1:
            tmap = {next(iter(tks)): title}
            inv = scan(roots, settings, None, tmap)
    timings["scan"] = round(time.time() - t0, 1)
    log("  %d Light-Gruppen, %d Kalibriersätze, %d unbekannt, %d Fehler" % (len(inv.lights), len(inv.calib), len(inv.unknown), len(inv.errors)))
    if not inv.lights:
        raise ValueError("Keine Light-Frames gefunden. Sind die richtigen Ordner gewählt?")
    # Gibt es ein Ziel schon als Projekt mit weiteren Quellordnern, werden diese mit einbezogen
    extra = []
    for t in ([target] if target else targets(inv)):
        old = sources(os.path.join(settings.workdir, "projects", safe_name(tmap.get(discover.target_key(t), t)))).get("roots") or []
        extra += [r for r in _canon_roots(old) if r not in roots and r not in extra and os.path.isdir(r)]
    if extra:
        log("Projekt existiert bereits – bisherige Ordner werden mit einbezogen: " + ", ".join(extra))
        roots = _canon_roots(roots + extra)
        inv = scan(roots, settings, lambda i, n: progress(stage="Ordner durchsuchen", step="scan", done=i, total=n), tmap)
        if skip_keys:
            inv.lights = [g for g in inv.lights if discover.target_key(g.target) not in skip_keys]
    if into:
        # An existing project keeps its own target even when the folder also holds other objects;
        # a new planning project may not silently absorb several objects.
        pdir_into = project_dir(settings, into)
        known = set()
        if os.path.isdir(pdir_into):
            m = meta(pdir_into)
            known = {discover.target_key(n) for n in (m.get("objects") or []) if n}
            # A project created for one object stores that object as its target, while planning
            # projects keep their own title there.
            if discover.target_key(m.get("target")) in {discover.target_key(g.target) for g in inv.lights}:
                known.add(discover.target_key(m.get("target")))
        if known:
            inv.lights = [g for g in inv.lights
                          if discover.target_key(g.target) in known
                          or any(discover.target_key(n) in known for n in g.names)]
            if not inv.lights:
                raise ValueError("In den Quellordnern wurde kein Frame für „%s“ gefunden – bitte die passenden Ordner wählen." % into)
        else:
            names = sorted({g.target for g in inv.lights})
            if len(names) > 1:
                raise ValueError("Die Quellordner enthalten mehrere Ziele (%s) – für dieses Projekt bitte nur die Ordner eines Ziels wählen."
                                 % ", ".join(names))
        for g in inv.lights:
            g.target = into
        target = into
    canon_roots = _canon_roots(roots)
    results = []
    for t in ([target] if target else targets(inv)):
        t0 = time.time()
        if cancel is not None and cancel.is_set():
            break
        groups = [g for g in inv.lights if discover.target_key(g.target) == discover.target_key(t)]
        if not groups:
            log("Ziel nicht gefunden: %s" % t)
            continue
        pdir = project_dir(settings, groups[0].target)
        if meta(pdir).get("archived") and os.path.isdir(pdir):
            log("Archiviert – wird nicht neu analysiert: %s" % groups[0].target)
            continue
        adir = os.path.join(pdir, "analysis")
        frames = [f for g in groups for f in g.frames]
        os.makedirs(pdir, exist_ok=True)
        with _STATE_LOCK:                     # consistent snapshot plus lock without interleaved writes
            before_roots = _canon_roots(sources(pdir).get("roots") or [])
            before_ov = _read(os.path.join(pdir, "overrides.json"), {})
            _update_sources(pdir, analysis_required=True)
        # Shared calibration files must not imply shared project selections.
        log("Prüfe Kalibrierdaten …")
        t_flat = time.time()
        view = calib_view(inv, before_ov)
        calib = calibcheck.check_all(view, progress=lambda i, n, f: progress(stage="Flats prüfen", step="flats", done=i, total=n),
                                     catalog_dir=catalog_dir(settings))
        timings["flats"] = round(time.time() - t_flat, 1)
        log("Vermesse %d Frames von %s …" % (len(frames), groups[0].target))

        def _p(i, n, err, _t=groups[0].target):
            progress(stage="Fotos vermessen", step="measure", target=_t, done=i, total=n)
            if i % 10 == 0 or i == n or err:
                log("  %d/%d%s" % (i, n, ("  FEHLER " + str(err)) if err else ""))
        analyze.analyze_group(frames, adir, settings.analysis_workers, progress=_p,
                              catalog_dir=catalog_dir(settings), log=log)
        if cancel is not None and cancel.is_set():
            log("Abgebrochen – die Neuanalyse bleibt gesperrt.")
            break
        t_meas = round(time.time() - t0, 1)
        with open(os.path.join(pdir, "inventory.json"), "w", encoding="utf-8") as fh:
            json.dump(inv.summary(), fh, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(pdir, "calibration.json"), "w", encoding="utf-8") as fh:
            json.dump(calibcheck.public(calib), fh, indent=2, ensure_ascii=False, default=str)
        # Header errors belong only to the project whose canonical roots contain the file.
        own_errs = [(p, r) for p, r in inv.errors if _within(p, canon_roots)]
        write_frames_index(pdir, groups, view.calib, errors=own_errs)
        log("Erzeuge Bericht …")
        progress(stage="Bericht & Plan", step="report", target=groups[0].target, done=0, total=0)
        res = report_build.build(groups[0].target, groups, view, calib, adir, settings.workdir, os.path.join(pdir, "report.html"))
        apply_edits(pdir)
        is_new = not os.path.exists(os.path.join(pdir, "project.json"))
        m0 = meta(pdir)
        objs = sorted({n for g in groups for n in g.names})
        if is_new and objs and m0.get("target") != objs[0]:
            save_meta(pdir, name=m0["name"])
            mm = meta(pdir)
            mm["target"] = groups[0].target
            mm["objects"] = objs
            _write(os.path.join(pdir, "project.json"), mm)
        # Release as the last step: a failed report/edit or a concurrent sources/override write stays blocked.
        with _STATE_LOCK:
            cur = sources(pdir)
            raced = (_canon_roots(cur.get("roots") or []) != before_roots
                     or _read(os.path.join(pdir, "overrides.json"), {}) != before_ov
                     or (cancel is not None and cancel.is_set()))
            if raced:
                log("Quellen oder Frame-Auswahl wurden während der Analyse geändert – Neuanalyse nötig.")
                _write(os.path.join(pdir, "sources.json"), dict(cur, analysis_required=True))
            else:
                _write(os.path.join(pdir, "sources.json"),
                       dict(cur, roots=canon_roots, target=groups[0].target,
                            analysis_required=False, plan_stale=False))
        res["target"] = groups[0].target
        try:
            plan = load_plan(pdir)
            logbook.event("analysis", project=safe_name(groups[0].target), roots=roots,
                          duration_s=round(time.time() - t_all, 1), timings=dict(timings, measure=t_meas),
                          frames=len(frames), errors=len(inv.errors), unknown=len(inv.unknown),
                          runs=[dict(name=r["name"], lights=r["lightCount"], hours=r.get("hours"), flats=len(r["flats"]),
                                     flat_set=r.get("flat_set"), bias=len(r["bias"]), bias_set=r.get("bias_set"),
                                     darks=len(r["darks"]), flat_groups=len(r.get("flat_groups") or []) or None)
                                for r in plan.get("runs", [])],
                          findings=[f["title"] for m in calib["matches"] if m["group"]["target"] == groups[0].target
                                    for f in m["findings"]])
        except Exception:  # noqa: BLE001
            logbook.error("Analyse-Protokoll fehlgeschlagen")
        res["project"] = safe_name(groups[0].target)
        log("  " + res["html"])
        results.append(res)
    return results


# ------------------------------------------------------------ Projekte (für Oberfläche/KI)
#
# Langlebigkeit: Jedes Projekt hat eine project.json mit Schema-Version. Alle Dateien werden tolerant
# gelesen (fehlende Felder = Standardwerte), Pfade zu eigenen Ergebnissen werden relativ zum Projektordner
# gespeichert – so lässt sich ein Projekt auch nach Jahren, mit neuer Programmversion oder nach dem
# Verschieben des Arbeitsverzeichnisses öffnen und weiterführen.

SCHEMA = 2


def meta(pdir: str) -> dict:
    """project.json lesen (oder aus älteren Dateien ableiten)."""
    m = _read(os.path.join(pdir, "project.json"), None)
    if not isinstance(m, dict):
        src = _read(os.path.join(pdir, "sources.json"), {})
        plan = _read(os.path.join(pdir, "plan.json"), {})
        tgt = src.get("target") or plan.get("target") or os.path.basename(pdir)
        m = dict(schema=SCHEMA, name=tgt, target=tgt, description="",
                 created=plan.get("created") or _mtime_iso(pdir), checklist={})
        try:
            _write(os.path.join(pdir, "project.json"), m)
        except OSError:
            pass
    m.setdefault("name", m.get("target") or os.path.basename(pdir))
    m.setdefault("target", m["name"])
    m.setdefault("description", "")
    m.setdefault("checklist", {})
    return m


def save_meta(pdir: str, **changes) -> dict:
    m = meta(pdir)
    for k, v in changes.items():
        if k == "name":
            v = (v or "").strip()
            if not v:
                raise ValueError("Der Name darf nicht leer sein.")
        if k in ("name", "description", "checklist", "thumb"):
            m[k] = v
    m["schema"] = SCHEMA
    _write(os.path.join(pdir, "project.json"), m)
    return m


def _write(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _mtime_iso(p: str) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")
    except OSError:
        return ""


def list_projects(settings: Settings, include_archived: bool = False) -> list[dict]:
    base = os.path.join(settings.workdir, "projects")
    out = []
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base)):
        pdir = os.path.join(base, d)
        plan_p = os.path.join(pdir, "plan.json")
        if not os.path.exists(plan_p):
            continue
        plan = _read(plan_p, {})
        m = meta(pdir)
        if m.get("archived") and not include_archived:
            continue
        st = stacks(pdir)
        last = st[-1] if st else None
        facts = project_facts(pdir, plan, st)
        out.append(dict(name=d, title=m["name"], target=m["target"], description=m.get("description", ""),
                        archived=bool(m.get("archived")),
                        hours=facts["hours_total"], filters=[f["name"] for f in facts["filters"]],
                        last_night=facts["last_night"], created=m.get("created"),
                        frames=plan.get("total_frames"), runs=[r.get("name") for r in plan.get("runs", [])],
                        updated=max(os.path.getmtime(plan_p), os.path.getmtime(os.path.join(pdir, "stacks.json"))
                                    if os.path.exists(os.path.join(pdir, "stacks.json")) else 0),
                        stacks=len(st), last_stack=last and last.get("date"),
                        thumb_v="%s|%s" % (m.get("thumb") or "", last and last.get("date")),
                        has_report=os.path.exists(os.path.join(pdir, "report.html"))))
    out.sort(key=lambda p: -p["updated"])
    return out


# ------------------------------------------------------------ Stack-Historie

def _rel(pdir: str, path: str | None) -> str | None:
    if not path:
        return path
    try:
        r = os.path.relpath(path, pdir)
        return path if r.startswith("..") else r.replace("\\", "/")
    except ValueError:            # anderes Laufwerk
        return path


def _abs(pdir: str, path: str | None) -> str | None:
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(pdir, path))


def add_stack(pdir: str, rec: dict) -> dict:
    """Einen fertigen Stack in stacks.json eintragen (Pfade relativ zum Projekt)."""
    lst = _read(os.path.join(pdir, "stacks.json"), [])
    rec = dict(rec, schema=SCHEMA)
    rec["outputs"] = [dict(o, file=_rel(pdir, o.get("file")), preview=_rel(pdir, o.get("preview"))) for o in rec.get("outputs", [])]
    try:
        from . import __version__
        rec.setdefault("app_version", __version__)
    except ImportError:
        pass
    lst = [x for x in lst if x.get("id") != rec.get("id")] + [rec]
    _write(os.path.join(pdir, "stacks.json"), lst)
    return rec


def stacks(pdir: str, include_removed: bool = False) -> list[dict]:
    """Retain removed records so legacy discovery cannot resurrect them or touch result files."""
    p = os.path.join(pdir, "stacks.json")
    lst = _read(p, None)
    if lst is None:
        lst = _legacy_stacks(pdir)
        if lst:
            try:
                _write(p, lst)
            except OSError:
                pass
    out = []
    for r in lst or []:
        if r.get("removed") and not include_removed:
            continue
        r = dict(r)
        r["outputs"] = [dict(o, file=_abs(pdir, o.get("file")), preview=_abs(pdir, o.get("preview")),
                             exists=bool(o.get("file") and os.path.exists(_abs(pdir, o.get("file")))))
                        for o in r.get("outputs", [])]
        out.append(r)
    out.sort(key=lambda r: r.get("date") or "")
    return out


def _legacy_stacks(pdir: str) -> list[dict]:
    """Läufe aus Version 0.1 (runs/<zeit>/status.json) in die Historie übernehmen – ohne Kennzahlen."""
    res = []
    rdir = os.path.join(pdir, "runs")
    if not os.path.isdir(rdir):
        return res
    for stamp in sorted(os.listdir(rdir)):
        stj = _read(os.path.join(rdir, stamp, "status.json"), None)
        if not stj or stj.get("state") != "done":
            continue
        plan = _read(os.path.join(rdir, stamp, "plan.json"), {})
        for name, r in (stj.get("runs") or {}).items():
            prun = next((x for x in plan.get("runs", []) if x.get("name") == name), {})
            res.append(dict(id="%s-%s" % (stamp, name), run_id=stamp, date=stj.get("finished"), run=name,
                            filter=prun.get("filter"), mode=prun.get("mode"), lights=prun.get("lightCount"),
                            hours=prun.get("hours") or None, nights=prun.get("nights"), minutes=r.get("minutes"),
                            flats=len(prun.get("flats", [])), bias=len(prun.get("bias", [])), legacy=True,
                            options=plan.get("options", {}), schema=SCHEMA,
                            outputs=[dict(name=os.path.splitext(os.path.basename(f))[0].split("_")[-1], file=_rel(pdir, f),
                                          preview=None, kpi=None) for f in r.get("finals", [])]))
    return res


def update_stack_output(pdir: str, stack_id: str, index: int, **changes):
    lst = _read(os.path.join(pdir, "stacks.json"), [])
    for r in lst:
        if r.get("id") == stack_id and index < len(r.get("outputs", [])):
            o = r["outputs"][index]
            for k, v in changes.items():
                o[k] = _rel(pdir, v) if k in ("file", "preview") else v
    _write(os.path.join(pdir, "stacks.json"), lst)


def update_stack_meta(pdir: str, stack_id: str, name: str | None = None) -> dict:
    """Change display metadata without renaming result files."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Der Name darf nicht leer sein.")
    return _update_stack_record(pdir, stack_id, name=name)


def remove_stack(pdir: str, stack_id: str) -> dict:
    """Hide history metadata without deleting the corresponding result files."""
    return _update_stack_record(pdir, stack_id, removed=True)


def _update_stack_record(pdir: str, stack_id: str, **changes) -> dict:
    p = os.path.join(pdir, "stacks.json")
    lst = _read(p, None)
    if lst is None:
        lst = _legacy_stacks(pdir)
    rec = next((r for r in lst if r.get("id") == stack_id), None)
    if rec is None:
        raise ValueError("Stack nicht gefunden: " + stack_id)
    rec.update(changes)
    _write(p, lst)
    return rec


def archive_project(pdir: str) -> dict:
    """Hide the project through metadata while keeping every project file intact."""
    m = meta(pdir)
    m["archived"] = True
    m["schema"] = SCHEMA
    _write(os.path.join(pdir, "project.json"), m)
    return m


def create_project(settings: Settings, title: str, roots: list[str] | None = None) -> str:
    """Reserve project metadata before any light frames are available."""
    import datetime
    title = (title or "").strip()
    if not title:
        raise ValueError("Der Titel darf nicht leer sein.")
    pdir = project_dir(settings, title)
    if os.path.exists(pdir):
        raise ValueError("Ein Projekt mit diesem Namen existiert schon: " + title)
    roots = _canon_roots([r.strip().strip('"') for r in roots or [] if r and r.strip()])
    os.makedirs(pdir)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    _write(os.path.join(pdir, "project.json"), dict(schema=SCHEMA, name=title, target=title, description="",
                                                    created=now, checklist={}))
    _write(os.path.join(pdir, "plan.json"), dict(target=title, created=now, total_frames=0, options={}, runs=[]))
    _write(os.path.join(pdir, "sources.json"), dict(roots=roots, target=title, analysis_required=bool(roots)))
    return pdir


def assert_stack_ready(pdir: str) -> None:
    """Reject stale plans consistently for HTTP, CLI and job callers."""
    if sources(pdir).get("analysis_required"):
        raise ValueError("Die Quellordner wurden geändert – bitte zuerst neu analysieren.")
    if sources(pdir).get("plan_stale"):
        raise ValueError("Frame-Auswahl wurde geändert – bitte zuerst neu analysieren.")
    plan = _read(os.path.join(pdir, "plan.json"), {})
    if not plan.get("runs") or not plan.get("total_frames"):
        if not sources(pdir).get("roots"):
            raise ValueError("Keine Quellordner hinterlegt – Ordner wählen und analysieren.")
        raise ValueError("Keine gültige Analyse mit Light-Frames – bitte zuerst analysieren.")


def _canon_roots(roots) -> list[str]:
    """Resolve source aliases so repeated folders cannot double-count images."""
    return list(discover.normalize_roots(roots or []))


def _within(path: str, roots) -> bool:
    """True if the canonical path lies inside one of the canonical roots (Windows-drive safe)."""
    p = os.path.normcase(os.path.realpath(path))
    for r in roots:
        try:
            if os.path.commonpath([p, os.path.normcase(r)]) == os.path.normcase(r):
                return True
        except ValueError:                        # different drives
            continue
    return False


def latest_output(pdir: str, run_name: str) -> str | None:
    """Neuestes vorhandenes Ergebnis eines Laufs (z. B. RGB als Referenz für HaO3)."""
    for r in reversed(stacks(pdir)):
        if r.get("run") == run_name:
            for o in r.get("outputs", []):
                if o.get("exists"):
                    return o["file"]
    return None


# ------------------------------------------------------------ Arbeitsverzeichnis wechseln

def foreign_projects(old_workdir: str, new_workdir: str) -> list[str]:
    """Projekte im alten Arbeitsverzeichnis, die es im neuen noch nicht gibt."""
    a, b = os.path.join(old_workdir, "projects"), os.path.join(new_workdir, "projects")
    if not os.path.isdir(a) or os.path.abspath(a) == os.path.abspath(b):
        return []
    have = set(os.listdir(b)) if os.path.isdir(b) else set()
    return [d for d in sorted(os.listdir(a)) if os.path.isdir(os.path.join(a, d)) and d not in have]


def move_projects(old_workdir: str, new_workdir: str, names: list[str]) -> list[str]:
    import shutil
    moved = []
    os.makedirs(os.path.join(new_workdir, "projects"), exist_ok=True)
    for n in names:
        src = os.path.join(old_workdir, "projects", n)
        dst = os.path.join(new_workdir, "projects", n)
        if os.path.isdir(src) and not os.path.exists(dst):
            shutil.move(src, dst)
            moved.append(n)
    return moved


def project_facts(pdir: str, plan: dict | None = None, st: list | None = None) -> dict:
    """Eckdaten für das Dashboard: Belichtung, Lights, Nächte, Filter, Stacks."""
    plan = plan if plan is not None else _read(os.path.join(pdir, "plan.json"), {})
    st = st if st is not None else stacks(pdir)
    inv = _read(os.path.join(pdir, "inventory.json"), {})
    tgt = (meta(pdir).get("target") or "").lower()
    groups = [g for g in inv.get("lights", []) if (g.get("target") or "").lower() == tgt]
    filters = {}
    nights = set()
    for g in groups:
        f = filters.setdefault(g.get("filter") or "–", dict(name=g.get("filter") or "–", hours=0.0, lights=0, used=0, used_hours=0.0))
        # Older inventories lack the total that includes excluded lights.
        h = g.get("hours_total")
        f["hours"] += (g.get("hours") if h is None else h) or 0
        f["lights"] += g.get("frames") or 0
        nights |= set((g.get("nights") or {}).keys())
    for r in plan.get("runs", []):
        f = filters.setdefault(r.get("filter") or "–", dict(name=r.get("filter") or "–", hours=0.0, lights=0, used=0, used_hours=0.0))
        f["used"] += r.get("lightCount") or 0
        f["used_hours"] += r.get("hours") or 0
    fl = sorted(filters.values(), key=lambda x: -x["hours"])
    ns = sorted(n for n in nights if n and n != "?")
    return dict(hours_total=round(sum(f["hours"] for f in fl), 2), hours_used=round(sum(f["used_hours"] for f in fl), 2),
                lights_total=sum(f["lights"] for f in fl), lights_used=sum(f["used"] for f in fl),
                nights=len(ns), first_night=ns[0] if ns else None, last_night=ns[-1] if ns else None,
                filters=[dict(f, hours=round(f["hours"], 2), used_hours=round(f["used_hours"], 2)) for f in fl],
                stacks=len(st), stacked_hours=round(sum(x.get("hours") or 0 for x in st), 2),
                last_stack=st[-1].get("date") if st else None)


def thumbnail(pdir: str) -> str | None:
    """Vorschaubild für die Projekt-Kachel: selbst gewähltes Bild, sonst neuester Stack, sonst ein gutes Rohbild."""
    own = meta(pdir).get("thumb")
    if own:
        p = _abs(pdir, own)
        if p and os.path.exists(p):
            return p
    for r in reversed(stacks(pdir)):
        for o in r.get("outputs", []):
            if o.get("preview") and os.path.exists(o["preview"]):
                return o["preview"]
    out = os.path.join(pdir, "thumb.jpg")
    mcsv = os.path.join(pdir, "analysis", "metrics.csv")
    if not os.path.exists(mcsv):
        return _asset_thumb(pdir)
    if os.path.exists(out) and (not os.path.exists(mcsv) or os.path.getmtime(out) >= os.path.getmtime(mcsv)):
        return out
    try:
        import csv
        import numpy as np
        from PIL import Image
        from .analyze import thumb_name
        from .stackqa import stretch
        rows = list(csv.DictReader(open(mcsv, encoding="utf-8")))
        rows = [r for r in rows if r.get("nstars")]
        best = max(rows, key=lambda r: float(r["nstars"] or 0))
        t = np.load(os.path.join(pdir, "analysis", "thumbs", thumb_name(best["path"]))).astype(np.float32)
        t = (t - t.min()) / max(float(t.max() - t.min()), 1e-6)
        im = Image.fromarray((stretch(t) * 255).astype(np.uint8))
        im.save(out, "JPEG", quality=82)
        return out
    except Exception:  # noqa: BLE001
        return _asset_thumb(pdir)


def _asset_thumb(pdir: str) -> str | None:
    """Allow projects without raw lights to use an existing image as their thumbnail."""
    from . import assets
    for a in assets.discover(pdir):
        if a["exists"]:
            p, err = assets.preview(pdir, a["id"])
            if p:
                return p
    return None


def detail_extras(pdir: str) -> dict:
    """Keep planning projects readable before analysis artifacts exist."""
    from . import assets
    plan = _read(os.path.join(pdir, "plan.json"), {}) or {}
    plan.setdefault("runs", [])
    plan.setdefault("options", {})
    plan.setdefault("total_frames", 0)
    plan.setdefault("target", meta(pdir).get("target"))
    stale = bool(sources(pdir).get("plan_stale"))
    return dict(plan=plan, analysis_required=bool(sources(pdir).get("analysis_required")) or stale, plan_stale=stale,
                assets=assets.discover(pdir), asset_scan_locations=assets.scan_locations(pdir))


def set_thumbnail(pdir: str, stack_preview: str | None = None, frame: str | None = None) -> dict:
    """Projektbild festlegen: Vorschau eines Stacks, ein Rohbild (wird als Vorschau erzeugt) oder Automatik."""
    if stack_preview:
        p = os.path.normpath(_abs(pdir, stack_preview))
        if not p.startswith(os.path.join(pdir, "stacks")) or not os.path.exists(p):
            raise ValueError("Vorschau nicht gefunden")
        return save_meta(pdir, thumb=_rel(pdir, p))
    if frame:
        from .previews import frame_preview
        p = frame_preview(frame, os.path.join(pdir, "analysis", "previews"))
        return save_meta(pdir, thumb=_rel(pdir, p))
    return save_meta(pdir, thumb=None)


def pdir_of(settings: Settings, name: str) -> str:
    p = os.path.join(settings.workdir, "projects", safe_name(name))
    if not os.path.isdir(p):
        raise FileNotFoundError("Projekt nicht gefunden: " + name)
    return p


def load_plan(pdir: str) -> dict:
    return json.load(open(os.path.join(pdir, "plan.json"), encoding="utf-8"))


def _read(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


CALIB_KINDS = {"flats": "flat_set", "bias": "bias_set", "darks": "dark_set", "darkflats": "darkflat_set"}


def save_edits(pdir: str, edits: dict) -> dict:
    """Benutzer-Anpassungen am Plan merken (überleben eine Neu-Analyse) und sofort anwenden.
    edits = {options: {...}, runs: {name: {enabled, reference, wbpp, calib: {flats|bias|darks|darkflats: wahl}}}}
    Kalibrier-Wahl: "auto" | "none" | "<Kandidaten-id>" | {"folder": "<pfad>"}"""
    cur = _read(os.path.join(pdir, "plan_edits.json"), {"options": {}, "runs": {}})
    cur.setdefault("options", {}).update(edits.get("options") or {})
    for k, v in (edits.get("runs") or {}).items():
        r = cur.setdefault("runs", {}).setdefault(k, {})
        for kk, vv in v.items():
            if kk == "calib":
                cal = r.setdefault("calib", {})
                for kind, choice in vv.items():
                    if kind not in CALIB_KINDS:
                        raise ValueError("unbekannte Kalibrierart: " + kind)
                    if choice in (None, "auto"):
                        cal.pop(kind, None)
                    else:
                        cal[kind] = choice
            else:
                r[kk] = vv
    _write(os.path.join(pdir, "plan_edits.json"), cur)
    return apply_edits(pdir)


def apply_edits(pdir: str) -> dict:
    """plan.json = plan_base.json (Vorschlag der Analyse) + plan_edits.json (Änderungen des Benutzers)."""
    base_p = os.path.join(pdir, "plan_base.json")
    plan = _read(base_p, None) if os.path.exists(base_p) else None
    if plan is None:
        plan = load_plan(pdir)
    ed = _read(os.path.join(pdir, "plan_edits.json"), None)
    if ed:
        cands = _read(os.path.join(pdir, "calib_candidates.json"), {})
        plan.setdefault("options", {}).update(ed.get("options") or {})
        for r in plan["runs"]:
            e = (ed.get("runs") or {}).get(r["name"], {})
            r.update({k: v for k, v in e.items() if k in ("enabled", "reference", "wbpp")})
            for kind, choice in (e.get("calib") or {}).items():
                _apply_calib(r, kind, choice, cands)
        plan["edited"] = True
    # Manual folder choices must not reintroduce explicitly excluded calibration frames.
    ov = _read(os.path.join(pdir, "overrides.json"), {})
    if ov:
        for r in plan["runs"]:
            for kind in CALIB_KINDS:
                if isinstance(r.get(kind), list):
                    r[kind] = [p for p in r[kind] if ov.get(p) != "out"]
            for x in r.get("flat_groups") or []:
                x["flats"] = [p for p in x.get("flats", []) if ov.get(p) != "out"]
    _write(os.path.join(pdir, "plan.json"), plan)
    return plan


def _apply_calib(run: dict, kind: str, choice, cands: dict):
    setkey = CALIB_KINDS[kind]
    run.setdefault("calib_manual", {})
    if kind == "flats" and run.get("flat_groups"):
        run["flat_groups_auto"] = run.pop("flat_groups")     # eigene Wahl gilt für alle Nächte
    if choice == "none":
        run[kind], run[setkey] = [], None
        run.setdefault("calib_master", {})[kind] = False
        run["calib_manual"][kind] = "keine"
    elif isinstance(choice, dict) and choice.get("folder"):
        paths = calib_folder_files(choice["folder"])
        run[kind], run[setkey] = paths, choice["folder"]
        run["calib_manual"][kind] = "eigener Ordner"
        run.setdefault("calib_master", {})[kind] = len(paths) == 1 and os.path.basename(paths[0]).lower().startswith("master")
        if kind == "flats":
            run["flat_ok"], run["flat_sky"], run["flat_session"], run["flat_reasons"] = True, False, None, ["eigener Ordner"]
    elif isinstance(choice, str) and choice in cands:
        c = next((x for x in run.get("calib_choices", {}).get(kind, []) if x["id"] == choice), None)
        run[kind] = list(cands[choice])
        run[setkey] = c["folder"] if c else os.path.dirname(cands[choice][0]) if cands[choice] else None
        run["calib_manual"][kind] = "gewählt"
        run.setdefault("calib_master", {})[kind] = bool(c and c.get("is_master"))
        if kind == "flats" and c:
            run["flat_ok"] = c.get("verdict") != "critical"
            run["flat_sky"], run["flat_session"], run["flat_reasons"] = c.get("sky", False), c.get("session"), c.get("reasons", [])
    else:
        run["calib_manual"][kind] = "Auswahl nicht mehr vorhanden – Vorschlag verwendet"


IMAGE_EXT = (".fits", ".fit", ".fts", ".xisf")


def calib_folder_files(folder: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        out += [os.path.join(root, f) for f in files if f.lower().endswith(IMAGE_EXT)]
    return sorted(out)


def inspect_calib_folder(folder: str, kind: str, run: dict) -> dict:
    """Eigenen Kalibrier-Ordner prüfen: Anzahl, erkannter Typ, Einstellungen im Vergleich zu den Lights."""
    from collections import Counter
    from . import fitsmeta as fm
    if not os.path.isdir(folder):
        raise ValueError("Ordner nicht gefunden: " + folder)
    files = calib_folder_files(folder)
    if not files:
        raise ValueError("Keine FITS/XISF-Dateien in " + folder)
    metas, errors = [], 0
    for p in files[:200]:
        try:
            metas.append(fm.read_meta(p))
        except Exception:  # noqa: BLE001
            errors += 1
    want = {"flats": fm.FLAT, "bias": fm.BIAS, "darks": fm.DARK, "darkflats": fm.DARK}[kind]
    types = Counter(m.type for m in metas)
    warnings, problems = [], []
    other = {t: n for t, n in types.items() if t not in (want, fm.DARKFLAT if kind == "darkflats" else want)}
    if other:
        warnings.append("Nicht alle Dateien sehen nach %s aus (%s) – sie werden trotzdem als %s verwendet."
                        % ({"flats": "Flats", "bias": "Bias", "darks": "Darks", "darkflats": "Dark-Flats"}[kind],
                           ", ".join("%d× %s" % (n, t) for t, n in other.items()), kind))
    sk = run.get("settings_key") or []
    if sk:
        from .calibcheck import _settings_mismatch, _camera_note
        mism = Counter()
        for m in metas:
            for x in _settings_mismatch(tuple(sk), m.settings_key()):
                mism[x] += 1
        for x, n in mism.items():
            problems.append("%s (%d von %d Dateien)" % (x, n, len(metas)))
        notes = {x for m in metas for x in _camera_note(tuple(sk), m.settings_key())}
        warnings += sorted(notes)
    if kind == "flats":
        fl = Counter((m.filter or "–") for m in metas)
        if run.get("filter") and run["filter"] not in fl:
            warnings.append("Filter im Header: %s – die Lights haben %s." % (", ".join(fl), run["filter"]))
    if len(files) < 5:
        warnings.append("Nur %d Dateien – ein Master daraus rauscht stark." % len(files))
    if errors:
        warnings.append("%d Dateien nicht lesbar." % errors)
    return dict(folder=folder, frames=len(files), types=dict(types), problems=problems, warnings=warnings,
                ok=not problems)


def set_override(pdir: str, path: str, value: str | None) -> dict:
    with _STATE_LOCK:
        # Persist the lock first: a crash must never leave a ready plan with a changed selection.
        _update_sources(pdir, plan_stale=True)
        p = os.path.join(pdir, "overrides.json")
        ov = _read(p, {})
        if value in ("in", "out"):
            ov[path] = value
        else:
            ov.pop(path, None)
        _write(p, ov)
        return ov


def _update_sources(pdir: str, **changes) -> dict:
    with _STATE_LOCK:
        src = sources(pdir)
        src.update(changes)
        _write(os.path.join(pdir, "sources.json"), src)
        return src


def write_frames_index(pdir: str, lights, calibs, errors=None) -> list[dict]:
    """Persist all frame membership so browsing does not need to reread image headers."""
    rows = []
    for g in lights:
        for f in g.frames:
            rows.append(dict(path=f.path, type="light", filter=f.filter, night=f.night, excluded=bool(f.excluded)))
    for c in calibs:
        for f in list(c.frames) + list(getattr(c, "hidden", []) or []):
            rows.append(dict(path=f.path, type=c.type, filter=f.filter, night=f.night, excluded=bool(f.excluded)))
    for path, reason in errors or []:          # A broken header provides no trustworthy frame type.
        rows.append(dict(path=path, type="unknown", filter=None, night=None, excluded=True, error=str(reason)))
    os.makedirs(os.path.join(pdir, "analysis"), exist_ok=True)
    _write(os.path.join(pdir, "analysis", "frames.json"), rows)
    return rows


def apply_frame_overrides(calibs, ov: dict) -> None:
    """Keep excluded calibration frames browseable without including them in plans."""
    for c in calibs:
        kept, hidden = [], list(getattr(c, "hidden", []) or [])
        for f in c.frames:
            v = ov.get(f.path)
            if v == "out":
                hidden.append(f)
                continue
            if v == "in":
                f.excluded = False
            kept.append(f)
        c.frames = kept
        c.hidden = hidden


def calib_view(inv, ov: dict):
    """Copy calibration groups so one project's choices cannot affect another project."""
    import copy
    sets = []
    for c in inv.calib:
        cc = copy.copy(c)
        cc.frames = [copy.copy(f) for f in c.frames]
        cc.hidden = [copy.copy(f) for f in getattr(c, "hidden", []) or []]
        sets.append(cc)
    apply_frame_overrides(sets, ov)
    return NS(lights=inv.lights, calib=sets, unknown=inv.unknown, errors=inv.errors)


METRIC_KEYS = ("nstars", "ecc", "fwhm", "hfr_name", "bg", "noise", "snr_proxy")
MEASURE_FAILED = "nicht vermessen – Messung fehlgeschlagen oder Datei nicht lesbar (Analyse-Protokoll prüfen)"
def frames_summary(rows: list[dict]) -> dict:
    """Include failed inputs in overview counts instead of silently hiding them."""
    out = dict(total=len(rows), selected=0, stale=0)
    for k in ("ok", "warn", "suggest", "rejected", "error"):
        out[k] = 0
    for r in rows:
        out[r.get("status")] = out.get(r.get("status"), 0) + 1
        out["selected"] += 1 if r.get("selected") else 0
        out["stale"] += 1 if r.get("stale") else 0
    return out


def _measure_errors(pdir: str) -> dict:
    """Accept structured errors and older per-path error mappings."""
    raw = _read(os.path.join(pdir, "analysis", "measure_errors.json"), [])
    if isinstance(raw, dict):
        items = [dict(path=k, reason=v) for k, v in raw.items()]
    else:
        items = raw if isinstance(raw, list) else []
    out = {}
    for e in items:
        if isinstance(e, dict) and e.get("path"):
            out[e["path"]] = (e.get("kind") or "measure", e.get("reason") or "")
    return out


def _measure_text(kind: str | None, reason: str | None) -> str:
    """Distinguish unreadable inputs, concurrent changes and measurement failures."""
    why = reason or "Grund unbekannt"
    if kind == "stat":
        return "Datei nicht lesbar – " + why
    if kind == "changed":
        return "während der Messung geändert – " + why
    if kind == "measure":
        return "nicht vermessen – " + why
    return MEASURE_FAILED


def indexed_frame_paths(pdir: str) -> set:
    """Validate preview membership without reassessing quality for every image request."""
    rows = _read(os.path.join(pdir, "analysis", "frames.json"), None)
    if rows is not None:
        return {r.get("path") for r in rows if isinstance(r, dict) and r.get("path")}
    return {p for p in _metrics_rows(pdir) if p}


def frames(pdir: str) -> list[dict]:
    """Keep measured quality separate from selection and retain failed inputs for inspection."""
    met = _metrics_rows(pdir)
    rows = _read(os.path.join(pdir, "analysis", "frames.json"), None)
    if rows is None:                           # Older projects retain membership in their measurements.
        rows = [dict(path=p, type="light", filter=r.get("filter") or None, night=r.get("night") or None,
                     excluded=r.get("excluded") in ("1", "True", "true")) for p, r in met.items() if p]
    ov = _read(os.path.join(pdir, "overrides.json"), {})
    stale_all = bool(sources(pdir).get("plan_stale"))
    assess = _read(os.path.join(pdir, "analysis", "assessment.json"), {})
    errs = _measure_errors(pdir)
    out = []
    for r in rows:
        p = r["path"]
        a = assess.get(p)
        if isinstance(a, str):                  # Older assessments stored only a status string.
            a = dict(auto=a, status=a, flags=[])
        mrow = met.get(p)
        is_light = r.get("type") == "light"
        kind, reason = errs.get(p, (None, None))
        if r.get("type") == "unknown":                          # Unreadable headers cannot enter a plan.
            measure_error, kind = "Datei nicht lesbar – " + (r.get("error") or "Grund unbekannt"), "header"
        elif is_light and mrow is None:
            measure_error = _measure_text(kind, reason)
        else:
            measure_error = None
            kind = None
        if measure_error:
            auto = status = "error"
            flags = [dict(level="error", text=measure_error)]
        elif a:
            auto, status = a.get("auto", "ok"), a.get("status", "ok")
            flags = [dict(level=lvl, text=txt) for lvl, txt in a.get("flags", [])]
        else:
            auto = status = "rejected" if r.get("excluded") else "ok"
            flags = []
        if p in ov:
            selected, source = ov[p] == "in", "override"
        else:
            selected, source = auto not in ("rejected", "suggest", "error"), "auto"
        if measure_error:                      # An override cannot replace a missing measurement.
            selected = False
        metrics = {k: _num(mrow.get(k)) if mrow and mrow.get(k) not in (None, "") else None for k in METRIC_KEYS}
        out.append(dict(r, selected=selected, selection_source=source, auto_status=auto, status=status,
                        flags=flags, stale=bool(source == "override" and stale_all), measure_error=measure_error,
                        measure_kind=kind,
                        attention=status if status in ("error", "suggest", "warn") else None, metrics=metrics))
    return out


def _metrics_rows(pdir: str) -> dict:
    import csv
    try:
        with open(os.path.join(pdir, "analysis", "metrics.csv"), encoding="utf-8") as fh:
            return {r.get("path"): r for r in csv.DictReader(fh)}
    except OSError:
        return {}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def sources(pdir: str) -> dict:
    return _read(os.path.join(pdir, "sources.json"), {})


def set_sources(pdir: str, roots: list[str]) -> dict:
    roots = _canon_roots([r.strip().strip('"') for r in roots if r and r.strip()])
    with _STATE_LOCK:
        src = sources(pdir)
        if _canon_roots(src.get("roots") or []) != roots:
            src["analysis_required"] = True       # Changed sources invalidate the existing stacking plan.
        src["roots"] = roots
        _write(os.path.join(pdir, "sources.json"), src)
        return src
