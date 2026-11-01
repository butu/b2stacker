"""Kamera-Drehung je Nacht – entscheidet, ob Nächte eigene Flats brauchen.

Flats bilden Staub und Vignettierung ab. Wird die Kamera gegenüber dem Teleskop gedreht (manueller oder
motorischer Rotator), drehen sich Staubschatten auf Sensor/Filter mit – Flats einer anderen Stellung passen
dann nicht mehr. Zwei Quellen:

1. Rotator-Winkel im FITS-Header (ROTATOR/ROTATANG; NINA schreibt ihn auch für einen manuellen Rotator).
2. Gemessene Drehung des Sternfelds zwischen den Nächten (aus den Vorschaubildern der Analyse).
   Ein Meridian-Flip dreht das Bild um 180°, ändert aber nichts an Kamera ↔ Teleskop – daher modulo 180°.
"""
from __future__ import annotations

import math
import os

import numpy as np
from scipy import ndimage as ndi

ROT_TOL_DEG = 2.0          # ab dieser Drehung gelten Nächte als verschiedene Stellung


def stars_from_thumb(t: np.ndarray, n: int = 60) -> np.ndarray:
    """Hellste Sterne eines Vorschaubilds → (N, 3): y, x, Fluss (subpixel)."""
    t = t.astype(np.float32)
    bg = ndi.median_filter(t, size=9) if t.size < 400_000 else np.median(t)
    d = t - bg
    noise = 1.4826 * np.median(np.abs(d - np.median(d))) + 1e-6
    sm = ndi.gaussian_filter(d, 0.8)
    mx = ndi.maximum_filter(sm, size=5)
    ys, xs = np.nonzero((sm == mx) & (sm > 6 * noise))
    H, W = t.shape
    keep = (ys > 3) & (xs > 3) & (ys < H - 4) & (xs < W - 4)
    ys, xs = ys[keep], xs[keep]
    order = np.argsort(sm[ys, xs])[::-1][:n]
    out = []
    for k in order:
        y, x = ys[k], xs[k]
        w = np.clip(d[y - 2:y + 3, x - 2:x + 3], 0, None)
        s = w.sum()
        if s <= 0:
            continue
        yy, xx = np.mgrid[-2:3, -2:3]
        out.append((y + (w * yy).sum() / s, x + (w * xx).sum() / s, s))
    return np.array(out) if out else np.zeros((0, 3))


def estimate_rotation(a: np.ndarray, b: np.ndarray, len_tol: float = 0.01) -> tuple[float | None, float]:
    """Drehwinkel von Sternliste b gegenüber a (Grad, -180…180) über Paar-Vektoren.

    Für jedes Sternpaar in a und b: Länge und Richtung. Paare gleicher Länge (±1 %) stimmen im
    Richtungsunterschied überein → Häufungspunkt = Drehung. Robust gegen Verschiebung (Dithering).
    → (Winkel, Vertrauen 0…1)
    """
    if len(a) < 8 or len(b) < 8:
        return None, 0.0

    def pairs(s):
        i, j = np.triu_indices(len(s), 1)
        dy, dx = s[j, 0] - s[i, 0], s[j, 1] - s[i, 1]
        L = np.hypot(dy, dx)
        ang = np.degrees(np.arctan2(dy, dx))
        m = L > 8
        return L[m], ang[m]
    La, Aa = pairs(a[:, :2])
    Lb, Ab = pairs(b[:, :2])
    order = np.argsort(Lb)
    Lb, Ab = Lb[order], Ab[order]
    diffs = []
    for l, an in zip(La, Aa):
        lo, hi = np.searchsorted(Lb, [l * (1 - len_tol), l * (1 + len_tol)])
        if hi > lo:
            d = (Ab[lo:hi] - an + 180) % 360 - 180
            diffs.extend(d.tolist())
            diffs.extend(((d + 360) % 360 - 180).tolist())   # Paar-Richtung ist mehrdeutig (±180°)
    if len(diffs) < 20:
        return None, 0.0
    diffs = np.array(diffs)
    hist, edges = np.histogram(diffs, bins=720, range=(-180, 180))
    hist = np.convolve(hist, [1, 2, 1], mode="same")
    k = int(np.argmax(hist))
    centre = (edges[k] + edges[k + 1]) / 2
    near = diffs[np.abs((diffs - centre + 180) % 360 - 180) < 1.0]
    ang = float(np.median(near)) if len(near) else centre
    second = np.sort(hist)[-6] if len(hist) > 6 else 0
    conf = float(1 - second / max(hist[k], 1))
    return ang, round(conf, 2)


def fold180(a: float) -> float:
    """Meridian-Flip ignorieren: Winkel auf -90…90° falten."""
    return (a + 90) % 180 - 90


def night_rotations(rows: list[dict], analysis_dir: str, thumb_name) -> dict:
    """Je Nacht: gemessene Drehung des Sternfelds gegenüber der ersten Nacht (gefaltet auf ±90°)."""
    by_night: dict[str, list] = {}
    for r in rows:
        if r.get("status") in ("rejected",) or not r.get("night"):
            continue
        by_night.setdefault(r["night"], []).append(r)
    if len(by_night) < 2:
        return {}
    reps = {}
    for night, lst in by_night.items():
        best = max(lst, key=lambda r: r.get("nstars") or 0)
        p = os.path.join(analysis_dir, "thumbs", thumb_name(best["path"]))
        if os.path.exists(p):
            try:
                reps[night] = stars_from_thumb(np.load(p))
            except Exception:  # noqa: BLE001
                pass
    nights = sorted(reps)
    if len(nights) < 2:
        return {}
    ref = nights[0]
    out = {ref: dict(angle=0.0, conf=1.0, ref=ref)}
    for n in nights[1:]:
        a, c = estimate_rotation(reps[ref], reps[n])
        out[n] = dict(angle=round(fold180(a), 2) if a is not None else None, conf=c, ref=ref)
    return out


def group_nights(nights: list[str], header_rot: dict, measured: dict, tol: float = ROT_TOL_DEG) -> list[dict]:
    """Nächte nach Kamera-Stellung gruppieren. header_rot: {nacht: grad|None}; measured: night_rotations()."""
    use_header = sum(1 for n in nights if header_rot.get(n) is not None) >= max(2, len(nights) // 2 + 1)
    groups: list[dict] = []
    for n in sorted(nights):
        if use_header and header_rot.get(n) is not None:
            v, src = header_rot[n], "rotator"
        elif (measured.get(n) or {}).get("angle") is not None and (measured[n].get("conf") or 0) >= 0.3:
            v, src = measured[n]["angle"], "sky"
        else:
            v, src = None, None
        g = None
        if v is not None:
            for gg in groups:
                if gg["source"] == src and gg["angle"] is not None:
                    d = abs((v - gg["angle"] + 180) % 360 - 180) if src == "rotator" else abs(fold180(v - gg["angle"]))
                    if d <= tol:
                        g = gg
                        break
        elif groups:
            g = groups[-1]           # unbekannt → zur vorherigen Nacht
        if g is None:
            g = dict(nights=[], angle=v, source=src)
            groups.append(g)
        g["nights"].append(n)
    for i, g in enumerate(groups, 1):
        g["key"] = str(i)
    return groups
