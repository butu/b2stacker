"""Hochaufgelöste Vorschau einzelner Rohbilder (für die Lightbox) – bei Bedarf erzeugt und zwischengespeichert."""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading

import numpy as np

from . import fitsmeta, imageio
from .stackqa import stretch

# Rendering is RAM/CPU heavy; cache hits never take a slot.
_RENDER_SLOTS = threading.BoundedSemaphore(2)


class _BadThumbnail(Exception):
    """Analysis thumbnail unreadable or malformed; callers fall back to the raw preview."""


def _atomic_write(out: str, data: bytes) -> None:
    # Own temp file + os.replace so readers never see a half-written JPEG.
    d = os.path.dirname(out)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _jpeg_bytes(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def _step(n: int, width: int) -> int:
    # Bound working arrays before color conversion and stretching.
    return max(1, -(-n // width))


def _cfa_planes(raw: np.ndarray, bayer: str, k: int) -> list[np.ndarray]:
    """Preserve CFA phase while sampling before expensive full-size color conversion."""
    h, w = raw.shape[0] // 2 * 2, raw.shape[1] // 2 * 2
    (ry, rx), (g1y, g1x), (g2y, g2x), (by, bx) = imageio._OFFSETS[bayer.upper()]

    def pick(y, x):
        return raw[y:h:2 * k, x:w:2 * k]
    planes = [pick(ry, rx), pick(g1y, g1x), pick(g2y, g2x), pick(by, bx)]
    n0 = min(p.shape[0] for p in planes)   # offsets can differ by one sample at the edge
    n1 = min(p.shape[1] for p in planes)
    r, g1, g2, b = (p[:n0, :n1] for p in planes)
    return [r, (g1 + g2) * 0.5, b]


def frame_preview(path: str, cache_dir: str, width: int = 1800) -> str:
    """Downsample before stretching and reject renders whose source changed while loading."""
    from .catalog import file_sig
    sig0 = file_sig(path)
    key = hashlib.sha1(("%s|%s|%d" % (os.path.abspath(path), sig0, width)).encode()).hexdigest()[:20]
    out = os.path.join(cache_dir, key + ".jpg")
    if os.path.exists(out):
        return out
    from PIL import Image
    with _RENDER_SLOTS:
        if os.path.exists(out):
            return out
        meta = fitsmeta.read_meta(path)
        raw = imageio.load(path)
        if raw.ndim == 3:
            k = _step(max(raw.shape[1:]), width)
            img = raw[:, ::k, ::k].astype(np.float32) / 65535.0
        elif meta.bayer and meta.bayer.upper() in imageio._OFFSETS:
            k = _step(max(raw.shape) // 2, width)
            r, g, b = _cfa_planes(raw, meta.bayer, k)
            img = np.stack([r, g, b]).astype(np.float32) / 65535.0
            med = np.median(img[:, ::8, ::8].reshape(3, -1), axis=1)
            img = img * (med[1] / np.maximum(med, 1e-9))[:, None, None]     # rough white balance
        else:
            k = _step(max(raw.shape), width)
            img = raw[::k, ::k][None].astype(np.float32) / 65535.0
        st = stretch(img, target=0.2)
        arr = (np.moveaxis(st, 0, -1) * 255).astype(np.uint8) if st.shape[0] == 3 else (st[0] * 255).astype(np.uint8)
        im = Image.fromarray(arr)
        im.thumbnail((width, width), Image.LANCZOS)
        data = _jpeg_bytes(im)
        if file_sig(path) != sig0:
            raise RuntimeError("Rohbild während des Renderns geändert: %s" % os.path.basename(path))
        _atomic_write(out, data)
    return out


def _valid_analysis_thumb(path: str, analysis_dir: str, sig: str):
    """Path to the analysis thumbnail (.npy), only if metrics.csv matches the current file."""
    from . import analyze, catalog as cat
    row = analyze._read_csv_rows(os.path.join(analysis_dir, "metrics.csv")).get(path)
    if not row or row.get("file_sig") != sig or row.get("metrics_ver") != str(cat.LIGHT_VERSION):
        return None
    npy = os.path.join(analysis_dir, "thumbs", analyze.thumb_name(path))
    return npy if os.path.isfile(npy) else None


def _load_thumb(npy: str) -> np.ndarray:
    try:
        a = np.load(npy, allow_pickle=False)
    except (OSError, ValueError) as e:
        raise _BadThumbnail(str(e)) from e
    if a.ndim != 2 or a.size == 0 or a.dtype.kind not in "fiu":
        raise _BadThumbnail("shape %s dtype %s" % (a.shape, a.dtype))
    if a.dtype.kind == "f" and not np.isfinite(a).all():
        raise _BadThumbnail("non-finite values")
    return a


def _jpeg_from_thumb(a: np.ndarray, width: int) -> bytes:
    from PIL import Image
    st = stretch(a.astype(np.float32) / 65535.0, target=0.2)
    im = Image.fromarray((st * 255).astype(np.uint8), mode="L")
    im.thumbnail((width, width), Image.LANCZOS)
    return _jpeg_bytes(im)


def frame_thumbnail(path: str, analysis_dir: str, width: int = 320) -> str:
    """Reuse current analysis thumbnails to avoid loading full originals for grid tiles."""
    from .catalog import file_sig
    sig = file_sig(path)
    out_dir = os.path.join(analysis_dir, "frame-thumbs")
    npy = _valid_analysis_thumb(path, analysis_dir, sig)
    if npy is not None:
        try:
            stamp = file_sig(npy)
        except OSError:
            npy = None
    if npy is not None:
        key = hashlib.sha1(("%s|%s|%d|%s" % (os.path.abspath(path), sig, width, stamp)).encode()).hexdigest()[:20]
        out = os.path.join(out_dir, key + ".jpg")
        if os.path.exists(out):
            return out
        try:
            arr = _load_thumb(npy)
        except _BadThumbnail:
            npy = None
    if npy is None:
        return frame_preview(path, out_dir, width=width)
    with _RENDER_SLOTS:
        if os.path.exists(out):
            return out
        data = _jpeg_from_thumb(arr, width)
        if file_sig(path) != sig or file_sig(npy) != stamp:
            raise RuntimeError("Analysis state changed during rendering: %s" % os.path.basename(path))
        _atomic_write(out, data)
    return out
