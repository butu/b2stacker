"""Reference existing images without altering originals; cache only derived previews."""
from __future__ import annotations

import hashlib
import json
import os

IMAGE_EXT = (".psd", ".tif", ".tiff", ".jpg", ".jpeg")
STACK_EXT = (".xisf", ".fit", ".fits", ".fts")
PREVIEW_MAX = 1200
LINKED_FILE = "assets.json"


def _key(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:12]


def _stored(pdir: str, path: str) -> str:
    ap = os.path.abspath(path)
    rel = os.path.relpath(ap, os.path.abspath(pdir))
    return rel.replace("\\", "/") if not rel.startswith("..") else ap


def _entry(pdir: str, path: str, kind: str) -> dict:
    return dict(id=_key(path), name=os.path.basename(path), path=_stored(pdir, path),
                exists=os.path.isfile(path), kind=kind)


def _linked_paths(pdir: str) -> list[str]:
    try:
        with open(os.path.join(pdir, LINKED_FILE), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    return [p if os.path.isabs(p) else os.path.join(pdir, p) for p in raw if isinstance(p, str)]


def _source_roots(pdir: str) -> list[str]:
    try:
        with open(os.path.join(pdir, "sources.json"), encoding="utf-8") as fh:
            raw = json.load(fh).get("roots") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in raw if isinstance(r, str) and r.strip()]


def scan_locations(pdir: str) -> list[str]:
    """Keep automatic discovery shallow so opening a project never scans entire image trees."""
    cands = [pdir, os.path.join(pdir, "final")]
    for r in _source_roots(pdir):
        cands += [r, os.path.join(r, "final")]
    out, seen = [], set()
    for d in cands:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen and os.path.isdir(d):
            seen.add(k)
            out.append(os.path.abspath(d))
    return out


def discover(pdir: str) -> list[dict]:
    """Do not mistake raw FITS lights for finished image assets."""
    found: dict[str, dict] = {}
    for d in scan_locations(pdir):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p) and f.lower().endswith(IMAGE_EXT):
                found.setdefault(_key(p), dict(_entry(pdir, p, "auto"), location=_stored(pdir, d)))
    for p in _linked_paths(pdir):
        found.setdefault(_key(p), _entry(pdir, p, "linked"))
    return list(found.values())


def link(pdir: str, path: str) -> dict:
    """Store the reference only; the existing image stays in its original location."""
    p = (path or "").strip().strip('"')
    if not p or not os.path.isfile(p):
        raise ValueError("Datei nicht gefunden: " + p)
    if not p.lower().endswith(IMAGE_EXT + STACK_EXT):
        raise ValueError("Nicht unterstütztes Format (erlaubt: PSD, TIFF, JPEG, XISF, FITS).")
    stored = [_stored(pdir, x) for x in _linked_paths(pdir)]
    if _stored(pdir, p) not in stored:
        stored.append(_stored(pdir, p))
        _write_linked(pdir, stored)
    return _entry(pdir, p, "linked")


def _write_linked(pdir: str, paths: list[str]):
    tmp = os.path.join(pdir, LINKED_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(paths, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, os.path.join(pdir, LINKED_FILE))


def resolve(pdir: str, asset_id: str) -> dict:
    """Restrict image delivery to linked or discovered project assets."""
    a = next((x for x in discover(pdir) if x["id"] == asset_id), None)
    if a is None:
        raise FileNotFoundError("Asset nicht registriert")
    return a


def preview(pdir: str, asset_id: str) -> tuple[str | None, str | None]:
    """Return a derived preview or a readable error without modifying the source image."""
    a = resolve(pdir, asset_id)
    src = os.path.join(pdir, a["path"]) if not os.path.isabs(a["path"]) else a["path"]
    if not os.path.isfile(src):
        return None, "Datei fehlt"
    out_dir = os.path.join(pdir, "analysis", "asset-previews")
    out = os.path.join(out_dir, asset_id + ".jpg")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out, None
    try:
        _render(src, out, out_dir)
    except Exception as e:  # noqa: BLE001 - keep unsupported image formats visible to the user
        return None, "Vorschau nicht möglich (%s): %s" % (type(e).__name__, e)
    return out, None


def _render(src: str, out: str, out_dir: str):
    import numpy as np
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    with Image.open(src) as im:
        im.load()
        if im.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
            arr = np.asarray(im, dtype=np.float32)
            lo, hi = np.percentile(arr, [0.5, 99.5])
            arr = np.clip((arr - lo) / max(float(hi - lo), 1e-6), 0, 1)
            im = Image.fromarray((arr * 255).astype(np.uint8))
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
        tmp = out + ".tmp.jpg"
        im.convert("RGB").save(tmp, "JPEG", quality=85)
    os.replace(tmp, out)
