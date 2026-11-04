"""Bilddaten laden (FITS/XISF) und Bayer-Kanäle trennen."""
from __future__ import annotations

import io

import numpy as np

from . import xisf

FULL_SCALE = 65535.0


def load(path: str, data: bytes | None = None) -> np.ndarray:
    """Bild als float32 in ADU (0…65535). XISF-Float [0,1] wird hochskaliert. Form: (H, W) oder (C, H, W).

    Mit ``data`` (bereits gelesener Dateiinhalt) wird die Datei nicht noch einmal gelesen; ``path`` bestimmt
    dann nur das Format.
    """
    low = path.lower()
    if low.endswith(".xisf"):
        a = xisf.read_image(path if data is None else data).astype(np.float32)
        if a.max() <= 1.0 + 1e-6:
            a = a * FULL_SCALE
        return a[0] if a.shape[0] == 1 else a
    from astropy.io import fits
    src = path if data is None else io.BytesIO(data)
    with fits.open(src, memmap=False) as hd:
        a = hd[0].data
    a = np.asarray(a, dtype=np.float32)
    if a.max() <= 1.0 + 1e-6 and a.dtype.kind == "f":
        a = a * FULL_SCALE
    return a


_OFFSETS = {  # Position von (R, G1, G2, B) im 2×2-Block (zeile, spalte)
    "RGGB": ((0, 0), (0, 1), (1, 0), (1, 1)),
    "BGGR": ((1, 1), (0, 1), (1, 0), (0, 0)),
    "GRBG": ((0, 1), (0, 0), (1, 1), (1, 0)),
    "GBRG": ((1, 0), (0, 0), (1, 1), (0, 1)),
}


def split_cfa(a: np.ndarray, bayer: str | None) -> dict[str, np.ndarray]:
    """CFA-Rohbild → {'R','G','B'} in Superpixel-Auflösung. Mono → {'L'}."""
    if not bayer or bayer.upper() not in _OFFSETS:
        return {"L": a}
    (ry, rx), (g1y, g1x), (g2y, g2x), (by, bx) = _OFFSETS[bayer.upper()]
    h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
    a = a[:h, :w]
    return {"R": a[ry::2, rx::2], "G": (a[g1y::2, g1x::2] + a[g2y::2, g2x::2]) / 2, "B": a[by::2, bx::2]}


def radius_map(shape, step: int = 1) -> np.ndarray:
    h, w = shape
    yy, xx = np.mgrid[0:h:step, 0:w:step]
    return np.hypot(yy - h / 2, xx - w / 2) / np.hypot(h / 2, w / 2)
