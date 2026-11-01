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
    progress(stage="Ordner durchsuchen", done=0, total=0)
    inv = scan(roots, settings, lambda i, n: progress(stage="Header lesen", done=i, total=n))
    log("  %d Light-Gruppen, %d Kalibriersätze, %d unbekannt, %d Fehler" % (len(inv.lights), len(inv.calib), len(inv.unknown), len(inv.errors)))
    if not inv.lights:
        raise ValueError("Keine Light-Frames gefunden. Sind die richtigen Ordner gewählt?")
    log("Prüfe Kalibrierdaten …")
    calib = calibcheck.check_all(inv, progress=lambda i, n, f: (progress(stage="Flats prüfen", done=i, total=n), log("  Flats %d/%d %s" % (i, n, f))))
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
            progress(stage="Vermessen: " + _t, done=i, total=n)
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
        progress(stage="Bericht: " + groups[0].target, done=0, total=0)
        res = report_build.build(groups[0].target, groups, inv, calib, adir, settings.workdir, os.path.join(pdir, "report.html"))
        apply_edits(pdir)
        res["target"] = groups[0].target
        res["project"] = safe_name(groups[0].target)
        log("  " + res["html"])
        results.append(res)
    return results


# ------------------------------------------------------------ Projekte (für Oberfläche/KI)

def list_projects(settings: Settings) -> list[dict]:
    base = os.path.join(settings.workdir, "projects")
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        pdir = os.path.join(base, name)
        plan_p = os.path.join(pdir, "plan.json")
        if not os.path.exists(plan_p):
            continue
        try:
            plan = json.load(open(plan_p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        finals = sorted(os.listdir(os.path.join(pdir, "final"))) if os.path.isdir(os.path.join(pdir, "final")) else []
        out.append(dict(name=name, target=plan.get("target", name), frames=plan.get("total_frames"),
                        runs=[r["name"] for r in plan.get("runs", [])], updated=os.path.getmtime(plan_p),
                        finals=[f for f in finals if f.endswith(".xisf")], has_report=os.path.exists(os.path.join(pdir, "report.html"))))
    out.sort(key=lambda p: -p["updated"])
    return out


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
