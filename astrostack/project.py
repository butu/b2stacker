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


def run_report(roots, target: str | None, settings: Settings, log=print) -> list[dict]:
    """Kompletter Durchlauf für ein Ziel (oder alle Ziele, wenn target=None)."""
    log("Scanne %d Ordner …" % len(roots))
    inv = scan(roots, settings, None)
    log("  %d Light-Gruppen, %d Kalibriersätze, %d unbekannt, %d Fehler" % (len(inv.lights), len(inv.calib), len(inv.unknown), len(inv.errors)))
    log("Prüfe Kalibrierdaten …")
    calib = calibcheck.check_all(inv, progress=lambda i, n, f: log("  Flats %d/%d %s" % (i, n, f)))
    results = []
    for t in ([target] if target else targets(inv)):
        groups = [g for g in inv.lights if g.target.lower() == t.lower()]
        if not groups:
            log("Ziel nicht gefunden: %s" % t)
            continue
        pdir = project_dir(settings, groups[0].target)
        adir = os.path.join(pdir, "analysis")
        frames = [f for g in groups for f in g.frames]
        log("Vermesse %d Frames von %s …" % (len(frames), groups[0].target))
        analyze.analyze_group(frames, adir, settings.analysis_workers,
                              progress=lambda i, n, err: (i % 10 == 0 or i == n or err) and log("  %d/%d%s" % (i, n, ("  FEHLER " + str(err)) if err else "")))
        with open(os.path.join(pdir, "inventory.json"), "w", encoding="utf-8") as fh:
            json.dump(inv.summary(), fh, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(pdir, "calibration.json"), "w", encoding="utf-8") as fh:
            json.dump(calibcheck.public(calib), fh, indent=2, ensure_ascii=False, default=str)
        log("Erzeuge Bericht …")
        res = report_build.build(groups[0].target, groups, inv, calib, adir, settings.workdir, os.path.join(pdir, "report.html"))
        log("  " + res["html"])
        results.append(res)
    return results
