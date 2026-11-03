"""Authoritative frame selection per project.

Analysis captures every scanned light and calibration frame in a local context
once. Toggling a frame afterwards rebuilds the plan from that context, the
measurement cache and the user edits only: no scan, header, pixel or NAS read.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time

CONTEXT_FILE = "selection_context.json"
CONTEXT_VERSION = 1

KIND_TYPE = {"flats": "flat", "bias": "bias", "darks": "dark", "darkflats": "darkflat"}
TYPE_KIND = {v: k for k, v in KIND_TYPE.items()}
ERROR_STATUSES = ("error", "rejected", "suggest")


class SelectionUnavailable(RuntimeError):
    """The project has no local artifacts that allow rebuilding a selection."""


def _read(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    tmp = "%s.%s.%s.%s.tmp" % (path, os.getpid(), threading.get_ident(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_context(pdir: str) -> dict | None:
    ctx = _read(os.path.join(pdir, CONTEXT_FILE), None)
    return ctx if isinstance(ctx, dict) and ctx.get("version") == CONTEXT_VERSION else None


def _fingerprint(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime_ns)]


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------ capture (during analysis)

def capture(pdir: str, groups, calib_sets, plan_base: dict | None = None, templates: list | None = None,
            errors=None) -> dict:
    """Store the unfiltered scan result so later toggles never need the sources again."""
    prev = load_context(pdir)
    lights, fingerprints = [], {}
    for g in groups:
        for f in g.frames:
            lights.append(dict(path=f.path, type="light", filter=g.filter, filter_kind=g.filter_kind,
                               is_osc=bool(g.is_osc), settings=list(g.settings), target=g.target,
                               exposure=f.exposure, night=f.night, excluded=bool(f.excluded),
                               bayer=getattr(f, "bayer", None), rawWidth=getattr(f, "width", None),
                               rawHeight=getattr(f, "height", None)))
            fp = _fingerprint(f.path)
            if fp:
                fingerprints[f.path] = fp
    calib = []
    for c in calib_sets:
        frames = []
        for f in list(c.frames) + list(getattr(c, "hidden", []) or []):
            frames.append(dict(path=f.path, type=c.type, filter=c.filter, night=getattr(f, "night", None),
                               exposure=getattr(f, "exposure", None), excluded=bool(getattr(f, "excluded", False)),
                               bayer=getattr(f, "bayer", None), rawWidth=getattr(f, "width", None),
                               rawHeight=getattr(f, "height", None)))
            fp = _fingerprint(f.path)
            if fp:
                fingerprints[f.path] = fp
        calib.append(dict(id=c.id, type=c.type, filter=c.filter, folder=c.folder, exposure=c.exposure,
                          session=c.session, settings=list(c.settings), frames=frames))
    base = plan_base or {}
    ctx = dict(version=CONTEXT_VERSION, revision=int((prev or {}).get("revision") or 0) + 1,
               captured=_now(), target=base.get("target"), legacy=False, ready=True, has_membership=True,
               lights=lights, calib=calib, templates=list(templates or (prev or {}).get("templates") or []),
               run_paths={r["name"]: list(r.get("lights") or []) for r in base.get("runs", [])},
               errors=[dict(path=p, reason=str(r)) for p, r in (errors or [])],
               fingerprints=fingerprints)
    dropped = _reconcile_override_fingerprints(pdir, prev, fingerprints)
    if dropped:
        ctx["dropped_overrides"] = sorted(dropped)
    _write(os.path.join(pdir, CONTEXT_FILE), ctx)
    sync_index(pdir, ctx, base, _read(os.path.join(pdir, "overrides.json"), {}))
    return ctx


def _reconcile_override_fingerprints(pdir: str, prev: dict | None, fingerprints: dict) -> list:
    """Content changes invalidate manual choices; legacy choices without a fingerprint stay."""
    old_fp = (prev or {}).get("fingerprints") or {}
    if not old_fp:
        return []
    path = os.path.join(pdir, "overrides.json")
    ov = _read(path, {})
    dropped = [p for p in ov if p in old_fp and p in fingerprints and old_fp[p] != fingerprints[p]]
    if dropped:
        ov = {p: v for p, v in ov.items() if p not in dropped}
        _write(path, ov)
    return dropped


# ------------------------------------------------------------ plan templates

def templates_for(analysis_dir: str, groups, calib, workdir: str, target: str) -> list:
    """One run template per filter group, independent of the current selection."""
    from . import plan as plan_mod
    from .report import assess as assess_mod

    try:
        rows = assess_mod.load_rows(os.path.join(analysis_dir, "metrics.csv"))
    except OSError:
        rows = []
    for r in rows:
        r["status"] = "ok"                       # membership template, not a selection decision
    measured = {r["path"] for r in rows}
    unmeasured = {f.path for g in groups for f in g.frames} - measured
    matches = [m for m in (calib or {}).get("matches", [])
               if (m.get("group") or {}).get("target") == target]
    built = plan_mod.build_plan(target, groups, rows, [], matches, workdir, unmeasured=unmeasured)
    return built.get("runs", [])


# ------------------------------------------------------------ selection rules

def _error_paths(ctx: dict) -> set:
    return {e.get("path") for e in ctx.get("errors") or [] if e.get("path")}


def frame_selected(ctx: dict, row: dict, assessment: dict, overrides: dict) -> tuple[bool, str]:
    """Return membership decision and its source; errors can never be overridden in."""
    path = row.get("path")
    ov = overrides.get(path)
    source = "override" if ov in ("in", "out") else "auto"
    if row.get("type") == "unknown" or path in _error_paths(ctx):
        return False, source
    if ov == "out":
        return False, source
    if row.get("type") != "light":
        return (ov == "in") or not row.get("excluded"), source
    a = assessment.get(path)
    if isinstance(a, str):
        auto = a
    elif isinstance(a, dict):
        auto = a.get("auto", "ok")
    elif ctx.get("legacy"):
        auto = "rejected" if row.get("excluded") else "ok"
    else:
        auto = "error"                            # unmeasured light stays fail-closed
    if auto == "error":
        return False, source
    if ov == "in":
        return True, source
    return auto not in ERROR_STATUSES, source


def selection_state(pdir: str, ctx: dict, overrides: dict | None = None):
    assessment = _read(os.path.join(pdir, "analysis", "assessment.json"), {})
    ov = overrides if overrides is not None else _read(os.path.join(pdir, "overrides.json"), {})
    out = {}
    for row in ctx.get("lights", []) + [f for s in ctx.get("calib", []) for f in s.get("frames", [])]:
        out[row["path"]] = frame_selected(ctx, row, assessment, ov)
    for e in ctx.get("errors") or []:
        out[e.get("path")] = (False, "auto")
    return out


# ------------------------------------------------------------ plan rebuild (local only)

CALIB_KINDS = {"flats": "flat_set", "bias": "bias_set", "darks": "dark_set", "darkflats": "darkflat_set"}


def compute_plan(pdir: str, overrides: dict | None = None) -> tuple[dict, dict]:
    """Apply edits and the current selection to plan_base without scanning anything."""
    from . import project as project_mod

    ctx = dict(ensure_context(pdir))
    if not ctx.get("ready"):
        raise SelectionUnavailable("Für dieses Projekt fehlt eine lokale Auswahl – bitte einmal analysieren.")
    ctx["revision"] = int(ctx.get("revision") or 0) + 1
    ov = overrides if overrides is not None else _read(os.path.join(pdir, "overrides.json"), {})
    plan = project_mod.apply_edits(pdir, overrides=ov, write=False)
    if not isinstance(plan, dict):
        raise SelectionUnavailable("Kein Plan vorhanden")
    return reconcile(pdir, plan, ctx, ov), ctx


def rebuild_plan(pdir: str, overrides: dict | None = None) -> dict:
    """Compute, persist and index one selection revision."""
    plan, ctx = compute_plan(pdir, overrides)
    _write(os.path.join(pdir, "plan.json"), plan)
    commit(pdir, plan, ctx, overrides if overrides is not None else _read(os.path.join(pdir, "overrides.json"), {}))
    return plan


def commit(pdir: str, plan: dict, ctx: dict, overrides: dict) -> dict:
    """Persist the revision that was already written into the plan and index the same selection."""
    _write(os.path.join(pdir, CONTEXT_FILE), ctx)
    sync_index(pdir, ctx, plan, overrides)
    return ctx


def reconcile(pdir: str, plan: dict, ctx: dict, overrides: dict) -> dict:
    from . import project as project_mod

    state = selection_state(pdir, ctx, overrides)
    lights = {m["path"]: m for m in ctx.get("lights") or []}
    templates = [t for t in ctx.get("templates") or [] if t.get("name")]
    template_index = {t["name"]: i for i, t in enumerate(templates)}
    edits = (_read(os.path.join(pdir, "plan_edits.json"), {}) or {}).get("runs") or {}
    cands = _read(os.path.join(pdir, "calib_candidates.json"), {})
    existing = {r.get("name"): r for r in plan.get("runs", []) if r.get("name")}
    ordered: list[tuple[dict, list[str]]] = []
    seen = set()
    for t in templates:
        run = existing.get(t["name"])
        if run is None:                          # a fully deselected filter group comes back on demand
            run = dict(t)
            e = edits.get(t["name"]) or {}
            run.update({k: v for k, v in e.items() if k in ("enabled", "reference", "wbpp")})
            for kind, choice in (e.get("calib") or {}).items():
                project_mod._apply_calib(run, kind, choice, cands)
        ordered.append((run, _candidate_paths(ctx, run, t)))
        seen.add(run.get("name"))
    for r in plan.get("runs", []):               # legacy plans without captured templates
        if r.get("name") in seen:
            continue
        ordered.append((r, _candidate_paths(ctx, r, None)))
        seen.add(r.get("name"))

    runs = []
    for run, candidates in ordered:
        selected = [p for p in candidates if p in lights and state.get(p, (False, ""))[0]]
        if selected:
            _assign_lights(run, lights, selected)
        elif lights:
            continue
        # Without captured lights a legacy run has no membership domain; keep it for its calibration only.
        _assign_calib(run, ctx, overrides, state)
        run.setdefault("enabled", True)
        runs.append(run)
    runs.sort(key=lambda r: template_index.get(r.get("name"), len(template_index)))
    plan["runs"] = runs
    enabled = [r for r in runs if r.get("enabled", True)]
    plan["total_frames"] = sum(r.get("lightCount") or 0 for r in enabled)
    plan["planned_frames"] = sum(r.get("lightCount") or 0 for r in runs)
    plan["selection_revision"] = int(ctx.get("revision") or 0)
    plan.setdefault("options", {})
    return plan


def _candidate_paths(ctx: dict, run: dict, template: dict | None) -> list[str]:
    paths = list((ctx.get("run_paths") or {}).get(run.get("name")) or [])
    paths += list((template or {}).get("lights") or [])
    want_filter = run.get("filter")
    want_settings = tuple(run.get("settings_key") or [])
    for m in ctx.get("lights") or []:
        if m.get("filter") != want_filter:
            continue
        if want_settings and tuple(m.get("settings") or []) != want_settings:
            continue
        paths.append(m["path"])
    return list(dict.fromkeys(paths))


def _assign_lights(run: dict, lights: dict, selected: list[str]) -> None:
    rows = [lights[p] for p in selected]
    run["lights"] = list(selected)
    run["lightCount"] = len(rows)
    run["hours"] = round(sum((m.get("exposure") or 0) for m in rows) / 3600, 2)
    run["nights"] = sorted({m.get("night") for m in rows if m.get("night")})
    run["light_dirs"] = sorted({os.path.dirname(m["path"]) for m in rows})
    run.setdefault("filter", rows[0].get("filter"))


def _sets_by_key(ctx: dict) -> dict:
    return {(s.get("type"), s.get("folder")): s for s in ctx.get("calib") or []}


def _assign_calib(run: dict, ctx: dict, overrides: dict, state: dict) -> None:
    sets = _sets_by_key(ctx)
    assigned = dict(run.get("calib_assigned") or {})
    manual = run.get("calib_manual") or {}
    flat_groups = run.get("flat_groups") or []
    for kind, setkey in CALIB_KINDS.items():
        if manual.get(kind) == "keine":
            run[kind], run[setkey] = [], None
            assigned[kind] = None
            continue
        if kind == "flats" and flat_groups:
            for x in flat_groups:
                st = sets.get(("flat", x.get("flat_set")))
                if st:
                    x["flats"] = [f["path"] for f in st.get("frames", []) if state.get(f["path"], (False, ""))[0]]
                else:
                    x["flats"] = [p for p in x.get("flats") or [] if overrides.get(p) != "out"]
            allf = list(dict.fromkeys(p for x in flat_groups for p in x.get("flats") or []))
            run["flats"] = allf
            run["flat_set"] = " + ".join(sorted({x["flat_set"] for x in flat_groups if x.get("flat_set")})) or None
            continue
        folder = run.get(setkey) or assigned.get(kind)
        st = sets.get((KIND_TYPE[kind], folder)) if folder else None
        if st:
            paths = [f["path"] for f in st.get("frames", []) if state.get(f["path"], (False, ""))[0]]
            run[kind] = paths
            if paths:
                run[setkey] = folder
            else:                                # explicitly emptied set keeps its folder for traceability
                run[setkey] = folder
                assigned[kind] = folder
        else:
            run[kind] = [p for p in (run.get(kind) or []) if overrides.get(p) != "out"]
            if not run[kind]:
                run[setkey] = folder
                if folder:
                    assigned[kind] = folder
            elif folder:
                run[setkey] = folder
    run["calib_assigned"] = assigned


# ------------------------------------------------------------ index / visible rows

def _assigned(ctx: dict, plan: dict) -> tuple[set, dict]:
    """Assigned (type, folder) sets and paths that are not part of a captured set."""
    folders, paths = set(), {}
    for r in plan.get("runs", []):
        for kind, setkey in CALIB_KINDS.items():
            folder = r.get(setkey)
            if folder:
                folders.add((KIND_TYPE[kind], folder))
            remembered = (r.get("calib_assigned") or {}).get(kind)
            if remembered:
                folders.add((KIND_TYPE[kind], remembered))
            for p in r.get(kind) or []:
                paths.setdefault(p, KIND_TYPE[kind])
        for x in r.get("flat_groups") or []:
            if x.get("flat_set"):
                folders.add(("flat", x["flat_set"]))
            for p in x.get("flats") or []:
                paths.setdefault(p, "flat")
    return folders, paths


def _set_assigned(s: dict, folders: set, extra: dict) -> bool:
    return (s.get("type"), s.get("folder")) in folders or any(
        f.get("path") in extra for f in s.get("frames", []))


def visible_rows(ctx: dict, plan: dict) -> list[dict]:
    """All lights plus the calibration frames of the sets this project uses."""
    rows = [dict(m, type="light", excluded=bool(m.get("excluded"))) for m in ctx.get("lights") or []]
    folders, extra = _assigned(ctx, plan or {})
    known = set()
    for s in ctx.get("calib") or []:
        if ctx.get("has_membership") and not _set_assigned(s, folders, extra):
            continue
        for f in s.get("frames") or []:
            rows.append(dict(f, set_id=s.get("id"), set_type=s.get("type"), set_folder=s.get("folder"),
                             assigned=True))
            known.add(f["path"])
    for path, typ in extra.items():
        if path not in known:
            rows.append(dict(path=path, type=typ, filter=None, night=None, excluded=False, assigned=True,
                             set_id=os.path.dirname(path), set_type=typ, set_folder=os.path.dirname(path)))
    for e in ctx.get("errors") or []:
        rows.append(dict(path=e.get("path"), type="unknown", filter=None, night=None, excluded=True,
                         error=str(e.get("reason") or "Grund unbekannt")))
    return rows


def index_rows(ctx: dict, plan: dict) -> list[dict]:
    """Complete membership index; visibility is decided by visible_rows, not by dropping paths here."""
    rows = [dict(m, type="light", excluded=bool(m.get("excluded"))) for m in ctx.get("lights") or []]
    folders, extra = _assigned(ctx, plan or {})
    known = set()
    for s in ctx.get("calib") or []:
        assigned = _set_assigned(s, folders, extra)
        for f in s.get("frames") or []:
            rows.append(dict(f, set_id=s.get("id"), set_type=s.get("type"), set_folder=s.get("folder"),
                             assigned=assigned))
            known.add(f["path"])
    for path, typ in extra.items():
        if path not in known:
            rows.append(dict(path=path, type=typ, filter=None, night=None, excluded=False, assigned=True,
                             set_id=os.path.dirname(path), set_type=typ, set_folder=os.path.dirname(path)))
    for e in ctx.get("errors") or []:
        rows.append(dict(path=e.get("path"), type="unknown", filter=None, night=None, excluded=True,
                         error=str(e.get("reason") or "Grund unbekannt")))
    return rows


def sync_index(pdir: str, ctx: dict, plan: dict, overrides: dict | None = None) -> list[dict]:
    rows = index_rows(ctx, plan)
    os.makedirs(os.path.join(pdir, "analysis"), exist_ok=True)
    _write(os.path.join(pdir, "analysis", "frames.json"), rows)
    return rows


def plan_membership(plan: dict) -> tuple[dict, dict]:
    lights, calib = {}, {}
    for r in plan.get("runs", []):
        state = dict(name=r.get("name"), enabled=bool(r.get("enabled", True)))
        for p in r.get("lights") or []:
            lights.setdefault(p, []).append(state)
        for kind in CALIB_KINDS:
            for p in r.get(kind) or []:
                calib.setdefault(p, []).append(dict(state, kind=kind))
    return lights, calib


# ------------------------------------------------------------ migration from local artifacts

def ensure_context(pdir: str) -> dict:
    ctx = load_context(pdir)
    return ctx if ctx else _bootstrap(pdir)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _metrics_rows(pdir: str) -> dict:
    import csv
    try:
        with open(os.path.join(pdir, "analysis", "metrics.csv"), encoding="utf-8") as fh:
            return {r.get("path"): r for r in csv.DictReader(fh)}
    except OSError:
        return {}


def _bootstrap(pdir: str) -> dict:
    """Rebuild a selection context from local project files only; never scans sources."""
    plan = _read(os.path.join(pdir, "plan.json"), None) or _read(os.path.join(pdir, "plan_base.json"), {}) or {}
    frames = _read(os.path.join(pdir, "analysis", "frames.json"), []) or []
    metrics = _metrics_rows(pdir)
    light_rows = {r["path"]: r for r in frames if r.get("type") == "light" and r.get("path")}
    run_paths = {r["name"]: list(r.get("lights") or []) for r in plan.get("runs", []) if r.get("name")}
    plan_lights = [p for paths in run_paths.values() for p in paths]
    ordered = list(dict.fromkeys(plan_lights + list(light_rows) + list(metrics)))
    lights = []
    for p in ordered:
        m, row = metrics.get(p) or {}, light_rows.get(p) or {}
        lights.append(dict(path=p, type="light", filter=row.get("filter") or m.get("filter"),
                           filter_kind=None, is_osc=None, settings=[], target=plan.get("target"),
                           exposure=_num(m.get("exp")), night=row.get("night") or m.get("night"),
                           excluded=bool(row.get("excluded")), bayer=row.get("bayer"),
                           rawWidth=row.get("rawWidth"), rawHeight=row.get("rawHeight")))
    sets: dict = {}

    def add(kind: str, folder, path: str) -> None:
        typ = KIND_TYPE.get(kind, kind)
        key = (typ, folder or os.path.dirname(path))
        entry = sets.setdefault(key, dict(id=folder or os.path.dirname(path), type=typ, filter=None,
                                          folder=folder or os.path.dirname(path), exposure=None, session="",
                                          settings=[], frames=[]))
        if not any(f["path"] == path for f in entry["frames"]):
            src = next((r for r in frames if r.get("path") == path), {}) or {}
            entry["frames"].append(dict(path=path, type=typ, filter=src.get("filter"), night=src.get("night"),
                                        exposure=None, excluded=bool(src.get("excluded")),
                                        bayer=src.get("bayer"), rawWidth=src.get("rawWidth"),
                                        rawHeight=src.get("rawHeight")))

    for r in plan.get("runs", []):
        for kind, setkey in CALIB_KINDS.items():
            for p in r.get(kind) or []:
                add(kind, r.get(setkey), p)
        for x in r.get("flat_groups") or []:
            for p in x.get("flats") or []:
                add("flats", x.get("flat_set"), p)
    for src in frames:
        if src.get("type") not in (None, "light", "unknown") and src.get("path"):
            add(TYPE_KIND.get(src["type"], src["type"]), None, src["path"])
    errors = [dict(path=r.get("path"), reason=r.get("error")) for r in frames if r.get("type") == "unknown"]
    has_membership = any(run_paths.values())
    ready = bool(plan.get("runs")) and bool(lights or any(s["frames"] for s in sets.values())) and has_membership
    ctx = dict(version=CONTEXT_VERSION, revision=0, captured=_now(), target=plan.get("target"),
               legacy=True, ready=ready, has_membership=has_membership,
               lights=lights, calib=list(sets.values()),
               templates=[dict(r) for r in plan.get("runs", []) if r.get("name")],
               run_paths=run_paths, errors=errors, fingerprints={})
    try:
        _write(os.path.join(pdir, CONTEXT_FILE), ctx)
    except OSError:
        pass
    return ctx
