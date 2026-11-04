"""Schritt 2: Header lesen – Typ, Filter, Kamera, Gain, Belichtung … (nur der Kopf der Datei).

Nutzt den erprobten Header-Übersetzer ``fitsmeta`` (NINA, ASIAIR, Ekos, SharpCap …). Das vollständige
``FrameMeta`` landet als JSON in ``files.meta_json``, die wichtigsten Felder zusätzlich als Spalten.
Fehler (kaputte Datei, unbekanntes Format) werden pro Datei gespeichert und stoppen nichts.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from .. import db as dbm
from .. import fitsmeta as fm

HEADER_VERSION = 1            # erhöhen, wenn sich fitsmeta so ändert, dass alte Ergebnisse falsch sind

_COLUMNS = ("type", "is_master", "source", "object", "filter", "filter_kind", "exposure", "gain", "offset",
            "readout", "binning", "width", "height", "bayer", "ccd_temp", "date_obs", "night")


def _read(path: str):
    try:
        return fm.read_meta(path), None
    except Exception as e:  # noqa: BLE001 – jede Datei für sich
        return None, fm.friendly_error(e) if hasattr(fm, "friendly_error") else str(e)


def meta_from_json(text: str | None) -> fm.FrameMeta | None:
    if not text:
        return None
    d = json.loads(text)
    fields = {f for f in fm.FrameMeta.__dataclass_fields__}
    return fm.FrameMeta(**{k: v for k, v in d.items() if k in fields})


def _setup_id(db: dbm.Database, meta: fm.FrameMeta) -> int | None:
    cam, tel = fm.camera_norm(meta.camera) or "", (meta.telescope or "").strip()
    if not cam and not tel:
        return None
    row = db.one("SELECT id FROM setups WHERE telescope = ? AND camera = ?", (tel, cam))
    if row:
        if meta.focal_length or meta.pixel_size:
            db.execute("UPDATE setups SET focal_length = COALESCE(focal_length, ?), pixel_size = COALESCE(pixel_size, ?) "
                       "WHERE id = ?", (meta.focal_length, meta.pixel_size, row[0]))
        return int(row[0])
    cur = db.execute("INSERT INTO setups (telescope, camera, focal_length, pixel_size) VALUES (?, ?, ?, ?)",
                     (tel, cam, meta.focal_length, meta.pixel_size))
    return int(cur.lastrowid)


def store_meta(db: dbm.Database, fid: int, meta: fm.FrameMeta, excluded: bool) -> None:
    meta.excluded = bool(excluded)
    vals = [int(bool(meta.is_master)) if c == "is_master" else getattr(meta, c) for c in _COLUMNS]
    sets = ", ".join('"%s" = ?' % c for c in _COLUMNS)
    db.execute("UPDATE files SET %s, header_version = ?, header_error = NULL, setup_id = ?, meta_json = ? WHERE id = ?"
               % sets, (*vals, HEADER_VERSION, _setup_id(db, meta), json.dumps(meta.to_dict(), default=str), fid))


def read_headers(db: dbm.Database, pid: int, progress=None, cancel=None, workers: int = 8) -> dict:
    rows = db.query(
        "SELECT f.id, f.path, f.excluded_dir FROM files f JOIN project_files pf ON pf.file_id = f.id "
        "WHERE pf.project_id = ? AND f.missing_since IS NULL "
        "AND (f.header_version IS NULL OR f.header_version < ?) ORDER BY f.path", (pid, HEADER_VERSION))
    res = dict(read=0, errors=0)
    if not rows:
        return res
    if progress:
        progress(step="Header lesen", done=0, total=len(rows))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        batch = []
        for i, (row, (meta, err)) in enumerate(zip(rows, ex.map(lambda r: _read(r["path"]), rows)), 1):
            batch.append((row, meta, err))
            if len(batch) >= 200 or i == len(rows):
                with db.transaction():
                    for r, m, e in batch:
                        if m is None:
                            db.execute("UPDATE files SET header_version = ?, header_error = ?, type = 'unknown' "
                                       "WHERE id = ?", (HEADER_VERSION, e or "unlesbar", r["id"]))
                            res["errors"] += 1
                        else:
                            store_meta(db, int(r["id"]), m, bool(r["excluded_dir"]))
                            res["read"] += 1
                batch = []
                if progress:
                    progress(done=i, message=os.path.basename(row["path"]))
            if cancel is not None and cancel():
                break
    return res
