"""Vorhandene Stacks übernehmen: Ergebnisse aus b2stacker 0.10 oder direkt aus PixInsight/WBPP.

Was übernommen werden kann:
- ein Ordner eines alten 0.10-Projekts (mit ``stacks.json``): alle Stacks samt Datum, Belichtung und Notiz
- ein WBPP-Ausgabeordner oder ein Ordner mit Ergebnissen: alle ``masterLight*``-Dateien darin (bis 3 Ebenen tief)
- einzelne Bilddateien (XISF/FITS/TIFF) – jede wird ein eigener Stack

Die Dateien bleiben, wo sie sind, und gehören nicht b2stacker (``owned = 0``): Löschen eines übernommenen
Stacks entfernt nur den Eintrag, nie die Datei. Vorschau und Qualitätswerte rechnet danach ``stackview`` aus.
"""
from __future__ import annotations

import datetime
import json
import os
import re

from . import db as dbm

IMG_EXT = (".xisf", ".fits", ".fit", ".fts", ".tif", ".tiff")
MAX_DEPTH = 3
_FILTER_RX = re.compile(r"FILTER-([A-Za-z0-9+]+)", re.I)


def _is_image(p: str) -> bool:
    return p.lower().endswith(IMG_EXT)


def _masters_in(folder: str) -> list[str]:
    out = []
    base = folder.rstrip("\\/")
    for root, dirs, files in os.walk(folder):
        depth = root[len(base):].count(os.sep)
        if depth >= MAX_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d.lower() not in ("calibrated", "debayered", "registered", "cache", "logs",
                                                        "temp", "normalized", "measured")]
        for f in files:
            low = f.lower()
            if _is_image(f) and low.startswith("masterlight") and "autocrop" not in low:
                out.append(os.path.join(root, f))
    return sorted(out)


def _header(path: str) -> dict:
    try:
        from .fitsmeta import read_header
        return {str(k).upper(): v for k, v in dict(read_header(path)).items()}
    except Exception:  # noqa: BLE001 – TIFF oder unlesbar: dann eben ohne Kopfdaten
        return {}


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _when(h: dict, path: str) -> str:
    for k in ("DATE", "DATE-END", "DATE-OBS"):
        v = h.get(k)
        if v:
            try:
                return datetime.datetime.fromisoformat(str(v).strip().replace("Z", "")[:19]).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
    t = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(path: str) -> dict:
    """Lauf-Name, Frames, Belichtung und Datum eines Masters – aus dem Header, sonst aus dem Dateinamen."""
    h = _header(path)
    name = os.path.basename(path)
    m = _FILTER_RX.search(name)
    label = str(h.get("FILTER") or (m.group(1) if m else "") or "").strip() or os.path.splitext(name)[0][:40]
    n = int(_num(h.get("NCOMBINE")) or 0) or None
    exp = _num(h.get("EXPTIME")) or _num(h.get("EXPOSURE"))
    total = (n * exp) if (n and exp) else None
    return dict(label=label, frames=n, exposure_s=total, created_at=_when(h, path))


def _legacy_records(folder: str) -> list[dict]:
    """Stacks aus einem 0.10-Projektordner (``stacks.json``)."""
    p = os.path.join(folder, "stacks.json")
    try:
        with open(p, encoding="utf-8") as fh:
            lst = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for r in lst if isinstance(lst, list) else []:
        if r.get("removed"):
            continue
        for o in r.get("outputs") or []:
            f = o.get("file")
            if not f:
                continue
            f = f if os.path.isabs(f) else os.path.normpath(os.path.join(folder, f))
            if not _is_image(f):
                continue
            out.append(dict(path=f, label=o.get("name") or "", note=r.get("note") or "",
                            created_at=r.get("date") or None, hours=_num(r.get("hours")),
                            frames=r.get("frames") if isinstance(r.get("frames"), int) else None))
    return out


def collect(paths: list[str]) -> list[dict]:
    """Ausgewählte Dateien/Ordner → Kandidaten [{path, label?, note?, created_at?, hours?, frames?}]"""
    seen, out = set(), []

    def add(c):
        k = os.path.normcase(os.path.abspath(c["path"]))
        if k not in seen:
            seen.add(k)
            out.append(c)
    for p in paths or []:
        p = (p or "").strip().strip('"')
        if not p:
            continue
        if os.path.isdir(p):
            legacy = _legacy_records(p)
            for c in legacy:
                add(c)
            if not legacy:
                for f in _masters_in(p):
                    add(dict(path=f))
        elif os.path.isfile(p) and _is_image(p):
            add(dict(path=p))
    return out


def import_stacks(db: dbm.Database, pid: int, paths: list[str]) -> dict:
    """Kandidaten als fertige Stacks eintragen. → {added: [stack_id], skipped: [{path, why}], found: n}"""
    cands = collect(paths)
    added, skipped = [], []
    known = {os.path.normcase(r[0]) for r in db.query(
        "SELECT a.path FROM assets a WHERE a.project_id = ? AND a.stack_id IS NOT NULL", (pid,))}
    for c in cands:
        path = c["path"]
        if os.path.normcase(path) in known:
            skipped.append(dict(path=path, why="schon übernommen"))
            continue
        if not os.path.isfile(path):
            skipped.append(dict(path=path, why="Datei fehlt"))
            continue
        d = describe(path)
        label = c.get("label") or d["label"]
        created = c.get("created_at") or d["created_at"]
        exp = (c["hours"] * 3600) if c.get("hours") else d["exposure_s"]
        frames = c.get("frames") or d["frames"]
        name = "%s %s (übernommen)" % (created[:10], label)
        now = dbm.now()
        with db.transaction():
            cur = db.execute(
                "INSERT INTO stacks (project_id, name, folder, status, created_at, finished_at, run_label, frames, "
                "exposure_s, nights, settings_json, note, size_bytes) VALUES (?, ?, ?, 'done', ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (pid, name, os.path.dirname(path), created, created, label, frames, exp,
                 json.dumps(dict(origin="import", imported=True, source=path)), c.get("note") or "",
                 os.path.getsize(path)))
            sid = int(cur.lastrowid)
            db.execute("INSERT INTO assets (project_id, stack_id, path, size, added_at, owned) VALUES (?, ?, ?, ?, ?, 0) "
                       "ON CONFLICT (project_id, path) DO UPDATE SET stack_id = excluded.stack_id, hidden = 0",
                       (pid, sid, path, os.path.getsize(path), now))
            db.execute("INSERT INTO events (at, project_id, kind, data_json) VALUES (?, ?, 'stack_imported', ?)",
                       (now, pid, json.dumps(dict(stack=sid, name=name, path=path), ensure_ascii=False)))
        known.add(os.path.normcase(path))
        added.append(sid)
    return dict(added=added, skipped=skipped, found=len(cands))
