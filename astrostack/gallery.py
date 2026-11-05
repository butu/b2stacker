"""Galerie eines Projekts: bearbeitete und fremde Bilder (JPEG, PNG, TIFF, PSD, XISF, FITS, alte Masters).

- Hinzufügen per Auswahl (mehrere Dateien oder ganze Ordner auf einmal): die Dateien bleiben, wo sie sind;
  b2stacker merkt sich nur den Pfad (``assets`` mit ``owned = 0``) und löscht oder verändert sie nie.
- Was der Nutzer selbst in den Projektordner legt, findet der Watcher (``scan_project_dir``). Liegt die Datei in
  einem Stack-Ordner, gehört sie zu diesem Stack, sonst zum ganzen Projekt.
- „Aus der Galerie entfernen“ vergisst nur den Eintrag (``hidden = 1``); die Datei bleibt.
- Vorschaubilder entstehen einmal im Cache (``system/previews/assets``): JPEG/PNG/TIFF/PSD über Pillow,
  XISF/FITS über die eigenen Leser mit automatischem Strich. Ein unlesbares Format bekommt ein Symbol.
"""
from __future__ import annotations

import hashlib
import io
import json
import os

import numpy as np

from . import db as dbm

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".xisf", ".fit", ".fits", ".fts")
SKIP_DIRS = {"protokoll", "masters", "logs", ".git", "__pycache__"}
THUMB_W = 640
THUMB_VERSION = 1
MAX_FILES = 2000                    # je Hinzufügen – schützt vor versehentlich gewähltem Laufwerk


class GalleryError(ValueError):
    pass


def is_image(name: str) -> bool:
    n = name.lower()
    return n.endswith(IMAGE_EXT) and not n.startswith((".", "~$")) and not n.endswith(".tmp.jpg")


def _norm(path: str) -> str:
    return os.path.abspath(os.path.expanduser((path or "").strip().strip('"')))


def _walk(root: str, limit: int = MAX_FILES) -> list[str]:
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            if is_image(f):
                out.append(os.path.join(dirpath, f))
                if len(out) >= limit:
                    return out
    return out


def stack_for(db: dbm.Database, pdir: str, pid: int, path: str) -> int | None:
    """Liegt die Datei in einem Stack-Ordner dieses Projekts? → Stack-ID"""
    try:
        rel = os.path.relpath(path, pdir)
    except ValueError:                       # anderes Laufwerk
        return None
    if rel.startswith(".."):
        return None
    top = rel.replace("\\", "/").split("/")[0]
    if top == os.path.basename(path):
        return None
    sid = db.value("SELECT id FROM stacks WHERE project_id = ? AND folder = ? COLLATE NOCASE AND folder != ''",
                   (pid, top))
    return int(sid) if sid else None


def _register(db: dbm.Database, pid: int, path: str, stack_id: int | None, revive: bool) -> str:
    """→ 'new' | 'back' | 'known'"""
    try:
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size = mtime = None
    row = db.one("SELECT id, hidden, missing_since, owned, stack_id FROM assets WHERE project_id = ? AND path = ?",
                 (pid, path))
    if row is None:
        db.execute("INSERT INTO assets (project_id, stack_id, path, size, mtime_ns, added_at, owned) "
                   "VALUES (?, ?, ?, ?, ?, ?, 0)", (pid, stack_id, path, size, mtime, dbm.now()))
        return "new"
    if row["owned"]:
        return "known"
    if row["hidden"] and not revive:
        return "known"
    changed = bool(row["hidden"] or row["missing_since"])
    db.execute("UPDATE assets SET hidden = 0, missing_since = NULL, size = ?, mtime_ns = ?, "
               "stack_id = COALESCE(stack_id, ?) WHERE id = ?", (size, mtime, stack_id, row["id"]))
    return "back" if changed else "known"


