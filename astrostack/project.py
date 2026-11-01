"""Ablauf für ein Ziel: Scan → Kalibrier-Prüfung → Vermessung → Bericht + Plan."""
from __future__ import annotations

import json
import os
import re

from . import analyze, calibcheck, discover
from .report import build as report_build
from .settings import Settings


def safe_name(target: str) -> str:
    return re.sub(r"[^\w\- ]+", "_", target).strip() or "unbenannt"


def project_dir(settings: Settings, target: str) -> str:
    return os.path.join(settings.workdir, "projects", safe_name(target))


def scan(roots, settings: Settings, progress=None):
    opt = discover.ScanOptions(custom_filters=settings.custom_filters)
    return discover.scan(roots, opt, progress)


def targets(inv) -> list[str]:
    seen = []
    for g in inv.lights:
        if g.target not in seen:
            seen.append(g.target)
    return seen


def run_report(roots, target: str | None, settings: Settings, log=print, progress=None, cancel=None) -> list[dict]:
    """Kompletter Durchlauf für ein Ziel (oder alle Ziele, wenn target=None).

    progress(stage=…, done=…, total=…) wird laufend aufgerufen; cancel ist ein threading.Event.
    """
    progress = progress or (lambda **kw: None)
    roots = [os.path.abspath(r) for r in roots]
    log("Scanne %d Ordner …" % len(roots))
    progress(stage="Ordner durchsuchen", step="scan", done=0, total=0)
    inv = scan(roots, settings, lambda i, n: progress(stage="Ordner durchsuchen", step="scan", done=i, total=n))
    log("  %d Light-Gruppen, %d Kalibriersätze, %d unbekannt, %d Fehler" % (len(inv.lights), len(inv.calib), len(inv.unknown), len(inv.errors)))
    if not inv.lights:
        raise ValueError("Keine Light-Frames gefunden. Sind die richtigen Ordner gewählt?")
    log("Prüfe Kalibrierdaten …")
    calib = calibcheck.check_all(inv, progress=lambda i, n, f: (progress(stage="Flats prüfen", step="flats", done=i, total=n), log("  Flats %d/%d %s" % (i, n, f))))
    results = []
    for t in ([target] if target else targets(inv)):
        if cancel is not None and cancel.is_set():
            break
        groups = [g for g in inv.lights if g.target.lower() == t.lower()]
        if not groups:
            log("Ziel nicht gefunden: %s" % t)
            continue
        pdir = project_dir(settings, groups[0].target)
        adir = os.path.join(pdir, "analysis")
        frames = [f for g in groups for f in g.frames]
        log("Vermesse %d Frames von %s …" % (len(frames), groups[0].target))

        def _p(i, n, err, _t=groups[0].target):
            progress(stage="Fotos vermessen", step="measure", target=_t, done=i, total=n)
            if i % 10 == 0 or i == n or err:
                log("  %d/%d%s" % (i, n, ("  FEHLER " + str(err)) if err else ""))
        analyze.analyze_group(frames, adir, settings.analysis_workers, progress=_p)
        with open(os.path.join(pdir, "inventory.json"), "w", encoding="utf-8") as fh:
            json.dump(inv.summary(), fh, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(pdir, "calibration.json"), "w", encoding="utf-8") as fh:
            json.dump(calibcheck.public(calib), fh, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(pdir, "sources.json"), "w", encoding="utf-8") as fh:
            json.dump(dict(roots=roots, target=groups[0].target), fh, indent=2, ensure_ascii=False)
        log("Erzeuge Bericht …")
        progress(stage="Bericht & Plan", step="report", target=groups[0].target, done=0, total=0)
        res = report_build.build(groups[0].target, groups, inv, calib, adir, settings.workdir, os.path.join(pdir, "report.html"))
        apply_edits(pdir)
        meta(pdir)
        res["target"] = groups[0].target
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
        if k in ("name", "description", "checklist"):
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


def list_projects(settings: Settings) -> list[dict]:
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
        st = stacks(pdir)
        last = st[-1] if st else None
        out.append(dict(name=d, title=m["name"], target=m["target"], description=m.get("description", ""),
                        frames=plan.get("total_frames"), runs=[r.get("name") for r in plan.get("runs", [])],
                        updated=max(os.path.getmtime(plan_p), os.path.getmtime(os.path.join(pdir, "stacks.json"))
                                    if os.path.exists(os.path.join(pdir, "stacks.json")) else 0),
                        stacks=len(st), last_stack=last and last.get("date"),
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


def stacks(pdir: str) -> list[dict]:
    """Alle Stacks eines Projekts, älteste zuerst; ältere Läufe (vor stacks.json) werden übernommen."""
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


def save_edits(pdir: str, edits: dict) -> dict:
    """Benutzer-Anpassungen am Plan merken (überleben eine Neu-Analyse) und sofort anwenden.
    edits = {options: {...}, runs: {name: {enabled, reference, wbpp}}}"""
    cur = _read(os.path.join(pdir, "plan_edits.json"), {"options": {}, "runs": {}})
    cur["options"].update(edits.get("options") or {})
    for k, v in (edits.get("runs") or {}).items():
        cur["runs"].setdefault(k, {}).update(v)
    json.dump(cur, open(os.path.join(pdir, "plan_edits.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return apply_edits(pdir)


def apply_edits(pdir: str) -> dict:
    plan = load_plan(pdir)
    ed = _read(os.path.join(pdir, "plan_edits.json"), None)
    if ed:
        plan["options"].update(ed.get("options") or {})
        for r in plan["runs"]:
            r.update({k: v for k, v in (ed.get("runs") or {}).get(r["name"], {}).items() if k in ("enabled", "reference", "wbpp")})
        plan["edited"] = True
        json.dump(plan, open(os.path.join(pdir, "plan.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return plan


def set_override(pdir: str, path: str, value: str | None) -> dict:
    p = os.path.join(pdir, "overrides.json")
    ov = _read(p, {})
    if value in ("in", "out"):
        ov[path] = value
    else:
        ov.pop(path, None)
    json.dump(ov, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return ov


def sources(pdir: str) -> dict:
    return _read(os.path.join(pdir, "sources.json"), {})


def set_sources(pdir: str, roots: list[str]) -> dict:
    roots = [r.strip().strip('"') for r in roots if r and r.strip()]
    if not roots:
        raise ValueError("Mindestens ein Ordner nötig.")
    src = sources(pdir)
    src["roots"] = roots
    _write(os.path.join(pdir, "sources.json"), src)
    return src
