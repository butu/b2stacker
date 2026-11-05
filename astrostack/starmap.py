"""Erkannte Sterne (und Hotpixel) eines Frames für die Anzeige im Frame-Browser (Bericht 2 §15).

Nutzt dieselbe Sternerkennung wie die Analyse (``analyze._stars``), Koordinaten in Originalpixeln. Wird beim ersten
Abruf berechnet und im Projekt gecacht (``analysis/stars/<schlüssel>.json``) – die Originale werden nur gelesen.
Das ist b2stackers eigene Erkennung, nicht die von PixInsight; „würde verwerfen“ ist eine Näherung der
StarAlignment-Regel ``Fläche/Quadrat < maxStarDistortion``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os

import numpy as np

MAX_STARS = 300
MAX_HOT = 5000
VERSION = 1


def _key(path: str) -> str:
    try:
        st = os.stat(path)
        sig = "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
    except OSError:
        sig = path
    return hashlib.sha1(("%d|%s" % (VERSION, sig)).encode("utf-8")).hexdigest()[:20]


def distortion(ecc: float) -> float:
    """StarAlignment-Maß Fläche/umschreibendes Quadrat einer Ellipse: π/4 · b/a."""
    return math.pi / 4 * math.sqrt(max(0.0, 1 - ecc * ecc))


def compute(path: str, bayer: str | None, hot: bool = False, raw=None) -> dict:
    from . import analyze, imageio
    raw = imageio.load(path) if raw is None else raw
    if raw.ndim == 3:
        raw = raw.mean(axis=0)
    H, W = raw.shape
    ch = imageio.split_cfa(raw, bayer)
    scale = 2.0 if "G" in ch else 1.0
    L = (ch["R"] + 2 * ch["G"] + ch["B"]) / 4 if "G" in ch else ch["L"]
    bg = float(np.median(L))
    noise = float(1.4826 * np.median(np.abs(L[::4, ::4] - bg)))
    n, S = analyze._stars(L, bg, noise)
    stars = [[round(float(x) * scale + (scale - 1) / 2, 1), round(float(y) * scale + (scale - 1) / 2, 1),
              round(float(f) * scale, 2), round(float(e), 3), round(float(np.degrees(a)), 1)]
             for y, x, f, e, a in S[:MAX_STARS]]
    out = dict(width=int(W), height=int(H), nstars=int(n), stars=stars,
               fwhm_med=round(float(np.median(S[:, 2])) * scale, 2) if len(S) else None,
               ecc_med=round(float(np.median(S[:, 3])), 3) if len(S) else None)
    if hot:
        from . import hotpix
        mask, _rep = hotpix.detect(raw, is_cfa="G" in ch)
        ys, xs = np.nonzero(mask)
        out["hot_count"] = int(len(ys))
        step = max(1, len(ys) // MAX_HOT)
        out["hot"] = [[int(x), int(y)] for y, x in zip(ys[::step], xs[::step])]
    return out


def frame_stars(path: str, cache_dir: str, bayer: str | None, hot: bool = False) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    p = os.path.join(cache_dir, _key(path) + ".json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        if not hot or "hot" in d:
            return d
    except (OSError, ValueError):
        d = None
    res = compute(path, bayer, hot=hot)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    os.replace(tmp, p)
    return res
