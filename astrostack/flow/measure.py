"""Schritt 3: Vermessen – jede Datei genau einmal lesen: Prüfsumme, Messwerte und Thumbnail.

- Lights: Sterne, FWHM, Exzentrizität, Hintergrund, Rauschen … (``analyze``), Thumbnail für die Kachelansicht.
- Flats: Kanal-Helligkeit, Farbabfall, Vignettierung (``calibcheck``).
- Bias/Darks: nur die Prüfsumme (für die Master-Schlüssel).

Die Prüfsumme findet verschobene Dateien wieder: Fehlt eine Datei mit derselben Prüfsumme, übernimmt die
neue ihre Entscheidungen und Stack-Zugehörigkeit (Konzept Abschnitt 3, „robust gegen Unordnung“).
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from .. import db as dbm
from .headers import meta_from_json

MEASURE_VERSION = 1           # erhöhen, wenn sich analyze/calibcheck so ändert, dass alte Messwerte falsch sind
THUMBS = "thumbs"


def _work(item):
    """Im eigenen Prozess: Datei einmal lesen, Prüfsumme + Messung. → (id, ergebnis, fehler)"""
    fid, path, kind, meta_json = item
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        sha = hashlib.sha256(data).hexdigest()
        out = dict(checksum=sha, size=len(data))
        meta = meta_from_json(meta_json)
        if kind == "light" and meta is not None:
            from .. import analyze, imageio
            row, thumb = analyze._measure_raw(meta, imageio.load(path, data))
            out.update(row=row, thumb=thumb.astype(np.float16))
        elif kind == "flat" and meta is not None and not meta.is_master:
            from .. import calibcheck
            out["flat"] = calibcheck.flat_frame_stats(path, meta.bayer, data)
        return fid, out, None
    except Exception as e:  # noqa: BLE001
        from .. import fitsmeta as fm
        return fid, None, fm.friendly_error(e) if hasattr(fm, "friendly_error") else str(e)


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _relink_moved(db: dbm.Database, fid: int, sha: str) -> int:
    """Gibt es eine fehlende Datei mit derselben Prüfsumme, übernimmt ``fid`` ihre Entscheidungen. → Anzahl"""
    olds = db.query("SELECT id FROM files WHERE checksum = ? AND id != ? AND missing_since IS NOT NULL", (sha, fid))
    for o in olds:
        oid = int(o["id"])
        db.execute("INSERT OR IGNORE INTO file_status (project_id, file_id, status, origin, reason, decided_at) "
                   "SELECT project_id, ?, status, origin, reason, decided_at FROM file_status WHERE file_id = ?",
                   (fid, oid))
        db.execute("INSERT OR IGNORE INTO stack_files (stack_id, file_id, weight) "
                   "SELECT stack_id, ?, weight FROM stack_files WHERE file_id = ?", (fid, oid))
        db.execute("UPDATE stacks SET reference_file_id = ? WHERE reference_file_id = ?", (fid, oid))
        db.execute("DELETE FROM files WHERE id = ?", (oid,))
    return len(olds)


def _store(db: dbm.Database, thumbs_dir: str, fid: int, out: dict, now: str) -> int:
    sha = out["checksum"]
    db.execute("UPDATE files SET checksum = ? WHERE id = ?", (sha, fid))
    moved = _relink_moved(db, fid, sha)
    row, thumb_name = out.get("row"), None
    if out.get("thumb") is not None:
        thumb_name = sha[:20] + ".npy"
        p = os.path.join(thumbs_dir, thumb_name)
        if not os.path.exists(p):
            tmp = p + ".tmp"
            with open(tmp, "wb") as fh:
                np.save(fh, out["thumb"])
            os.replace(tmp, p)
    data = row if row is not None else out.get("flat")
    hot = None
    if row:
        hot = row.get("hotpixels")
    db.execute(
        "INSERT INTO frame_metrics (file_id, version, measured_at, nstars, fwhm, ecc, bg, noise, snr, hotpixels, thumb, "
        "data_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (file_id) DO UPDATE SET "
        "version = excluded.version, measured_at = excluded.measured_at, nstars = excluded.nstars, "
        "fwhm = excluded.fwhm, ecc = excluded.ecc, bg = excluded.bg, noise = excluded.noise, snr = excluded.snr, "
        "hotpixels = excluded.hotpixels, thumb = excluded.thumb, data_json = excluded.data_json",
        (fid, MEASURE_VERSION, now,
         _num((row or {}).get("nstars")), _num((row or {}).get("fwhm")), _num((row or {}).get("ecc")),
         _num((row or {}).get("bg")), _num((row or {}).get("noise")), _num((row or {}).get("snr_proxy")),
         _num(hot), thumb_name, json.dumps(data, default=str) if data is not None else None))
    return moved


def measure_files(db: dbm.Database, ws, pid: int, progress=None, cancel=None, workers: int = 2) -> dict:
    rows = db.query(
        "SELECT f.id, f.path, f.type, f.meta_json FROM files f JOIN project_files pf ON pf.file_id = f.id "
        "LEFT JOIN frame_metrics m ON m.file_id = f.id "
        "WHERE pf.project_id = ? AND f.missing_since IS NULL AND f.header_version IS NOT NULL "
        "AND f.header_error IS NULL AND (m.file_id IS NULL OR m.version < ? OR f.checksum IS NULL) "
        "ORDER BY CASE f.type WHEN 'light' THEN 0 WHEN 'flat' THEN 1 ELSE 2 END, f.date_obs, f.path",
        (pid, MEASURE_VERSION))
    res = dict(measured=0, errors=0, moved=0)
    if not rows:
        return res
    thumbs_dir = os.path.join(ws.previews, THUMBS)
    os.makedirs(thumbs_dir, exist_ok=True)
    if progress:
        progress(step="Frames vermessen", done=0, total=len(rows))
    items = [(int(r["id"]), r["path"], r["type"], r["meta_json"]) for r in rows]
    workers = max(1, int(workers))
    ex = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        results = ex.map(_work, items) if ex else map(_work, items)
        for i, (fid, out, err) in enumerate(results, 1):
            now = dbm.now()
            with db.transaction():
                if out is None:
                    db.execute("UPDATE files SET header_error = ? WHERE id = ?", ("Vermessen: " + (err or "Fehler"), fid))
                    res["errors"] += 1
                else:
                    res["moved"] += _store(db, thumbs_dir, fid, out, now)
                    res["measured"] += 1
            if progress:
                progress(done=i, message=os.path.basename(items[i - 1][1]))
            if cancel is not None and cancel():
                break
    finally:
        if ex:
            ex.shutdown(wait=True, cancel_futures=True)
    return res
