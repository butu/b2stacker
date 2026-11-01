"""Hochaufgelöste Vorschau einzelner Rohbilder (für die Lightbox) – bei Bedarf erzeugt und zwischengespeichert."""
from __future__ import annotations

import hashlib
import io
import os

import numpy as np

from . import fitsmeta, imageio
from .stackqa import stretch


def frame_preview(path: str, cache_dir: str, width: int = 1800) -> str:
    """Rohbild → gestrecktes Farb-/Graubild als JPEG (Superpixel-Debayer, ~1800 px breit)."""
    key = hashlib.sha1(("%s|%s|%d" % (os.path.abspath(path), os.path.getmtime(path), width)).encode()).hexdigest()[:20]
    out = os.path.join(cache_dir, key + ".jpg")
    if os.path.exists(out):
        return out
    from PIL import Image
    meta = fitsmeta.read_meta(path)
    raw = imageio.load(path)
    if raw.ndim == 3:
        img = raw / 65535.0
    else:
        ch = imageio.split_cfa(raw, meta.bayer)
        if "G" in ch:
            img = np.stack([ch["R"], ch["G"], ch["B"]]) / 65535.0
            med = np.median(img[:, ::8, ::8].reshape(3, -1), axis=1)
            img = img * (med[1] / np.maximum(med, 1e-9))[:, None, None]     # grob weißabgleichen
        else:
            img = ch["L"][None] / 65535.0
    st = stretch(img.astype(np.float32), target=0.2)
    arr = (np.moveaxis(st, 0, -1) * 255).astype(np.uint8) if st.shape[0] == 3 else (st[0] * 255).astype(np.uint8)
    im = Image.fromarray(arr)
    im.thumbnail((width, width), Image.LANCZOS)
    os.makedirs(cache_dir, exist_ok=True)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=86)
    with open(out, "wb") as fh:
        fh.write(buf.getvalue())
    return out
