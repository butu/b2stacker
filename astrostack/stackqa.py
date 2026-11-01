"""Fertige Stacks: Farbsäume korrigieren, in Kanäle aufteilen, Qualität messen, Vorschau erzeugen.

Farbsäume: Die Erdatmosphäre bricht Blau stärker als Rot (atmosphärische Dispersion) – je tiefer das Objekt,
desto weiter liegen die Farbkanäle auseinander. WBPP richtet das Farbbild als Ganzes aus, nicht jeden Kanal
einzeln. Deshalb wird hier der mittlere Versatz von Rot und Blau gegenüber Grün an den Sternen gemessen und
mit Subpixel-Genauigkeit zurückgeschoben (kubische Spline-Interpolation). Bei Dualband (Ha/OIII) liegt
Ha danach exakt auf OIII.
"""
from __future__ import annotations

import math
import os
import re

import numpy as np
from scipy import ndimage as ndi

from . import xisf
from .analyze import _stars

FULL = 65535.0
FRINGE_MIN_PX = 0.15        # ab diesem Versatz wird korrigiert
MIN_STARS = 25


# ------------------------------------------------------------------ Laden / Speichern

def load_master(path: str) -> np.ndarray:
    """Erstes Bild eines XISF-Masters als (C, H, W) float32 in [0,1]."""
    a = xisf.read_image(path, 0).astype(np.float32)
    if a.max() > 1.5:          # sollte nicht vorkommen, aber z. B. UInt16-Master
        a = a / FULL
    return a


def keywords_of(path: str) -> dict:
    try:
        xml, _ = xisf._xml(path)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for name, value in re.findall(r'<FITSKeyword name="([^"]+)" value="([^"]*)"', xml):
        if name not in out and name not in ("HISTORY", "COMMENT", "NAXIS", "NAXIS1", "NAXIS2", "NAXIS3", "BITPIX"):
            out[name] = value.strip().strip("'").strip()
    return out


# ------------------------------------------------------------------ Sterne / Versatz

def _lum(img: np.ndarray) -> np.ndarray:
    return img.mean(axis=0) if img.shape[0] > 1 else img[0]


def _bg_noise(L: np.ndarray) -> tuple[float, float]:
    s = L[::4, ::4]
    bg = float(np.median(s))
    # Rauschen aus Nachbarpixel-Differenzen: unempfindlich gegen Nebel und Verläufe
    a = L[::2, ::2]
    d = (a[:, 1:] - a[:, :-1]).ravel()[::3]
    noise = float(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2))
    return bg, max(noise, 1e-9)


def _centroids(ch: np.ndarray, pts: np.ndarray, r: int = 7) -> np.ndarray:
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    out = np.full((len(pts), 2), np.nan)
    H, W = ch.shape
    for i, (y, x) in enumerate(pts):
        y, x = int(y), int(x)
        if y < r or x < r or y >= H - r or x >= W - r:
            continue
        w = ch[y - r:y + r + 1, x - r:x + r + 1]
        b = np.concatenate([w[0], w[-1], w[1:-1, 0], w[1:-1, -1]])
        c = w - np.median(b)
        thr = 0.15 * c.max()
        c = np.where(c > thr, c - thr, 0)
        s = c.sum()
        if s > 0:
            out[i] = ((c * yy).sum() / s + y, (c * xx).sum() / s + x)
    return out


def star_points(L: np.ndarray, maxn: int = 600) -> tuple[int, np.ndarray]:
    bg, noise = _bg_noise(L)
    n, S = _stars(L * FULL, bg * FULL, noise * FULL, maxn=maxn)
    return n, S


def channel_shifts(img: np.ndarray, ref: int = 1, pts: np.ndarray | None = None) -> dict:
    """Mittlerer Versatz (dy, dx) jedes Kanals gegenüber Kanal ``ref`` (Grün) in Pixeln."""
    if img.shape[0] < 3:
        return {}
    if pts is None:
        _, S = star_points(img[ref])
        pts = S[:, :2] if len(S) else np.zeros((0, 2))
    if len(pts) < MIN_STARS:
        return {"stars": int(len(pts))}
    c_ref = _centroids(img[ref], pts)
    res = {"stars": int(len(pts))}
    for k, name in ((0, "R"), (2, "B")):
        c = _centroids(img[k], pts)
        d = c - c_ref
        ok = np.isfinite(d).all(axis=1) & (np.abs(d) < 3).all(axis=1)
        if ok.sum() < MIN_STARS:
            continue
        dy, dx = np.median(d[ok, 0]), np.median(d[ok, 1])
        res[name] = (round(float(dy), 3), round(float(dx), 3))
    return res


