"""Frame-Vermessung in einem Durchgang: Qualität (FWHM, Exzentrizität, Sterne, Hintergrund, Rauschen,
Gradient, Farbverhältnisse) + Rig-Daten (Sternform im 3×3-Feld, Richtungskohärenz, radial/tangential)
+ Mond (Phase, Höhe) + Vorschau-Thumbnail.

FWHM wird in **nativen Pixeln** gespeichert (bei Farbkameras aus Superpixeln ×2 umgerechnet).
Sternformen werden mit lokalem Hintergrund pro Stern gemessen (Nebel verfälscht sonst die Größe).
"""
from __future__ import annotations

import csv
import json
import os
import warnings
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from scipy import ndimage as ndi

from . import catalog, imageio

warnings.filterwarnings("ignore")

COLS = ["path", "night", "excluded", "filter", "filter_kind", "date_obs", "exp", "gain", "offset", "readout", "temp",
        "settemp", "alt", "airmass", "hum", "ambtemp", "dewpoint", "wind", "focpos", "foctemp", "rot", "pier",
        "pixscale", "hfr_name", "moon_illum", "moon_alt",
        "bg", "noise", "snr_proxy", "nstars", "fwhm", "ecc", "grad", "r_g", "b_g", "p001", "sat_frac",
        "fwhm_c", "fwhm_corner", "tiles_fwhm", "tiles_ecc", "tiles_ang", "tiles_n", "coherence", "coh_angle", "radial",
        "file_sig", "sha256", "metrics_ver"]
# Only these pixel-derived columns are cached; context columns are always rebuilt from the current meta.
METRIC_COLS = ["bg", "noise", "snr_proxy", "nstars", "fwhm", "ecc", "grad", "r_g", "b_g", "p001", "sat_frac",
               "fwhm_c", "fwhm_corner", "tiles_fwhm", "tiles_ecc", "tiles_ang", "tiles_n", "coherence", "coh_angle",
               "radial", "moon_illum", "moon_alt"]
ERRORS_FILE = "measure_errors.json"


def moon_info(meta) -> tuple:
    try:
        from astropy.coordinates import AltAz, EarthLocation, get_body
        from astropy.time import Time
        from astropy.utils import iers
        import astropy.units as u
        iers.conf.auto_download = False
        if not (meta.date_obs and meta.site_lat is not None and meta.site_lon is not None):
            return None, None
        t = Time(meta.date_obs[:23])
        loc = EarthLocation(lat=meta.site_lat * u.deg, lon=meta.site_lon * u.deg, height=(meta.site_elev or 0) * u.m)
        moon, sun = get_body("moon", t, loc), get_body("sun", t, loc)
        illum = (1 - np.cos(sun.separation(moon).rad)) / 2
        alt = moon.transform_to(AltAz(obstime=t, location=loc)).alt.deg
        return round(float(illum) * 100, 1), round(float(alt), 1)
    except Exception:
        return None, None


def _stars(L, bg, noise, maxn=1500):
    sm = ndi.gaussian_filter(L - bg, 1.0)
    mx = ndi.maximum_filter(sm, size=9)
    ys, xs = np.nonzero((sm == mx) & (sm > 8 * noise))
    hh, ww = L.shape
    m = (ys > 10) & (xs > 10) & (ys < hh - 10) & (xs < ww - 10)
    ys, xs = ys[m], xs[m]
    nstars = len(ys)
    order = np.argsort(sm[ys, xs])[::-1][:maxn]
    yy, xx = np.mgrid[-7:8, -7:8]
    out = []
    for k in order:
        y, x = ys[k], xs[k]
        raw = L[y - 7:y + 8, x - 7:x + 8]
        if raw.max() > 60000:
            continue
        border = np.concatenate([raw[0], raw[-1], raw[1:-1, 0], raw[1:-1, -1]])
        lbg = np.median(border)
        lsd = 1.4826 * np.median(np.abs(border - lbg))
        thr = max(lsd, noise * 0.5)
        cut = raw - lbg
        if cut.max() < 15 * thr:
            continue
        c = np.where(cut > 2 * thr, cut, 0)
        s = c.sum()
        if s <= 0:
            continue
        cx, cy = (c * xx).sum() / s, (c * yy).sum() / s
        sxx = (c * (xx - cx) ** 2).sum() / s
        syy = (c * (yy - cy) ** 2).sum() / s
        sxy = (c * (xx - cx) * (yy - cy)).sum() / s
        tr = sxx + syy
        det = np.sqrt(max(((sxx - syy) / 2) ** 2 + sxy ** 2, 0))
        l1, l2 = tr / 2 + det, tr / 2 - det
        if l2 <= 0.1:
            continue
        out.append((y, x, 2.3548 * np.sqrt((l1 + l2) / 2), np.sqrt(1 - l2 / l1), 0.5 * np.arctan2(2 * sxy, sxx - syy)))
    return nstars, (np.array(out) if out else np.zeros((0, 5)))


