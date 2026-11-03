"""Reference existing images without altering originals; cache only derived previews.

Browsing reads a persisted inventory snapshot.  Folder listings and source
stat calls happen only during an explicit refresh (analysis, link, prepare).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os

IMAGE_EXT = (".psd", ".tif", ".tiff", ".jpg", ".jpeg")
STACK_EXT = (".xisf", ".fit", ".fits", ".fts")
PREVIEW_MAX = 1200
LINKED_FILE = "assets.json"
SNAPSHOT = os.path.join("analysis", "inventory-assets.json")
SNAPSHOT_VERSION = 1


def _key(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:12]


def _stored(pdir: str, path: str) -> str:
    ap = os.path.abspath(path)
    try:
        rel = os.path.relpath(ap, os.path.abspath(pdir))
    except ValueError:                        # other drive/mount: keep the reference absolute
        return ap
    return rel.replace("\\", "/") if not rel.startswith("..") else ap


def _entry(pdir: str, path: str, kind: str, location: str | None = None) -> dict:
    return dict(id=_key(path), name=os.path.basename(path), path=_stored(pdir, path),
                exists=os.path.isfile(path), kind=kind, location=location,
                offline=not os.path.isdir(os.path.dirname(path)) and not os.path.isfile(path))


def _snapshot_path(pdir: str) -> str:
    return os.path.join(pdir, SNAPSHOT)


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _snapshot(pdir: str) -> dict | None:
    try:
        with open(_snapshot_path(pdir), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


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
    """Persisted scan locations; a browser GET must never test whether they exist."""
    cands = [pdir, os.path.join(pdir, "final")]
    for r in _source_roots(pdir):
        cands += [r, os.path.join(r, "final")]
    out, seen = [], set()
    for d in cands:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            out.append(os.path.abspath(d))
    return out


def _existing_locations(pdir: str) -> list[str]:
    return [d for d in scan_locations(pdir) if os.path.isdir(d)]


def _linked_entries(pdir: str, check: bool = False) -> dict[str, dict]:
    if check:
        return {_key(p): _entry(pdir, p, "linked") for p in _linked_paths(pdir)}
    # Metadata only: reading a linked path's state would touch an offline original.
    return {_key(p): dict(id=_key(p), name=os.path.basename(p), path=_stored(pdir, p),
                          exists=False, kind="linked", location=None, offline=None)
            for p in _linked_paths(pdir)}


def _snapshot_entries(pdir: str) -> dict[str, dict]:
    snap = _snapshot(pdir)
    entries = snap.get("entries") if snap else None
    return {e["id"]: dict(e) for e in entries or [] if isinstance(e, dict) and e.get("id")}


def _local_asset_hint(pdir: str) -> bool:
    """Local project folders only; source roots may be offline and stay untouched on GET."""
    for d in (pdir, os.path.join(pdir, "final")):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        if any(n.lower().endswith(IMAGE_EXT) and os.path.isfile(os.path.join(d, n)) for n in names):
            return True
    return False


def state(pdir: str) -> dict:
    """Report whether an old project still needs an explicit inventory refresh."""
    snap = _snapshot(pdir)
    linked = os.path.isfile(os.path.join(pdir, LINKED_FILE))
    old_analysis = os.path.isfile(os.path.join(pdir, "analysis", "metrics.csv"))
    hint = snap is None and _local_asset_hint(pdir)
    return dict(version=snap.get("version") if snap else None,
                generated=snap.get("generated") if snap else None,
                entries=len(snap.get("entries") or []) if snap else 0,
                migration_needed=snap is None and (linked or old_analysis or hint))


def _merge_preview(pdir: str, entry: dict, old: dict | None) -> dict:
    if old and old.get("preview"):
        prev = os.path.join(pdir, "analysis", "asset-previews", old["preview"])
        if os.path.isfile(prev):
            entry["preview"] = old["preview"]
            entry["preview_version"] = old.get("preview_version")
    return entry


def _persist(pdir: str, entries: list[dict]) -> None:
    _write_json(_snapshot_path(pdir), dict(
        version=SNAPSHOT_VERSION, generated=datetime.datetime.now().isoformat(timespec="seconds"),
        locations=[_stored(pdir, d) for d in _existing_locations(pdir)], entries=entries))


def refresh(pdir: str, render_previews: bool = False) -> list[dict]:
    """Explicitly scan configured locations and persist the inventory snapshot."""
    old = _snapshot_entries(pdir)
    found: dict[str, dict] = {}
    for d in _existing_locations(pdir):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p) and f.lower().endswith(IMAGE_EXT):
                e = _entry(pdir, p, "auto", location=_stored(pdir, d))
                found.setdefault(e["id"], e)
    found.update(_linked_entries(pdir, check=True))
    for aid, entry in found.items():
        _merge_preview(pdir, entry, old.get(aid))
    if render_previews:
        for entry in found.values():
            if entry.get("exists"):
                _render_entry(pdir, entry)
    entries = list(found.values())
    _persist(pdir, entries)
    return entries


def discover(pdir: str) -> list[dict]:
    """Snapshot + linked metadata only: no folder listing, no source stat, no render."""
    entries = _snapshot_entries(pdir)
    for aid, entry in _linked_entries(pdir).items():
        entries.setdefault(aid, entry)
    return list(entries.values())


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
    entries = refresh(pdir)
    entry = next((e for e in entries if e["id"] == _key(p)), _entry(pdir, p, "linked"))
    if entry.get("exists"):
        _render_entry(pdir, entry)
        _persist(pdir, entries)
    return entry


def _write_linked(pdir: str, paths: list[str]):
    tmp = os.path.join(pdir, LINKED_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(paths, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, os.path.join(pdir, LINKED_FILE))


def _find(pdir: str, asset_id: str) -> dict | None:
    entry = _snapshot_entries(pdir).get(asset_id)
    if entry is not None:
        return entry
    return _linked_entries(pdir).get(asset_id)


def resolve(pdir: str, asset_id: str) -> dict:
    """Restrict image delivery to linked or snapshot project assets."""
    a = _find(pdir, asset_id)
    if a is None:
        raise FileNotFoundError("Asset nicht registriert")
    return a


def _source_path(pdir: str, entry: dict) -> str:
    return entry["path"] if os.path.isabs(entry["path"]) else os.path.join(pdir, entry["path"])


def _preview_path(pdir: str, asset_id: str) -> str:
    return os.path.join(pdir, "analysis", "asset-previews", asset_id + ".jpg")


def cached_preview(pdir: str, asset_id: str) -> tuple[str | None, str | None]:
    """Return an already rendered preview or a clear message; never touch the original."""
    a = _find(pdir, asset_id)
    if a is None:
        return None, "Asset nicht registriert"
    out = _preview_path(pdir, asset_id)
    if os.path.isfile(out):
        return out, None
    if a.get("exists") is False:
        return None, "Original nicht erreichbar (offline)" if a.get("offline") else "Datei fehlt"
    return None, "Vorschau nicht vorbereitet – Asset erneut verknüpfen oder Vorschauen vorbereiten."


def preview(pdir: str, asset_id: str) -> tuple[str | None, str | None]:
    """Explicit render path for CLI/prepare; browser endpoints use cached_preview."""
    a = resolve(pdir, asset_id)
    src = _source_path(pdir, a)
    if not os.path.isfile(src):
        return None, "Datei fehlt"
    out = _preview_path(pdir, asset_id)
    try:
        _render(src, out, os.path.dirname(out))
    except Exception as e:  # noqa: BLE001 - keep unsupported image formats visible to the user
        return None, "Vorschau nicht möglich (%s): %s" % (type(e).__name__, e)
    _render_entry(pdir, a, out=out)
    entries = _snapshot_entries(pdir)
    entries[a["id"]] = a
    _persist(pdir, list(entries.values()))
    return out, None


def _render_entry(pdir: str, entry: dict, out: str | None = None) -> str | None:
    src = _source_path(pdir, entry)
    out = out or _preview_path(pdir, entry["id"])
    try:
        _render(src, out, os.path.dirname(out))
    except Exception:  # noqa: BLE001 - the inventory must keep the original entry
        entry.pop("preview", None)
        entry.pop("preview_version", None)
        return None
    try:
        st = os.stat(src)
        entry["preview_version"] = hashlib.sha1(
            ("%s|%s|%s" % (entry["id"], st.st_mtime_ns, st.st_size)).encode()).hexdigest()[:16]
    except OSError:
        entry["preview_version"] = None
    entry["preview"] = entry["id"] + ".jpg"
    return out


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
