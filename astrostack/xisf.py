"""Minimaler XISF-Leser/-Schreiber (monolithisch, unkomprimiert, Float32/UInt16).

Reicht für PixInsight-Masters und eigene Ausgaben; komprimierte XISF werden erkannt und abgelehnt.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

import numpy as np

_SIG = b"XISF0100"
_DTYPES = {"Float32": "<f4", "Float64": "<f8", "UInt16": "<u2", "UInt8": "u1", "UInt32": "<u4"}


def _xml(path: str) -> tuple[str, int]:
    with open(path, "rb") as f:
        if f.read(8) != _SIG:
            raise ValueError("keine XISF-Datei: " + path)
        hl = int.from_bytes(f.read(4), "little")
        f.read(4)
        return f.read(hl).decode("utf-8", "replace"), 16 + hl


def _images(xml: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<Image\s([^>]*)>", xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        out.append(attrs)
    return out


def read_header(path: str) -> dict:
    """FITS-Keywords + Geometrie des ersten Bildes als dict (wie ein FITS-Header)."""
    xml, _ = _xml(path)
    h: dict = {}
    for name, value in re.findall(r'<FITSKeyword name="([^"]+)" value="([^"]*)"', xml):
        v = value.strip().strip("'").strip()
        if name in h or name in ("HISTORY", "COMMENT"):
            continue
        try:
            h[name] = float(v) if re.fullmatch(r"[-+]?\d+(\.\d*)?([eE][-+]?\d+)?", v) else v
        except ValueError:
            h[name] = v
    imgs = _images(xml)
    if imgs:
        g = imgs[0].get("geometry", "")
        parts = g.split(":")
        if len(parts) >= 2:
            h.setdefault("NAXIS1", int(parts[0]))
            h.setdefault("NAXIS2", int(parts[1]))
            if len(parts) > 2:
                h.setdefault("NAXIS3", int(parts[2]))
        h["_XISF_IMAGES"] = [i.get("id", "") for i in imgs]
    return h


def read_image(path: str, index: int = 0) -> np.ndarray:
    """Bild Nr. ``index`` als Array (Kanäle, Höhe, Breite)."""
    xml, _ = _xml(path)
    imgs = _images(xml)
    a = imgs[index]
    if "compression" in a:
        raise ValueError("komprimierte XISF wird nicht unterstützt: " + path)
    w, h, *c = (int(x) for x in a["geometry"].split(":"))
    ch = c[0] if c else 1
    off, size = (int(x) for x in a["location"].split(":")[1:3])
    dt = np.dtype(_DTYPES[a.get("sampleFormat", "Float32")])
    with open(path, "rb") as f:
        f.seek(off)
        data = np.frombuffer(f.read(size), dtype=dt)
    return data.reshape(ch, h, w)


def write_image(path: str, img: np.ndarray, keywords: dict | None = None, color: bool | None = None) -> None:
    """Float32-Bild (H×W oder C×H×W) als XISF schreiben. Werte sollten in [0,1] liegen."""
    a = np.asarray(img, dtype="<f4")
    if a.ndim == 2:
        a = a[None]
    ch, h, w = a.shape
    space = "RGB" if (color if color is not None else ch == 3) else "Gray"
    kw = "".join('<FITSKeyword name="%s" value="%s" comment=""/>' % (escape(k), escape(str(v)))
                 for k, v in (keywords or {}).items())
    size = a.nbytes

    def header(off: int) -> bytes:
        return ('<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" xmlns="http://www.pixinsight.com/xisf">'
                '<Image geometry="%d:%d:%d" sampleFormat="Float32" bounds="0:1" colorSpace="%s" '
                'pixelStorage="Planar" location="attachment:%d:%d">%s</Image>'
                '<Metadata><Property id="XISF:CreatorApplication" type="String">AstroStack</Property></Metadata>'
                '</xisf>' % (w, h, ch, space, off, size, kw)).encode("utf-8")

    off = 16 + len(header(0)) + 64
    off = (off + 4095) // 4096 * 4096
    hdr = header(off)
    with open(path, "wb") as f:
        f.write(_SIG)
        f.write(len(hdr).to_bytes(4, "little"))
        f.write(b"\0" * 4)
        f.write(hdr)
        f.write(b"\0" * (off - 16 - len(hdr)))
        f.write(np.ascontiguousarray(a).tobytes())