def add(db: dbm.Database, ws, pid: int, paths: list[str], origin: str = "ui") -> dict:
    """Dateien und/oder ganze Ordner zur Galerie hinzufügen (die Dateien bleiben, wo sie sind)."""
    proj = db.one("SELECT folder FROM projects WHERE id = ?", (pid,))
    if not proj:
        raise GalleryError("Projekt nicht gefunden.")
    pdir = ws.project_dir(proj["folder"])
    files, missing, unsupported = [], [], []
    for raw in paths or []:
        p = _norm(raw)
        if not raw or not raw.strip():
            continue
        if os.path.isdir(p):
            files += _walk(p, MAX_FILES - len(files))
        elif os.path.isfile(p):
            (files if is_image(p) else unsupported).append(p)
        else:
            missing.append(p)
        if len(files) >= MAX_FILES:
            break
    if not files and (missing or unsupported):
        raise GalleryError(("Nicht gefunden: " + ", ".join(missing)) if missing else
                           "Kein unterstütztes Bild (JPEG, PNG, TIFF, PSD, XISF, FITS): " + ", ".join(unsupported))
    res = dict(new=0, back=0, known=0, missing=missing, unsupported=unsupported)
    with db.transaction():
        for f in files:
            res[_register(db, pid, f, stack_for(db, pdir, pid, f), revive=True)] += 1
        if res["new"] or res["back"]:
            db.event("assets_added", pid, json.dumps(dict(n=res["new"] + res["back"], origin=origin,
                                                          names=[os.path.basename(f) for f in files[:3]])))
    return res


def scan_project_dir(db: dbm.Database, ws, pid: int) -> dict:
    """Projektordner nach neuen Bildern durchsuchen (Watcher) und fehlende markieren."""
    proj = db.one("SELECT folder FROM projects WHERE id = ?", (pid,))
    if not proj:
        return dict(new=0, back=0, missing=0)
    pdir = ws.project_dir(proj["folder"])
    found = set(_walk(pdir, 20000)) if os.path.isdir(pdir) else set()
    res = dict(new=0, back=0, known=0, missing=0)
    now = dbm.now()
    with db.transaction():
        for f in sorted(found):
            res[_register(db, pid, f, stack_for(db, pdir, pid, f), revive=False)] += 1
        inside = os.path.normcase(os.path.join(pdir, ""))
        for r in db.query("SELECT id, path FROM assets WHERE project_id = ? AND missing_since IS NULL", (pid,)):
            if not os.path.normcase(r["path"]).startswith(inside):
                continue                                   # außerhalb: nur check_missing (kann offline sein)
            if r["path"] not in found and not os.path.exists(r["path"]):
                db.execute("UPDATE assets SET missing_since = ? WHERE id = ?", (now, r["id"]))
                res["missing"] += 1
        if res["new"] or res["back"]:
            db.event("assets_added", pid, json.dumps(dict(n=res["new"] + res["back"], origin="watcher")))
    res.pop("known")
    return res


def check_missing(db: dbm.Database, pid: int) -> int:
    """Fremde Bilder außerhalb des Projektordners prüfen (nur Existenz). → Anzahl geänderter Einträge"""
    n = 0
    now = dbm.now()
    with db.transaction():
        for r in db.query("SELECT id, path, missing_since FROM assets WHERE project_id = ? AND owned = 0 AND hidden = 0",
                          (pid,)):
            there = os.path.exists(r["path"])
            if there and r["missing_since"]:
                db.execute("UPDATE assets SET missing_since = NULL WHERE id = ?", (r["id"],))
                n += 1
            elif not there and not r["missing_since"]:
                db.execute("UPDATE assets SET missing_since = ? WHERE id = ?", (now, r["id"]))
                n += 1
    return n


