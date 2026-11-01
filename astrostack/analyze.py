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

from . import imageio

warnings.filterwarnings("ignore")

COLS = ["path", "night", "excluded", "filter", "filter_kind", "date_obs", "exp", "gain", "offset", "readout", "temp",
        "settemp", "alt", "airmass", "hum", "ambtemp", "dewpoint", "wind", "focpos", "foctemp", "rot", "pier",
        "pixscale", "hfr_name", "moon_illum", "moon_alt",
        "bg", "noise", "snr_proxy", "nstars", "fwhm", "ecc", "grad", "r_g", "b_g", "p001", "sat_frac",
        "fwhm_c", "fwhm_corner", "tiles_fwhm", "tiles_ecc", "tiles_ang", "tiles_n", "coherence", "coh_angle", "radial"]


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


def measure(meta) -> tuple[dict, np.ndarray]:
    """Ein Light vermessen. → (Zeile, Thumbnail 1/8-Auflösung der Luminanz)"""
    raw = imageio.load(meta.path)
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
        path=meta.path, night=meta.night, excluded=int(meta.excluded), filter=meta.filter, filter_kind=meta.filter_kind,
        date_obs=meta.date_obs, exp=meta.exposure, gain=meta.gain, offset=meta.offset, readout=meta.readout,
        temp=meta.ccd_temp, settemp=meta.set_temp, alt=meta.alt, airmass=meta.airmass, hum=meta.humidity,
        ambtemp=meta.ambient_temp, dewpoint=meta.dew_point, wind=meta.wind, focpos=meta.focus_pos, foctemp=meta.focus_temp,
        rot=meta.rotator, pier=meta.pier_side, pixscale=meta.pixel_scale, hfr_name=meta.hfr_from_name,
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


def _work(meta):
    try:
        return measure(meta), None
    except Exception as e:
        return None, (meta.path, str(e))


def analyze_group(frames, outdir: str, workers: int = 2, progress=None) -> str:
    """Alle Frames einer Light-Gruppe vermessen (fortsetzbar). Schreibt metrics.csv + thumbs/."""
    os.makedirs(os.path.join(outdir, "thumbs"), exist_ok=True)
    p = os.path.join(outdir, "metrics.csv")
    done = set()
    if os.path.exists(p) and os.path.getsize(p) > 0:
        done = {r["path"] for r in csv.DictReader(open(p, encoding="utf-8"))}
    todo = [f for f in frames if f.path not in done]
    new = not done
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if new:
            w.writeheader()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, (res, err) in enumerate(ex.map(_work, todo)):
                if res:
                    row, thumb = res
                    np.save(os.path.join(outdir, "thumbs", thumb_name(row["path"])), thumb)
                    w.writerow(row)
                    fh.flush()
                if progress:
                    progress(i + 1, len(todo), err)
    return p
