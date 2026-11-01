"""Ablauf für ein Ziel: Scan → Kalibrier-Prüfung → Vermessung → Bericht + Plan."""
from __future__ import annotations

import json
import os
import re

from . import analyze, calibcheck, discover, logbook
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
    import time
    logbook.setup(settings.workdir)
    progress = progress or (lambda **kw: None)
    roots = [os.path.abspath(r) for r in roots]
    t_all = time.time()
    timings = {}
    log("Scanne %d Ordner …" % len(roots))
    progress(stage="Ordner durchsuchen", step="scan", done=0, total=0)
    t0 = time.time()
    inv = scan(roots, settings, lambda i, n: progress(stage="Ordner durchsuchen", step="scan", done=i, total=n))
    timings["scan"] = round(time.time() - t0, 1)
    log("  %d Light-Gruppen, %d Kalibriersätze, %d unbekannt, %d Fehler" % (len(inv.lights), len(inv.calib), len(inv.unknown), len(inv.errors)))
    if not inv.lights:
        raise ValueError("Keine Light-Frames gefunden. Sind die richtigen Ordner gewählt?")
    log("Prüfe Kalibrierdaten …")
    t0 = time.time()
    calib = calibcheck.check_all(inv, progress=lambda i, n, f: (progress(stage="Flats prüfen", step="flats", done=i, total=n), log("  Flats %d/%d %s" % (i, n, f))))
    timings["flats"] = round(time.time() - t0, 1)
    # Gibt es ein Ziel schon als Projekt mit weiteren Quellordnern, werden diese mit einbezogen
    extra = []
    for t in ([target] if target else targets(inv)):
        old = sources(os.path.join(settings.workdir, "projects", safe_name(t))).get("roots") or []
        extra += [r for r in old if os.path.abspath(r) not in roots and r not in extra and os.path.isdir(r)]
    if extra:
        log("Projekt existiert bereits – bisherige Ordner werden mit einbezogen: " + ", ".join(extra))
        roots = roots + [os.path.abspath(r) for r in extra]
        inv = scan(roots, settings, lambda i, n: progress(stage="Ordner durchsuchen", step="scan", done=i, total=n))
        calib = calibcheck.check_all(inv, progress=lambda i, n, f: progress(stage="Flats prüfen", step="flats", done=i, total=n))
    results = []
    for t in ([target] if target else targets(inv)):
        t0 = time.time()
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
        t_meas = round(time.time() - t0, 1)
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
        facts = project_facts(pdir, plan, st)
        out.append(dict(name=d, title=m["name"], target=m["target"], description=m.get("description", ""),
                        hours=facts["hours_total"], filters=[f["name"] for f in facts["filters"]],
                        last_night=facts["last_night"], created=m.get("created"),
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
        f["hours"] += g.get("hours") or 0
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
    """Vorschaubild für die Projekt-Kachel: neuester Stack, sonst ein gutes Rohbild aus der Analyse."""
    for r in reversed(stacks(pdir)):
        for o in r.get("outputs", []):
            if o.get("preview") and os.path.exists(o["preview"]):
                return o["preview"]
    out = os.path.join(pdir, "thumb.jpg")
    mcsv = os.path.join(pdir, "analysis", "metrics.csv")
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
        return None


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
    _write(os.path.join(pdir, "plan.json"), plan)
    return plan


def _apply_calib(run: dict, kind: str, choice, cands: dict):
    setkey = CALIB_KINDS[kind]
    run.setdefault("calib_manual", {})
    if kind == "flats" and run.get("flat_groups"):
        run["flat_groups_auto"] = run.pop("flat_groups")     # eigene Wahl gilt für alle Nächte
    if choice == "none":
        run[kind], run[setkey] = [], None
        run["calib_manual"][kind] = "keine"
    elif isinstance(choice, dict) and choice.get("folder"):
        paths = calib_folder_files(choice["folder"])
        run[kind], run[setkey] = paths, choice["folder"]
        run["calib_manual"][kind] = "eigener Ordner"
        if kind == "flats":
            run["flat_ok"], run["flat_sky"], run["flat_session"], run["flat_reasons"] = True, False, None, ["eigener Ordner"]
    elif isinstance(choice, str) and choice in cands:
        c = next((x for x in run.get("calib_choices", {}).get(kind, []) if x["id"] == choice), None)
        run[kind] = list(cands[choice])
        run[setkey] = c["folder"] if c else os.path.dirname(cands[choice][0]) if cands[choice] else None
        run["calib_manual"][kind] = "gewählt"
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