def listing(db: dbm.Database, pid: int, pdir: str | None = None) -> list[dict]:
    """Galerie-Bilder (neueste zuerst) mit ``place``: Stack, Projektordner oder der Ordner, in dem das Bild liegt."""
    inside = os.path.normcase(os.path.join(pdir, "")) if pdir else None
    rows = db.query("SELECT a.id, a.path, a.size, a.mtime_ns, a.added_at, a.missing_since, a.stack_id, "
                    "s.name AS stack_name, p.cover_asset_id = a.id AS cover FROM assets a "
                    "JOIN projects p ON p.id = a.project_id LEFT JOIN stacks s ON s.id = a.stack_id "
                    "WHERE a.project_id = ? AND a.owned = 0 AND a.hidden = 0 ORDER BY a.added_at DESC, a.id DESC",
                    (pid,))
    out = []
    for r in rows:
        d = dict(r)
        d["name"] = os.path.basename(r["path"])
        d["ext"] = os.path.splitext(r["path"])[1].lower().lstrip(".")
        d["cover"] = bool(r["cover"])
        d["missing"] = bool(r["missing_since"])
        d["version"] = thumb_version(r)
        if r["stack_name"]:
            d["place"] = "Stack " + r["stack_name"]
        elif inside and os.path.normcase(r["path"]).startswith(inside):
            d["place"] = "Projektordner"
        else:
            d["place"] = os.path.basename(os.path.dirname(r["path"])) or os.path.dirname(r["path"])
        out.append(d)
    return out


def hide(db: dbm.Database, pid: int, aid: int) -> None:
    with db.transaction():
        cur = db.execute("UPDATE assets SET hidden = 1 WHERE id = ? AND project_id = ? AND owned = 0", (aid, pid))
        if not cur.rowcount:
            raise GalleryError("Bild nicht in der Galerie.")
        db.execute("UPDATE projects SET cover_asset_id = NULL WHERE id = ? AND cover_asset_id = ?", (pid, aid))


def set_cover(db: dbm.Database, pid: int, aid: int | None) -> None:
    with db.transaction():
        if aid is not None and not db.value("SELECT 1 FROM assets WHERE id = ? AND project_id = ? AND hidden = 0",
                                            (aid, pid)):
            raise GalleryError("Bild gehört nicht zu diesem Projekt.")
        db.execute("UPDATE projects SET cover_asset_id = ? WHERE id = ?", (aid, pid))
        db.event("cover_set", pid, json.dumps(dict(asset=aid)))


# ------------------------------------------------------------------ Vorschaubilder

def thumb_version(row) -> str:
    return hashlib.sha1(("%s|%s|%s|%d" % (row["path"], row["size"], row["mtime_ns"], THUMB_VERSION)).encode()
                        ).hexdigest()[:16]


