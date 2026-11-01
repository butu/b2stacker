"""Hotpixel-Entfernung vor dem Stacken (wenn keine passenden Darks da sind).

Warum (NGC 1999, 2026-11-01): ohne Darks und ohne Dithering liegen ~4 500 Hotpixel pro 600-s-Bild immer an denselben
Sensor-Stellen. StarAlignment hat diese Hotpixel für Sterne gehalten und die Bilder am *Sensor* statt am Himmel
ausgerichtet (Verschiebung 0,00 px, Drehung 0,00° – auch für die Bilder nach dem Meridian-Flip, die in Wahrheit um
180° gedreht sind). Ergebnis: eine um 180° gedrehte Geisterkopie der hellen Sterne im Stack.

Vorgehen (wie CosmeticCorrection „Auto detect“, aber Bayer-bewusst):
  * je Farbebene des Bayer-Musters: Abweichung jedes Pixels vom Median seiner 8 gleichfarbigen Nachbarn
  * Hotpixel = Abweichung > K·σ UND alle 8 direkten Nachbarn (beliebige Farbe) nahezu unauffällig
    (ein Stern ist immer mehrere Pixel breit, ein Hotpixel nicht)
  * Ersatz durch den Median der gleichfarbigen Nachbarn. Header bleibt erhalten (+ HISTORY).
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from astropy.io import fits
from scipy import ndimage as ndi

K_SIGMA = 6.0          # Schwelle über dem Rauschen
ISOLATION = 0.25       # Nachbarn dürfen höchstens 25 % der Hotpixel-Abweichung haben
COLD_SIGMA = 8.0       # kalte (tote) Pixel


def _planes(is_cfa: bool):
    return [(0, 0), (0, 1), (1, 0), (1, 1)] if is_cfa else [(0, 0)]


def detect(data: np.ndarray, is_cfa: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """→ (Maske der Hot-/Kaltpixel, Ersatzwerte)."""
    d = data.astype(np.float32)
    step = 2 if is_cfa else 1
    resid = np.zeros_like(d)
    med = np.zeros_like(d)
    sig = np.zeros_like(d)
    for oy, ox in _planes(is_cfa):
        p = d[oy::step, ox::step]
        m = ndi.median_filter(p, size=3, mode="reflect")
        r = p - m
        s = 1.4826 * float(np.median(np.abs(r[::4, ::4] - np.median(r[::4, ::4])))) or 1.0
        resid[oy::step, ox::step] = r
        med[oy::step, ox::step] = m
        sig[oy::step, ox::step] = s
    # direkte Nachbarn (alle Farben) ohne das Pixel selbst
    fp = np.ones((3, 3), bool)
    fp[1, 1] = False
    nmax = ndi.maximum_filter(resid, footprint=fp, mode="reflect")
    hot = (resid > K_SIGMA * sig) & (nmax < ISOLATION * resid)
    nmin = ndi.minimum_filter(resid, footprint=fp, mode="reflect")
    cold = (resid < -COLD_SIGMA * sig) & (nmin > ISOLATION * resid)
    return hot | cold, med


def clean_file(src: str, dst: str, is_cfa: bool | None = None) -> dict:
    """Eine FITS-Datei bereinigt nach dst schreiben. Gibt Statistik zurück."""
    with fits.open(src, memmap=False) as hd:
        h = hd[0].header.copy()
        data = hd[0].data
    if data is None or data.ndim != 2:
        raise ValueError("kein 2D-Rohbild: " + src)
    if is_cfa is None:
        is_cfa = bool(h.get("BAYERPAT") or h.get("COLORTYP") or h.get("CFA-PAT"))
    mask, rep = detect(data, is_cfa)
    out = data.copy()
    if np.issubdtype(out.dtype, np.integer):
        info = np.iinfo(out.dtype)
        out[mask] = np.clip(np.rint(rep[mask]), info.min, info.max).astype(out.dtype)
    else:
        out[mask] = rep[mask]
    n = int(mask.sum())
    h.add_history("b2stacker: %d Hot-/Kaltpixel ersetzt (keine Darks vorhanden)" % n)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    for k in ("BZERO", "BSCALE", "BITPIX"):           # astropy setzt das passend zum Datentyp (uint16 → int16+32768)
        h.remove(k, ignore_missing=True, remove_all=True)
    hdu = fits.PrimaryHDU(out, header=h)
    hdu.writeto(tmp, overwrite=True, output_verify="silentfix")
    os.replace(tmp, dst)
    return {"src": src, "dst": dst, "pixels": n}


def cleaned_path(cache: str, src: str) -> str:
    """Eindeutiger Zielpfad: Unterordner je Quellordner, Dateiname bleibt (wichtig für Zuordnung)."""
    d = hashlib.sha1(os.path.dirname(os.path.abspath(src)).lower().encode("utf-8")).hexdigest()[:10]
    base = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(cache, d, base + ".fits")


def clean_lights(paths: list[str], cache: str, is_cfa: bool | None = None, workers: int = 4,
                 log=lambda *a: None, progress=None, cancel=None) -> tuple[dict, list[dict]]:
    """Alle Lights bereinigen (bereits bereinigte, aktuelle Dateien werden wiederverwendet).

    → ({original: bereinigt}, Statistik je Datei)."""
    mapping: dict[str, str] = {}
    todo = []
    for p in paths:
        q = cleaned_path(cache, p)
        mapping[p] = q
        if not (os.path.exists(q) and os.path.getmtime(q) >= os.path.getmtime(p)):
            todo.append((p, q))
    stats: list[dict] = []
    done = len(paths) - len(todo)
    if progress:
        progress(done, len(paths))

    def one(pq):
        if cancel is not None and cancel.is_set():
            return None
        return clean_file(pq[0], pq[1], is_cfa)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r in ex.map(one, todo):
            done += 1
            if r:
                stats.append(r)
            if progress:
                progress(done, len(paths))
    return mapping, stats
