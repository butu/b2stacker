"""Fertige Stacks ansehen und vergleichen: Qualitätswerte, Vorschau, zwei Stacks übereinander.

Nach jedem erfolgreichen Stack (und für ältere Stacks einmal im Hintergrund) wird das Light-Master gelesen:

- Qualitätswerte mit ``stackqa.kpis`` (FWHM, Rauschen, Sterne, Exzentrizität, Verlauf) → ``stacks.quality_json``.
- Eine kleine lineare Kopie (höchstens ``SMALL_W`` Pixel breit, float16, ``.npy``) und eine gestreckte JPEG-Vorschau
  unter ``system/previews/stacks/``. Beides gehört b2stacker und verschwindet mit dem Stack.

Vergleich zweier Stacks: beide kleinen Kopien werden mit demselben Strich (Stretch) gerendert. Vorher wird B auf A
ausgerichtet (Phasenkorrelation, nur Verschiebung – eine Drehung zwischen den Stacks wird noch nicht ausgeglichen)
und linear an A angeglichen (gleicher Hintergrund, gleiche Signalhöhe), damit Unterschiede im Rauschen und in der
Schärfe sichtbar werden und nicht nur unterschiedliche Helligkeit. Die Bilder landen in ``system/previews/compare/``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os

import numpy as np
from scipy import ndimage as ndi

from . import db as dbm
from . import stackqa

QUALITY_VERSION = 1          # erhöhen, wenn sich die Messung ändert → ältere Stacks werden neu gemessen
SMALL_W = 1200               # Breite der linearen Kopie (Vergleich, Vorschau)
PREVIEW_W = 1200
COMPARE_VERSION = 3
STACKS_DIR = "stacks"
COMPARE_DIR = "compare"

# Kennzahl → (Beschriftung, Einheit, besser: "low" | "high" | None)
METRICS = [
    ("fwhm", "Schärfe (FWHM)", "", "low"),
    ("noise", "Rauschen", "ADU", "low"),
    ("stars", "Sterne", "", "high"),
    ("ecc", "Sternform (Exzentrizität)", "", "low"),
    ("gradient_pct", "Verlauf", "%", "low"),
    ("snr_bg", "Hintergrund / Rauschen", "", "high"),
]


class StackViewError(Exception):
    """Vorschau oder Vergleich nicht möglich (Klartext für die Oberfläche)."""


# ------------------------------------------------------------------ Pfade

def stacks_dir(ws) -> str:
    return os.path.join(ws.previews, STACKS_DIR)


def light_master(db: dbm.Database, sid: int) -> str | None:
    """Pfad des Light-Masters eines Stacks (ohne Autocrop-Variante, wenn es beide gibt)."""
    from .stacking import _light_master
    paths = [r[0] for r in db.query("SELECT path FROM assets WHERE stack_id = ? AND owned = 1 ORDER BY id", (sid,))]
    lm = _light_master(paths)
    if lm:
        return lm[0]
    # übernommener Stack (0.10/PixInsight): die Datei gehört nicht b2stacker, ist aber das Ergebnis
    if not paths and json.loads(db.value("SELECT settings_json FROM stacks WHERE id = ?", (sid,)) or "{}").get("imported"):
        other = [r[0] for r in db.query("SELECT path FROM assets WHERE stack_id = ? ORDER BY id", (sid,))]
        return other[0] if other else None
    return None


def _pixscale(db: dbm.Database, sid: int) -> float | None:
    row = db.one("SELECT s.focal_length, s.pixel_size, f.binning FROM stack_files sf JOIN files f ON f.id = sf.file_id "
                 "JOIN setups s ON s.id = f.setup_id WHERE sf.stack_id = ? AND s.focal_length > 0 "
                 "AND s.pixel_size > 0 LIMIT 1", (sid,))
    if not row:
        return None
    scale = 206.265 * row["pixel_size"] * (row["binning"] or 1) / row["focal_length"]
    st = json.loads(db.value("SELECT settings_json FROM stacks WHERE id = ?", (sid,)) or "{}")
    if any(s.get("key") == "drizzle" and s.get("value") for s in (st.get("settings") or []) if isinstance(s, dict)):
        scale /= 2
    return scale


# ------------------------------------------------------------------ Verkleinern, Strich, JPEG

def shrink(img: np.ndarray, width: int = SMALL_W) -> np.ndarray:
    """(C, H, W) → Blockmittel auf höchstens ``width`` Pixel Breite (rauscharm, ohne Aliasing)."""
    c, h, w = img.shape
    k = max(1, -(-w // width))
    if k == 1:
        return img.astype(np.float32)
    h2, w2 = h // k * k, w // k * k
    return img[:, :h2, :w2].reshape(c, h2 // k, k, w2 // k, k).mean(axis=(2, 4)).astype(np.float32)


def stretch_params(a: np.ndarray, target: float = 0.22) -> tuple[float, float]:
    """Parameter eines automatischen Strichs (wie ``stackqa.stretch``): (Schwarzpunkt, Mittelton)."""
    s = a[..., ::2, ::2]
    med = float(np.median(s))
    mad = float(np.median(np.abs(s - med))) * 1.4826
    lo = max(med - 2.8 * mad, float(s.min()))
    m = min(max((med - lo) / max(1 - lo, 1e-6), 1e-6), 1.0)
    b = m * (target - 1) / ((2 * target - 1) * m - target)
    return lo, b


def apply_stretch(a: np.ndarray, lo: float, b: float) -> np.ndarray:
    x = np.clip((a - lo) / max(1 - lo, 1e-6), 0, 1)
    return np.clip(((b - 1) * x) / ((2 * b - 1) * x - b), 0, 1)


def _neutral(a: np.ndarray) -> np.ndarray:
    """Farbkanäle grob angleichen (nur für die Ansicht)."""
    if a.shape[0] != 3:
        return a
    med = np.median(a.reshape(3, -1)[:, ::7], axis=1)
    return a - med[:, None, None] + med.mean()


def to_jpeg(a: np.ndarray, out: str, quality: int = 86) -> str:
    """Gestrecktes (C, H, W) in [0,1] als JPEG schreiben (atomar)."""
    from PIL import Image

    from .previews import _atomic_write
    import io
    arr = (np.moveaxis(a, 0, -1) * 255).astype(np.uint8) if a.shape[0] == 3 else (a[0] * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=quality)
    _atomic_write(out, buf.getvalue())
    return out


# ------------------------------------------------------------------ Messen

def measure(db: dbm.Database, ws, sid: int) -> dict:
    """Qualität und Vorschau eines Stacks aus dem Light-Master. Speichert und liefert ``quality``.
    Fehler (Datei fehlt, unlesbar) landen als ``error`` im Ergebnis – der Stack bleibt, wie er ist."""
    path = light_master(db, sid)
    q: dict = dict(version=QUALITY_VERSION, measured_at=dbm.now())
    try:
        if not path:
            raise StackViewError("Kein Light-Master zu diesem Stack gespeichert.")
        if not os.path.isfile(path):
            raise StackViewError("Datei fehlt: " + path)
        img = stackqa.load_master(path)
        img = np.nan_to_num(img, nan=0.0, posinf=1.0, neginf=0.0)
        k = stackqa.kpis(img, _pixscale(db, sid))
        q.update(k)
        q["fwhm"] = k.get("fwhm_as") if k.get("fwhm_as") else k.get("fwhm_px")
        q["fwhm_unit"] = "″" if k.get("fwhm_as") else "px"
        small = shrink(img)
        st = os.stat(path)
        name = "stack-%d-%s" % (sid, hashlib.sha1(("%s|%d|%d|%d" % (path, st.st_size, st.st_mtime_ns, QUALITY_VERSION))
                                                  .encode()).hexdigest()[:12])
        d = stacks_dir(ws)
        os.makedirs(d, exist_ok=True)
        _drop_previews(ws, sid, keep=name)
        np.save(os.path.join(d, name + ".npy"), small.astype(np.float16))
        disp = _neutral(small)
        to_jpeg(apply_stretch(disp, *stretch_params(disp)), os.path.join(d, name + ".jpg"))
        q["preview"] = name
        q["source"] = path
    except StackViewError as e:
        q["error"] = str(e)
    except Exception as e:  # noqa: BLE001 – unlesbares Master (z. B. komprimiertes XISF) blockiert nichts
        q["error"] = "Master unlesbar (%s: %s)" % (type(e).__name__, e)
    with db.transaction():
        db.execute("UPDATE stacks SET quality_json = ? WHERE id = ?", (json.dumps(q), sid))
    return q


def needs_measure(row) -> bool:
    """Stack fertig, aber ohne (aktuelle) Qualitätswerte?"""
    if row["status"] != "done":
        return False
    q = json.loads(row["quality_json"] or "null")
    return not q or int(q.get("version") or 0) < QUALITY_VERSION


def _drop_previews(ws, sid: int, keep: str | None = None) -> None:
    d = stacks_dir(ws)
    try:
        names = os.listdir(d)
    except OSError:
        return
    pre = "stack-%d-" % sid
    for n in names:
        if n.startswith(pre) and (keep is None or not n.startswith(keep)):
            try:
                os.remove(os.path.join(d, n))
            except OSError:          # Windows: gerade geöffnet → beim nächsten Mal
                pass


def forget(ws, sid: int) -> None:
    """Vorschauen und Vergleichsbilder eines gelöschten Stacks entfernen."""
    _drop_previews(ws, sid)
    d = os.path.join(ws.previews, COMPARE_DIR)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        parts = n.split("-")
        if len(parts) > 2 and parts[0] == "cmp" and str(sid) in (parts[1], parts[2]):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


def preview_path(ws, quality: dict | None) -> str | None:
    if not quality or not quality.get("preview"):
        return None
    p = os.path.join(stacks_dir(ws), quality["preview"] + ".jpg")
    return p if os.path.isfile(p) else None


# ------------------------------------------------------------------ Ausrichten

def _features(a: np.ndarray) -> np.ndarray:
    """Luminanz → Sterne hervorheben (Hintergrund und Nebel abziehen, Ausreißer kappen)."""
    L = a.mean(axis=0) if a.ndim == 3 else a
    L = L - ndi.median_filter(L[::4, ::4], size=5).repeat(4, 0).repeat(4, 1)[:L.shape[0], :L.shape[1]] \
        if min(L.shape) >= 40 else L - np.median(L)
    s = float(np.median(np.abs(L))) * 1.4826 + 1e-9
    return np.clip(L / s, 0, 50)


def rotation_between(a: np.ndarray, b: np.ndarray, min_deg: float = 0.25) -> tuple[float | None, float]:
    """Winkel (Grad), um den B mit ``ndi.rotate`` gedreht werden muss, damit es auf A liegt – nur wenn die Drehung
    deutlich und sicher ist, sonst ``None``. Sternpaare liefern den Winkel bis auf ±180° (Verschiebung stört nicht);
    die Probe mit der Phasenkorrelation entscheidet zwischen beiden und schützt vor Fehltreffern."""
    from .rotation import estimate_rotation, stars_from_thumb
    La = a.mean(axis=0) if a.ndim == 3 else a
    Lb = b.mean(axis=0) if b.ndim == 3 else b
    ang, conf = estimate_rotation(stars_from_thumb(La, 80), stars_from_thumb(Lb, 80))
    if ang is None or conf < 0.3:
        return None, conf
    base = phase_shift(La[None], Lb[None])[2]
    best, score = None, 0.0
    for cand in (ang, ang - 180 if ang > 0 else ang + 180):
        if abs(cand) < min_deg:
            return None, conf                              # praktisch nicht gedreht
        q = phase_shift(La[None], ndi.rotate(Lb, cand, reshape=False, order=1, mode="nearest")[None])[2]
        if q > score:
            best, score = cand, q
    if score < 8 or score < 1.5 * base:
        return None, conf
    return float(best), conf


def phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """Verschiebung (dy, dx), um die ``b`` verschoben werden muss, damit es auf ``a`` liegt, und die Güte des
    Treffers (Spitze / Rauschen der Korrelationsfläche; unter ~8 ist das Ergebnis unsicher)."""
    fa, fb = _features(a), _features(b)
    h, w = min(fa.shape[0], fb.shape[0]), min(fa.shape[1], fb.shape[1])
    fa, fb = fa[:h, :w], fb[:h, :w]
    win = np.outer(np.hanning(h), np.hanning(w))
    A, B = np.fft.fft2(fa * win), np.fft.fft2(fb * win)
    R = A * np.conj(B)
    R /= np.abs(R) + 1e-12
    r = np.real(np.fft.ifft2(R))
    iy, ix = np.unravel_index(int(np.argmax(r)), r.shape)
    peak = float(r[iy, ix])
    conf = peak / (float(np.std(r)) + 1e-12)

    def sub(m1, c0, p1):                       # Parabel durch drei Punkte → Subpixel
        d = m1 - 2 * c0 + p1
        return 0.0 if abs(d) < 1e-12 else 0.5 * (m1 - p1) / d
    dy = iy + sub(r[(iy - 1) % h, ix], peak, r[(iy + 1) % h, ix])
    dx = ix + sub(r[iy, (ix - 1) % w], peak, r[iy, (ix + 1) % w])
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    return float(dy), float(dx), conf


def match_linear(b: np.ndarray, a: np.ndarray) -> np.ndarray:
    """B linear an A angleichen: gleicher Hintergrund (Median) und gleiche Signalhöhe (99,5-%-Perzentil).
    Das Rauschen bleibt dabei im Verhältnis erhalten – genau das soll der Vergleich zeigen."""
    out = np.empty_like(b)
    for c in range(b.shape[0]):
        ca = a[min(c, a.shape[0] - 1)]
        sa, sb = ca[::3, ::3], b[c][::3, ::3]
        ma, mb = float(np.median(sa)), float(np.median(sb))
        pa, pb = float(np.percentile(sa, 99.5)), float(np.percentile(sb, 99.5))
        k = (pa - ma) / (pb - mb) if pb - mb > 1e-9 else 1.0
        out[c] = (b[c] - mb) * k + ma
    return out


def _load_small(ws, quality: dict) -> np.ndarray:
    p = os.path.join(stacks_dir(ws), (quality or {}).get("preview", "") + ".npy")
    if not quality or not quality.get("preview") or not os.path.isfile(p):
        raise StackViewError("Für diesen Stack gibt es noch keine Vorschau (wird gerade berechnet).")
    return np.load(p, allow_pickle=False).astype(np.float32)


def compare(db: dbm.Database, ws, pid: int, a: int, b: int) -> dict:
    """Zwei Stacks vergleichen: Werte nebeneinander (besser/schlechter) und zwei ausgerichtete Vorschauen."""
    rows = {r["id"]: r for r in db.query("SELECT * FROM stacks WHERE project_id = ? AND id IN (?, ?)", (pid, a, b))}
    if a == b or a not in rows or b not in rows:
        raise StackViewError("Bitte zwei verschiedene Stacks dieses Projekts wählen.")
    qa, qb = (json.loads(rows[x]["quality_json"] or "null") or {} for x in (a, b))
    metrics = []
    for key, label, unit, better in METRICS:
        va, vb = qa.get(key), qb.get(key)
        if key == "fwhm":
            unit = qa.get("fwhm_unit") or qb.get("fwhm_unit") or "px"
            if qa.get("fwhm_unit") != qb.get("fwhm_unit") and va is not None and vb is not None:
                va, vb, unit = qa.get("fwhm_px"), qb.get("fwhm_px"), "px"
        win = None
        if better and va is not None and vb is not None and va != vb:
            rel = abs(va - vb) / max(abs(va), abs(vb), 1e-9)
            if rel >= 0.02:                                   # unter 2 % gilt als gleich
                win = ("a" if va < vb else "b") if better == "low" else ("a" if va > vb else "b")
        metrics.append(dict(key=key, label=label, unit=unit, a=va, b=vb, better=win, rule=better))
    out = dict(a=_stack_brief(rows[a], qa), b=_stack_brief(rows[b], qb), metrics=metrics, images=None, align=None)
    try:
        out.update(_render_pair(ws, a, b, qa, qb))
    except StackViewError as e:
        out["image_error"] = str(e)
    return out


def _stack_brief(r, q) -> dict:
    return dict(id=r["id"], name=r["name"], run=r["run_label"], created_at=r["created_at"], frames=r["frames"],
                hours=round((r["exposure_s"] or 0) / 3600, 2), note=r["note"], status=r["status"],
                quality_error=(q or {}).get("error"))


def compare_paths(ws, a: int, b: int, qa: dict, qb: dict) -> tuple[str, str, str]:
    key = hashlib.sha1(("%s|%s|%d" % (qa.get("preview"), qb.get("preview"), COMPARE_VERSION)).encode()).hexdigest()[:12]
    base = os.path.join(ws.previews, COMPARE_DIR, "cmp-%d-%d-%s" % (a, b, key))
    return base + "-a.jpg", base + "-b.jpg", base + ".json"


def _render_pair(ws, a: int, b: int, qa: dict, qb: dict) -> dict:
    pa, pb, meta = compare_paths(ws, a, b, qa, qb)
    version = os.path.basename(meta)[:-5]
    if os.path.isfile(pa) and os.path.isfile(pb) and os.path.isfile(meta):
        with open(meta, encoding="utf-8") as fh:
            info = json.load(fh)
    else:
        A, B = _load_small(ws, qa), _load_small(ws, qb)
        if A.shape[0] != B.shape[0]:                         # Farbe gegen Mono: beide als Luminanz
            A, B = A.mean(axis=0, keepdims=True), B.mean(axis=0, keepdims=True)
        if A.shape[1:] != B.shape[1:]:                       # unterschiedlich zugeschnitten: B auf A-Größe bringen
            H, W = A.shape[1:]
            Bp = np.zeros_like(A)
            h, w = min(H, B.shape[1]), min(W, B.shape[2])
            Bp[:, :h, :w] = B[:, :h, :w]
            B = Bp
        angle, rconf = rotation_between(A, B)
        if angle is not None:                                # gedreht (Kamera/Meridian-Flip): B zurückdrehen
            B = np.stack([ndi.rotate(ch, angle, reshape=False, order=1, mode="nearest") for ch in B])
            phi = math.radians((angle + 90) % 180 - 90)      # 180° gedreht deckt dieselbe Fläche ab
            H, W = A.shape[1:]
            my = int(math.ceil(W * abs(math.sin(phi)) / 2 + H * (1 - math.cos(phi)) / 2))
            mx = int(math.ceil(H * abs(math.sin(phi)) / 2 + W * (1 - math.cos(phi)) / 2))
            if H - 2 * my > H // 2 and W - 2 * mx > W // 2:  # nur den Bereich, den beide Bilder abdecken
                A, B = A[:, my:H - my, mx:W - mx], B[:, my:H - my, mx:W - mx]
        dy, dx, conf = phase_shift(A, B)
        ok = conf >= 8 and abs(dy) < A.shape[1] / 3 and abs(dx) < A.shape[2] / 3
        if ok and math.hypot(dy, dx) >= 0.05:
            B = np.stack([ndi.shift(ch, (dy, dx), order=1, mode="nearest") for ch in B])
            # nur den Bereich zeigen, den beide Bilder abdecken (kein Rand aus aufgefüllten Pixeln)
            H, W = A.shape[1:]
            y0, y1 = int(math.ceil(max(dy, 0))), H - int(math.ceil(max(-dy, 0)))
            x0, x1 = int(math.ceil(max(dx, 0))), W - int(math.ceil(max(-dx, 0)))
            if y1 - y0 > H // 2 and x1 - x0 > W // 2:
                A, B = A[:, y0:y1, x0:x1], B[:, y0:y1, x0:x1]
        B = match_linear(B, A)
        An, Bn = _neutral(A), _neutral(B)
        lo, m = stretch_params(An)                           # ein Strich für beide
        os.makedirs(os.path.dirname(pa), exist_ok=True)
        to_jpeg(apply_stretch(An, lo, m), pa)
        to_jpeg(apply_stretch(Bn, lo, m), pb)
        info = dict(dy=round(dy, 2), dx=round(dx, 2), confidence=round(conf, 1), aligned=bool(ok),
                    rotation=round(-angle, 2) if angle is not None else 0.0,      # B gegenüber A
                    width=int(A.shape[2]), height=int(A.shape[1]))
        from .previews import _atomic_write
        _atomic_write(meta, json.dumps(info).encode("utf-8"))
    return dict(images=dict(a=pa, b=pb, version=version), align=info)