def context_row(meta) -> dict:
    """Context columns; always rebuilt, never cached (folder/project dependent)."""
    return dict(
        path=meta.path, night=meta.night, excluded=int(meta.excluded), filter=meta.filter, filter_kind=meta.filter_kind,
        date_obs=meta.date_obs, exp=meta.exposure, gain=meta.gain, offset=meta.offset, readout=meta.readout,
        temp=meta.ccd_temp, settemp=meta.set_temp, alt=meta.alt, airmass=meta.airmass, hum=meta.humidity,
        ambtemp=meta.ambient_temp, dewpoint=meta.dew_point, wind=meta.wind, focpos=meta.focus_pos, foctemp=meta.focus_temp,
        rot=meta.rotator, pier=meta.pier_side, pixscale=meta.pixel_scale, hfr_name=meta.hfr_from_name)


def measure(meta) -> tuple[dict, np.ndarray]:
    """Ein Light vermessen. → (Zeile, Thumbnail 1/8-Auflösung der Luminanz)"""
    return _measure_raw(meta, imageio.load(meta.path))


def _measure_raw(meta, raw) -> tuple[dict, np.ndarray]:
    """Measure from already loaded pixels so preview rendering can share the same read."""
    if raw.ndim == 3:   # bereits debayert
        raw = raw.mean(axis=0)
    p001 = float(np.percentile(raw[::7, ::7], 0.01))
    sat = float((raw[::4, ::4] >= 65000).mean())
    ch = imageio.split_cfa(raw, meta.bayer)
    scale_px = 2.0 if "G" in ch else 1.0
    if "G" in ch:
        R, G, B = ch["R"], ch["G"], ch["B"]
        L = (R + 2 * G + B) / 4
        r_g = float(np.median(R) / max(np.median(G), 1e-6))
        b_g = float(np.median(B) / max(np.median(G), 1e-6))
    else:
        L, r_g, b_g = ch["L"], None, None
    bg = float(np.median(L))
    noise = float(1.4826 * np.median(np.abs(L[::4, ::4] - bg)))
    hh, ww = L.shape
    tiles = np.array([[np.median(L[i * hh // 5:(i + 1) * hh // 5:2, j * ww // 5:(j + 1) * ww // 5:2]) for j in range(5)] for i in range(5)])
    nstars, S = _stars(L, bg, noise)
    cy, cx = hh / 2, ww / 2
    tf, te, ta, tn = [], [], [], []
    for i in range(3):
        for j in range(3):
            T = S[(S[:, 0] >= i * hh / 3) & (S[:, 0] < (i + 1) * hh / 3) & (S[:, 1] >= j * ww / 3) & (S[:, 1] < (j + 1) * ww / 3)] if len(S) else S
            tn.append(int(len(T)))
            if len(T) >= 8:
                tf.append(round(float(np.median(T[:, 2])) * scale_px, 3))
                te.append(round(float(np.median(T[:, 3])), 3))
                z = np.sum(T[:, 3] * np.exp(2j * T[:, 4])) / max(np.sum(T[:, 3]), 1e-9)
                ta.append(round(float(np.degrees(np.angle(z) / 2)), 1))
            else:
                tf.append(None); te.append(None); ta.append(None)
    coh = cang = rad = None
    if len(S) >= 30:
        w = S[:, 3]
        z = np.sum(w * np.exp(2j * S[:, 4])) / np.sum(w)
        coh, cang = round(float(abs(z)), 3), round(float(np.degrees(np.angle(z) / 2)), 1)
        r = np.hypot(S[:, 0] - cy, S[:, 1] - cx)
        far = r > 0.5 * np.hypot(cy, cx)
        if far.sum() >= 20:
            phi = np.arctan2(S[far, 0] - cy, S[far, 1] - cx)
            rad = round(float(np.sum(w[far] * np.cos(2 * (S[far, 4] - phi))) / np.sum(w[far])), 3)
    corners = [tf[k] for k in (0, 2, 6, 8) if tf[k]]
    mi, ma = moon_info(meta)
    row = dict(
        context_row(meta),
        moon_illum=mi, moon_alt=ma,
        bg=round(bg, 2), noise=round(noise, 3), snr_proxy=round(bg / max(noise, 1e-3), 2), nstars=nstars,
        fwhm=round(float(np.median(S[:, 2])) * scale_px, 3) if len(S) else None,
        ecc=round(float(np.median(S[:, 3])), 3) if len(S) else None,
        grad=round(float((tiles.max() - tiles.min()) / max(noise, 1e-3)), 2),
        r_g=None if r_g is None else round(r_g, 4), b_g=None if b_g is None else round(b_g, 4),
        p001=round(p001, 1), sat_frac=round(sat, 6),
        fwhm_c=tf[4], fwhm_corner=round(float(np.median(corners)), 3) if corners else None,
        tiles_fwhm=json.dumps(tf), tiles_ecc=json.dumps(te), tiles_ang=json.dumps(ta), tiles_n=json.dumps(tn),
        coherence=coh, coh_angle=cang, radial=rad)
    t = L[: hh // 8 * 8 // 1, : ww // 8 * 8]
    step = 8 if "G" in ch else 16
    t = L[: hh // step * step, : ww // step * step].reshape(hh // step, step, ww // step, step).mean(axis=(1, 3)).astype(np.float32)
    return row, t


def thumb_name(path: str) -> str:
    import hashlib
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16] + ".npy"


def _work(item):
    from . import previews
    meta, preview = item
    try:
        raw = imageio.load(meta.path)
        row, thumb = _measure_raw(meta, raw)
    except Exception as e:
        return None, (meta.path, str(e))
    record = None
    if preview:
        sha = preview["sha"]
        try:
            if not sha:
                sha = catalog.sha256_file(meta.path)
            record = previews.store_from_raw(meta.path, meta.bayer, raw, sha, preview["sig"],
                                             preview["root"])
        except Exception as e:  # noqa: BLE001 – preview trouble must never discard the measurement
            record = previews.failure_record(sha, meta.bayer, preview["sig"], e)
    return (row, thumb, record), None


def _read_csv_rows(p: str) -> dict:
    """Existing metrics.csv keyed by path; a broken file counts as empty so frames are re-measured."""
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return {}
    try:
        with open(p, encoding="utf-8", newline="") as fh:
            return {r["path"]: r for r in csv.DictReader(fh) if r.get("path")}
    except (OSError, ValueError, csv.Error, UnicodeDecodeError):
        return {}


def analyze_group(frames, outdir: str, workers: int = 2, progress=None, catalog_dir=None, log=None,
                  preview_cache_dir=None) -> str:
    """Measure all frames of a light group; writes metrics.csv, thumbs/, previews.json and measure_errors.json.

    Reuse order: CSV row with the same stat signature, then catalog (same content), else measure.
    The CSV is rebuilt from current frames, so removed files drop out. Errors are replaced per run;
    ``kind`` is stat | measure | changed. Preview media are prepared during the same read as measurement.
    """
    from . import previews
    os.makedirs(os.path.join(outdir, "thumbs"), exist_ok=True)
    p = os.path.join(outdir, "metrics.csv")
    old = _read_csv_rows(p)
    cat = catalog.open_catalog(catalog_dir) if catalog_dir else None
    preview_root = previews.cache_root(preview_cache_dir)
    ver = str(catalog.LIGHT_VERSION)
    rows: dict[str, dict] = {}
    errors: list[dict] = []
    todo = []
    reused = []
    n_csv = n_cat = 0
    for f in frames:
        try:
            sha, sig = cat.resolve(f.path) if cat else (None, catalog.file_sig(f.path))
        except OSError as e:
            _emit(log, "Nicht lesbar, übersprungen: %s (%s)" % (os.path.basename(f.path), e))
            errors.append(_error(f.path, "stat", e))
            continue
        thumb_file = os.path.join(outdir, "thumbs", thumb_name(f.path))
        o = old.get(f.path)
        if o and o.get("file_sig") == sig and o.get("metrics_ver") == ver and os.path.exists(thumb_file):
            rows[f.path] = dict(context_row(f), **{k: o.get(k) for k in METRIC_COLS},
                                file_sig=sig, sha256=o.get("sha256", ""), metrics_ver=ver)
            n_csv += 1
            reused.append(f)
            continue
        hit = cat.light_get(sha, f.bayer) if (cat and sha) else None
        if hit:
            metrics, thumb = hit
            np.save(thumb_file, thumb)
            rows[f.path] = dict(context_row(f), **metrics, file_sig=sig, sha256=sha, metrics_ver=ver)
            n_cat += 1
            reused.append(f)
            continue
        todo.append((f, sig, sha, "geändert" if f.path in old else "neu"))
    n_new = sum(1 for t in todo if t[3] == "neu")
    n_changed = len(todo) - n_new
    preview_records: dict[str, dict] = {}
    if todo:
        work = []
        for f, sig, sha, _why in todo:
            preview = {"sha": sha, "sig": sig, "root": preview_root}
            work.append((f, preview))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, ((f, sig, sha, _why), (res, err)) in enumerate(zip(todo, ex.map(_work, work))):
                kind = "measure"
                if res and _sig_or_none(f.path) != sig:
                    err = (f.path, "während der Messung geändert")
                    res, kind = None, "changed"
                if res:
                    row, thumb, preview_rec = res
                    np.save(os.path.join(outdir, "thumbs", thumb_name(f.path)), thumb)
                    if preview_rec:
                        preview_records[f.path] = preview_rec
                    if cat and sha:
                        cat.light_put(sha, f.bayer, {k: row[k] for k in METRIC_COLS}, thumb)
                    rows[f.path] = dict(row, file_sig=sig, sha256=sha or "", metrics_ver=ver)
                    _emit(log, "  %s  FWHM %s  Sterne %s  Rauschen %s" % (
                        os.path.basename(f.path), row.get("fwhm"), row.get("nstars"), row.get("noise")))
                elif err:
                    _emit(log, "Messung fehlgeschlagen: %s (%s)" % (os.path.basename(err[0]), err[1]))
                    errors.append(_error(f.path, kind, err[1]))
                if progress:
                    progress(i + 1, len(todo), err)
    _emit(log, "Katalog: %d aus Projekt-CSV, %d aus Katalog, %d neu, %d geändert" % (n_csv, n_cat, n_new, n_changed))
    tmp = p + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for f in frames:
            if f.path in rows:
                w.writerow(rows[f.path])
    os.replace(tmp, p)
    _write_errors(outdir, errors)
    _prepare_previews(reused, preview_records, outdir, preview_root, catalog_dir, log)
    if cat:
        cat.flush()
        if cat.write_failures:
            _emit(log, "Katalog-Schreibfehler: %d (Analyse fortgesetzt; einzelne Einträge nicht gespeichert)"
                  % cat.write_failures)
    return p


def _prepare_previews(reused, records: dict, outdir: str, preview_root: str, catalog_dir, log) -> None:
    """Cached rows still need one explicit read when their project manifest has no browse media yet."""
    from . import previews
    try:
        if records:
            previews.record_media(outdir, records)
        missing = previews.missing_frames(reused, outdir, preview_root)
        if missing:
            _emit(log, "Bereite Vorschauen vor: %d Frames …" % len(missing))
            previews.prepare_frames(missing, outdir, preview_root, catalog_dir=catalog_dir)
    except Exception as e:  # noqa: BLE001 – preview cache trouble must never fail the analysis
        _emit(log, "Vorschau-Cache nicht schreibbar: %s" % e)


def _error(path: str, kind: str, reason) -> dict:
    return {"path": path, "kind": kind, "reason": str(reason)}


def _write_errors(outdir: str, errors: list[dict]) -> None:
    """Replace measure_errors.json fully, so fixed files disappear on the next run."""
    p = os.path.join(outdir, ERRORS_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(errors, key=lambda e: e["path"]), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _emit(log, msg: str) -> None:
    if log:
        log(msg)


def _sig_or_none(path: str):
    try:
        return catalog.file_sig(path)
    except OSError:
        return None