def _jpeg(im, width: int) -> bytes:
    from PIL import Image
    im.thumbnail((width, width), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _from_array(a: np.ndarray, width: int) -> bytes:
    from PIL import Image

    from .stackview import _neutral, apply_stretch, shrink, stretch_params
    a = np.nan_to_num(np.asarray(a, dtype=np.float32))
    if a.ndim == 2:
        a = a[None]
    if a.shape[0] not in (1, 3):
        a = a[:1]
    a = shrink(a, width * 2)
    hi = float(a.max())
    if hi > 1.5:
        a = a / (65535.0 if hi <= 65535 else hi)
    a = _neutral(np.clip(a, 0, 1))
    s = apply_stretch(a, *stretch_params(a))
    arr = (np.moveaxis(s, 0, -1) * 255).astype(np.uint8) if s.shape[0] == 3 else (s[0] * 255).astype(np.uint8)
    return _jpeg(Image.fromarray(arr), width)


def render(path: str, width: int = THUMB_W) -> bytes:
    """Vorschau-JPEG einer Bilddatei. Wirft eine Ausnahme, wenn das Format nicht lesbar ist."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xisf":
        from . import xisf
        return _from_array(xisf.read_image(path, 0), width)
    if ext in (".fit", ".fits", ".fts"):
        from astropy.io import fits
        with fits.open(path, memmap=False) as hd:
            data = next(h.data for h in hd if getattr(h, "data", None) is not None)
        return _from_array(np.asarray(data, dtype=np.float32), width)
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 400_000_000          # große Panoramen sind kein Angriff
    with Image.open(path) as im:
        im.load()
        if im.mode in ("I", "I;16", "I;16B", "I;16L", "F") or im.mode.startswith("I;"):
            arr = np.asarray(im, dtype=np.float32)
            lo, hi = np.percentile(arr, [0.5, 99.5])
            arr = np.clip((arr - lo) / max(float(hi - lo), 1e-6), 0, 1)
            return _jpeg(Image.fromarray((arr * 255).astype(np.uint8)), width)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        return _jpeg(im.copy(), width)


VIEW_W = 1800


def thumb(db: dbm.Database, ws, aid: int, width: int = THUMB_W) -> tuple[str, str]:
    """Vorschaubild (``THUMB_W``) oder Großansicht (``VIEW_W``) eines Galerie-Bilds bzw. Stack-Masters aus dem
    Cache. → (Pfad, Version)"""
    from .previews import _atomic_write
    row = db.one("SELECT id, path, size, mtime_ns, missing_since, owned, stack_id FROM assets WHERE id = ?", (aid,))
    if row is None:
        raise FileNotFoundError("Bild nicht gefunden.")
    if row["owned"] and row["stack_id"]:                   # Stack-Master: die Stack-Vorschau nehmen
        from . import stackview
        q = json.loads(db.value("SELECT quality_json FROM stacks WHERE id = ?", (row["stack_id"],)) or "null")
        p = stackview.preview_path(ws, q)
        if p:
            return p, os.path.basename(p)[:-4]
    version = thumb_version(row)
    out = os.path.join(ws.previews, "assets", "%d-%s%s.jpg" % (aid, version, "" if width == THUMB_W else "-w%d" % width))
    if os.path.isfile(out):
        return out, version
    if row["missing_since"] or not os.path.isfile(row["path"]):
        raise FileNotFoundError("Datei fehlt: " + row["path"])
    try:
        st = os.stat(row["path"])
        if (st.st_size, st.st_mtime_ns) != (row["size"], row["mtime_ns"]):     # geändert → neue Version
            with db.transaction():
                db.execute("UPDATE assets SET size = ?, mtime_ns = ? WHERE id = ?", (st.st_size, st.st_mtime_ns, aid))
            return thumb(db, ws, aid, width)
    except OSError:
        pass
    _atomic_write(out, render(row["path"], width))
    return out, version


def relocate(db: dbm.Database, pid: int, old_dir: str, new_dir: str) -> None:
    """Projektordner umbenannt: gespeicherte Pfade darunter mitziehen (Galerie, Stack-Dateien, Masters)."""
    old = os.path.join(old_dir, "")
    with db.transaction():
        for table in ("assets", "masters"):
            for r in db.query("SELECT id, path FROM %s WHERE project_id = ?" % table, (pid,)):
                if os.path.normcase(r["path"]).startswith(os.path.normcase(old)):
                    db.execute("UPDATE %s SET path = ? WHERE id = ?" % table,
                               (os.path.join(new_dir, r["path"][len(old):]), r["id"]))


# ------------------------------------------------------------------ Bild-Infos für die Großansicht

INFO_VERSION = 1
_EXIF = {306: "Datum", 305: "Software", 271: "Hersteller", 272: "Kamera", 315: "Autor", 270: "Beschreibung",
         33432: "Copyright"}


def _load_any(path: str) -> tuple[np.ndarray, dict, dict]:
    """→ (Bild als float32 [K, H, W], Header, Format-Infos)"""
    ext = os.path.splitext(path)[1].lower()
    fmt: dict = {}
    if ext == ".xisf":
        from . import xisf
        a = np.asarray(xisf.read_image(path, 0))
        hdr = dict(xisf.read_header(path))
        fmt["format"] = "XISF"
    elif ext in (".fit", ".fits", ".fts"):
        from astropy.io import fits
        with fits.open(path, memmap=False) as hd:
            h = next(x for x in hd if getattr(x, "data", None) is not None)
            a = np.asarray(h.data)
            hdr = {k: v for k, v in dict(hd[0].header).items() if k not in ("COMMENT", "HISTORY", "")}
            hdr.update({k: v for k, v in dict(h.header).items() if k not in ("COMMENT", "HISTORY", "")})
        fmt["format"] = "FITS"
    else:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 400_000_000
        with Image.open(path) as im:
            fmt["format"] = im.format or ext.lstrip(".").upper()
            fmt["mode"] = im.mode
            try:
                ex = im.getexif()
                hdr = {_EXIF[k]: str(v)[:200] for k, v in ex.items() if k in _EXIF}
            except Exception:  # noqa: BLE001
                hdr = {}
            if im.mode not in ("RGB", "L", "F", "I") and not im.mode.startswith("I;"):
                im = im.convert("RGB")
            a = np.asarray(im)
        fmt["bits"] = {"1": 1, "L": 8, "P": 8, "RGB": 8, "RGBA": 8, "I;16": 16, "I;16B": 16, "I;16L": 16,
                       "I": 32, "F": 32}.get(fmt.get("mode"), None)
        if a.ndim == 3:
            a = np.moveaxis(a[..., :3], -1, 0)
    fmt.setdefault("bits", {1: 8, 2: 16, 4: 32, 8: 64}.get(a.dtype.itemsize))
    fmt["float"] = bool(np.issubdtype(a.dtype, np.floating))
    a = np.asarray(a, dtype=np.float32)
    if a.ndim == 2:
        a = a[None]
    return a, hdr, fmt


def _hdr_public(hdr: dict) -> dict:
    out = {}
    for k, v in list(hdr.items())[:300]:
        if isinstance(v, (bytes, bytearray)):
            continue
        s = v if isinstance(v, (int, float, bool)) else str(v)[:200]
        if isinstance(s, float) and s != s:
            continue
        out[str(k)] = s
    return out


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def info(db: dbm.Database, ws, aid: int, pixscale: float | None = None) -> dict:
    """Alles zu einem Galerie-Bild: Pixelmaße, Kanäle, Bittiefe, Header bzw. EXIF und gemessene Werte (Sterne,
    FWHM, Rundheit, Rauschen, Hintergrund). Ergebnis wird je Dateiversion zwischengespeichert."""
    row = db.one("SELECT id, path, size, mtime_ns, missing_since FROM assets WHERE id = ?", (aid,))
    if row is None:
        raise FileNotFoundError("Bild nicht gefunden.")
    version = thumb_version(row)
    cache = os.path.join(ws.previews, "assets", "%d-%s-info%d.json" % (aid, version, INFO_VERSION))
    try:
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        pass
    if row["missing_since"] or not os.path.isfile(row["path"]):
        raise FileNotFoundError("Datei fehlt: " + row["path"])
    a, hdr, fmt = _load_any(row["path"])
    k, h, w = a.shape
    out = dict(width=int(w), height=int(h), channels=int(k), megapixel=round(w * h / 1e6, 1), **fmt,
               header=_hdr_public(hdr))
    hu = {str(x).upper(): y for x, y in hdr.items()}
    px, fl = _num(hu.get("XPIXSZ")), _num(hu.get("FOCALLEN"))
    scale = (206.265 * px / fl) if (px and fl) else pixscale
    out["pixscale"] = round(scale, 3) if scale else None
    if scale:
        out["field"] = "%.0f′ × %.0f′" % (w * scale / 60, h * scale / 60)
    try:
        from . import stackqa
        b = np.nan_to_num(a)
        hi = float(b.max()) if b.size else 1.0
        if hi > 1.5:
            b = b / (65535.0 if hi <= 65535 else hi)
        q = stackqa.kpis(np.clip(b, 0, 1), scale)
        out["measured"] = {x: q.get(x) for x in ("stars", "fwhm_px", "fwhm_as", "hfr_px", "ecc", "noise", "bg",
                                                 "snr_bg", "gradient_pct")}
        out["linear"] = bool(fmt.get("float") and (q.get("bg") or 0) < 0.25 * 65535)
    except Exception as e:  # noqa: BLE001 – Messung ist ein Zusatz
        out["measured"] = None
        out["measure_error"] = str(e)[:200]
    from .previews import _atomic_write
    _atomic_write(cache, json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
    return out