def align_channels(img: np.ndarray, shifts: dict, log=print) -> tuple[np.ndarray, bool]:
    """R und B um den gemessenen Versatz zurückschieben, wenn er sichtbar ist."""
    todo = {k: v for k, v in shifts.items() if k in ("R", "B") and math.hypot(*v) >= FRINGE_MIN_PX}
    if not todo:
        return img, False
    out = img.copy()
    for name, (dy, dx) in todo.items():
        k = {"R": 0, "B": 2}[name]
        out[k] = ndi.shift(img[k], (-dy, -dx), order=3, mode="nearest", prefilter=True)
        log("  Farbsaum korrigiert: %s um %.2f px verschoben" % ({"R": "Rot", "B": "Blau"}[name], math.hypot(dy, dx)))
    return out, True


# ------------------------------------------------------------------ Kennzahlen

def kpis(img: np.ndarray, pixscale: float | None = None, shifts: dict | None = None) -> dict:
    """Qualitätskennzahlen eines fertigen Stacks (linear, vor jeder Bearbeitung)."""
    L = _lum(img)
    bg, noise = _bg_noise(L)
    n, S = star_points(L, maxn=800)
    H, W = L.shape
    fwhm = float(np.median(S[:, 2])) if len(S) else None
    ecc = float(np.median(S[:, 3])) if len(S) else None
    hfr = _hfr(L, S[:200, :2]) if len(S) else None
    # Verlauf: Hintergrund in 8×8 Kacheln (unteres Quartil, damit Nebel nicht zählt)
    t = np.array([[np.percentile(L[i * H // 8:(i + 1) * H // 8:4, j * W // 8:(j + 1) * W // 8:4], 25)
                   for j in range(8)] for i in range(8)])
    grad = float((t.max() - t.min()) / max(abs(np.median(t)), 1e-9) * 100)
    sh = shifts or {}
    fringe = max([math.hypot(*sh[k]) for k in ("R", "B") if k in sh], default=None)
    out = dict(
        noise=round(noise * FULL, 3),                 # ADU (16 bit)
        bg=round(bg * FULL, 1),
        snr_bg=round(bg / noise, 1),
        stars=int(n),
        fwhm_px=round(fwhm, 2) if fwhm else None,
        fwhm_as=round(fwhm * pixscale, 2) if (fwhm and pixscale) else None,
        hfr_px=round(hfr, 2) if hfr else None,
        ecc=round(ecc, 3) if ecc is not None else None,
        gradient_pct=round(grad, 1),
        fringe_px=round(fringe, 2) if fringe is not None else None,
        width=int(W), height=int(H), channels=int(img.shape[0]),
    )
    return out


def _hfr(L: np.ndarray, pts: np.ndarray, r: int = 10) -> float | None:
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    vals = []
    H, W = L.shape
    for y, x in pts:
        y, x = int(y), int(x)
        if y < r or x < r or y >= H - r or x >= W - r:
            continue
        w = L[y - r:y + r + 1, x - r:x + r + 1]
        b = np.concatenate([w[0], w[-1], w[1:-1, 0], w[1:-1, -1]])
        c = np.clip(w - np.median(b), 0, None)
        s = c.sum()
        if s <= 0:
            continue
        cy, cx = (c * yy).sum() / s, (c * xx).sum() / s
        rr = np.hypot(yy - cy, xx - cx)
        vals.append((c * rr).sum() / s)
    return float(np.median(vals)) if len(vals) >= 10 else None


# ------------------------------------------------------------------ Vorschau

def stretch(a: np.ndarray, target: float = 0.22) -> np.ndarray:
    """Automatischer Strich (MTF wie PixInsight STF, gemeinsam über alle Kanäle)."""
    s = a[..., ::4, ::4] if a.ndim == 3 else a[::4, ::4]
    med = float(np.median(s))
    mad = float(np.median(np.abs(s - med))) * 1.4826
    lo = max(med - 2.8 * mad, float(s.min()))
    x = np.clip((a - lo) / max(1 - lo, 1e-6), 0, 1)
    m = np.clip((med - lo) / max(1 - lo, 1e-6), 1e-6, 1)
    b = m * (target - 1) / ((2 * target - 1) * m - target)
    return np.clip(((b - 1) * x) / ((2 * b - 1) * x - b), 0, 1)


def preview_jpeg(img: np.ndarray, out_path: str, width: int = 1600) -> str:
    from PIL import Image
    C, H, W = img.shape
    step = max(1, W // (width * 2))
    small = img[:, ::step, ::step]
    if C == 3:   # Kanäle grob angleichen (nur für die Vorschau)
        med = np.median(small.reshape(3, -1), axis=1)
        small = small - med[:, None, None] + med.mean()
    st = stretch(small)
    arr = (np.moveaxis(st, 0, -1) * 255).astype(np.uint8) if C == 3 else (st[0] * 255).astype(np.uint8)
    im = Image.fromarray(arr)
    im.thumbnail((width, width), Image.LANCZOS)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    im.save(out_path, "JPEG", quality=85)
    return out_path


# ------------------------------------------------------------------ Gesamtablauf je Lauf

def final_name(target: str, label: str, date: str, hours: float | None) -> str:
    safe = lambda s: "".join(ch for ch in s if ch.isalnum() or ch in "-_") or "x"   # noqa: E731
    h = ("_%.1fh" % hours) if hours else ""
    return "%s_%s_%s%s.xisf" % (safe(target), safe(label), date, h)


def unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s_%d%s" % (base, i, ext)):
        i += 1
    return "%s_%d%s" % (base, i, ext)


def process_master(master: str, run: dict, target: str, finaldir: str, previewdir: str, date: str,
                   pixscale: float | None = None, fix_fringes: bool = True, log=print) -> list[dict]:
    """Master → Endergebnisse (Farbsäume korrigiert, ggf. aufgeteilt) mit Kennzahlen und Vorschau."""
    os.makedirs(finaldir, exist_ok=True)
    img = load_master(master)
    kw = keywords_of(master)
    hours = run.get("hours")
    outputs = []
    shifts_before = {}
    if img.shape[0] == 3:
        log("  Messe Farbkanäle …")
        _, S = star_points(img[1])
        pts = S[:, :2] if len(S) else np.zeros((0, 2))
        shifts_before = channel_shifts(img, 1, pts)
        log("  Versatz gegenüber Grün: " + ", ".join(
            "%s %.2f px" % (k, math.hypot(*v)) for k, v in shifts_before.items() if k in ("R", "B")) if any(
            k in shifts_before for k in ("R", "B")) else "  Versatz: zu wenige Sterne zum Messen")
        if fix_fringes:
            img, changed = align_channels(img, shifts_before, log)
            shifts_after = channel_shifts(img, 1, pts) if changed else shifts_before
        else:
            changed, shifts_after = False, shifts_before
    else:
        changed, shifts_after = False, {}
    extra = {"ASTROSTK": "AstroStack %s" % run.get("name", ""), "FRINGEFX": "T" if changed else "F"}
    if run.get("mode") == "extract" and img.shape[0] == 3:
        for ch in run["outputs"]:
            code = run["channel_map"][ch]
            idx = ["RGB".index(c) for c in code]
            mono = img[idx].mean(axis=0, keepdims=True)
            path = unique(os.path.join(finaldir, final_name(target, ch, date, hours)))
            xisf.write_image(path, mono, dict(kw, FILTER=ch, **extra), color=False)
            k = kpis(mono, pixscale)
            k["fringe_px"] = round(max([math.hypot(*shifts_after[c]) for c in ("R", "B") if c in shifts_after], default=0), 2) \
                if shifts_after else None
            k["fringe_before_px"] = round(max([math.hypot(*shifts_before[c]) for c in ("R", "B") if c in shifts_before], default=0), 2) \
                if shifts_before else None
            pv = preview_jpeg(mono, os.path.join(previewdir, os.path.splitext(os.path.basename(path))[0] + ".jpg"))
            outputs.append(dict(name=ch, file=path, preview=pv, kpi=k))
            log("  %s: %s" % (ch, os.path.basename(path)))
    else:
        label = run.get("name") or "Master"
        path = unique(os.path.join(finaldir, final_name(target, label, date, hours)))
        xisf.write_image(path, img, dict(kw, **extra), color=img.shape[0] == 3)
        k = kpis(img, pixscale, shifts_after)
        if shifts_before:
            k["fringe_before_px"] = round(max([math.hypot(*shifts_before[c]) for c in ("R", "B") if c in shifts_before], default=0), 2)
        pv = preview_jpeg(img, os.path.join(previewdir, os.path.splitext(os.path.basename(path))[0] + ".jpg"))
        outputs.append(dict(name=label, file=path, preview=pv, kpi=k))
        log("  %s: %s" % (label, os.path.basename(path)))
    return outputs
