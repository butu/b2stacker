"""Bilder für die neue Oberfläche: Kachel-Thumbnails und große Vorschauen einzelner Frames.

- Thumbnail: aus dem bei der Vermessung gespeicherten ``.npy`` (``frame_metrics.thumb`` in
  ``system/previews/thumbs``) – das Original wird dafür nie gelesen. Gerendert wird einmal als JPEG nach
  ``system/previews/thumbs-jpg``; der Name enthält den Thumbnail-Namen und die Render-Version.
- Vorschau: ``previews.frame_preview`` liest das Original (nur auf ausdrücklichen Klick) und legt das JPEG in
  ``system/previews/frames`` ab; der Dateiname enthält Pfad und Dateistempel, ändert sich das Original, entsteht
  ein neues JPEG.

Ausgeliefert werden nur Dateien, die zu einem Projekt gehören (``project_files``); beliebige Pfade sind nicht
erreichbar, weil die Oberfläche nur Datei-IDs schickt.
"""
from __future__ import annotations

import os

import numpy as np

from .. import previews
from ..flow.measure import THUMBS
from ..stackqa import stretch

RENDER_VERSION = 1            # erhöhen, wenn sich die gerenderten Pixel ändern
THUMB_WIDTH = 320


class MediaError(Exception):
    """Bild nicht verfügbar; ``status`` ist der HTTP-Code, die Nachricht Klartext."""

    def __init__(self, msg: str, status: int = 404):
        super().__init__(msg)
        self.status = status


def _known_file(db, fid: int):
    row = db.one("SELECT f.id, f.path, f.missing_since, m.thumb FROM files f "
                 "LEFT JOIN frame_metrics m ON m.file_id = f.id "
                 "WHERE f.id = ? AND EXISTS (SELECT 1 FROM project_files pf WHERE pf.file_id = f.id)", (fid,))
    if row is None:
        raise MediaError("Datei nicht gefunden.")
    return row


def _render_thumb(npy: str, width: int) -> bytes:
    try:
        a = np.load(npy, allow_pickle=False)
    except (OSError, ValueError) as e:
        raise MediaError("Thumbnail unlesbar: %s" % e, 422) from None
    if a.ndim != 2 or a.size == 0 or a.dtype.kind not in "fiu":
        raise MediaError("Thumbnail unlesbar (Form %s)." % (a.shape,), 422)
    # float16-Thumbnails laufen bei gesättigten Sternen über (65535 → inf)
    a = np.nan_to_num(a.astype(np.float32), nan=0.0, posinf=65535.0, neginf=0.0)
    if float(a.max()) > 1.0:
        a = a / 65535.0
    from PIL import Image
    im = Image.fromarray((stretch(np.clip(a, 0, 1), target=0.2) * 255).astype(np.uint8), mode="L")
    im.thumbnail((width, width), Image.LANCZOS)
    return previews._jpeg_bytes(im)


def thumb(ws, db, fid: int) -> tuple[str, str]:
    """Thumbnail-JPEG eines Frames (gerendert und zwischengespeichert). → (Pfad, Version für ETag)"""
    row = _known_file(db, fid)
    name = row["thumb"]
    if not name or os.path.basename(name) != name:
        raise MediaError("Für diesen Frame gibt es noch kein Thumbnail (Vermessung läuft oder steht aus).")
    npy = os.path.join(ws.previews, THUMBS, name)
    if not os.path.isfile(npy):
        raise MediaError("Thumbnail fehlt – wird bei der nächsten Prüfung neu erstellt.")
    version = "%s-r%d-w%d" % (os.path.splitext(name)[0], RENDER_VERSION, THUMB_WIDTH)
    out = os.path.join(ws.previews, "thumbs-jpg", version + ".jpg")
    if not os.path.isfile(out):
        previews._atomic_write(out, _render_thumb(npy, THUMB_WIDTH))
    return out, version


def preview(ws, db, fid: int) -> tuple[str, str]:
    """Große Vorschau eines Frames aus dem Original. → (Pfad, Version für ETag)"""
    row = _known_file(db, fid)
    if row["missing_since"] or not os.path.isfile(row["path"]):
        raise MediaError("Originaldatei nicht erreichbar: %s" % row["path"])
    try:
        out = previews.frame_preview(row["path"], os.path.join(ws.previews, "frames"))
    except (OSError, ValueError, RuntimeError) as e:
        raise MediaError("Vorschau nicht möglich: %s" % e, 422) from None
    return out, os.path.splitext(os.path.basename(out))[0]
