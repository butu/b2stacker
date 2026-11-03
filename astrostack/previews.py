"""Vorschauen einzelner Rohbilder: dauerhafter, ortsunabhängiger Cache für Raster und Lightbox.

Die Analyse bereitet Thumbnails und Bildschirmvorschauen einmalig vor; cached_media liest danach nur
noch das Projekt-Manifest und die JPEGs im lokalen Cache, nie das Original. Volle Auflösung entsteht
erst auf ausdrückliche Anfrage (full_prepare).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading

import numpy as np
from scipy import ndimage as ndi

from . import catalog, fitsmeta, imageio
from .stackqa import stretch

# Rendering is RAM/CPU heavy; cache hits never take a slot.
_RENDER_SLOTS = threading.BoundedSemaphore(2)

RENDER_VERSION = 1                 # bump when rendered pixels change; part of every cache key
THUMB_WIDTH = 320
SCREEN_WIDTH = 1920
VARIANTS = {"thumb": THUMB_WIDTH, "screen": SCREEN_WIDTH, "full": None}
MANIFEST_NAME = "previews.json"
_KEY_RE = re.compile(r"^[0-9a-f]{16,64}$")


class PreviewError(RuntimeError):
    """Explicit preview preparation failed; callers show a readable message instead of a blank image."""


class _SourceChanged(RuntimeError):
    """The original changed while it was being rendered; nothing stale may be cached."""


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


def _render_image(raw: np.ndarray, bayer: str | None, width: int):
    """Downsample before stretching so thumbnails never carry full-size stretch artifacts."""
    from PIL import Image
    target_size = None
    if raw.ndim == 3:
        k = _step(max(raw.shape[1:]), width)
        img = raw[:, ::k, ::k].astype(np.float32) / 65535.0
    elif bayer and bayer.upper() in imageio._OFFSETS:
        k = _step(max(raw.shape) // 2, width)
        r, g, b = _cfa_planes(raw, bayer, k)
        img = np.stack([r, g, b]).astype(np.float32) / 65535.0
        med = np.median(img[:, ::8, ::8].reshape(3, -1), axis=1)
        img = img * (med[1] / np.maximum(med, 1e-9))[:, None, None]     # rough white balance
        # CFA planes are superpixels; scale the result to the requested display size.
        scale = min(1.0, width / max(raw.shape))
        target_size = (round(raw.shape[1] * scale), round(raw.shape[0] * scale))
    else:
        k = _step(max(raw.shape), width)
        img = raw[::k, ::k][None].astype(np.float32) / 65535.0
    st = stretch(img, target=0.2)
    arr = (np.moveaxis(st, 0, -1) * 255).astype(np.uint8) if st.shape[0] == 3 else (st[0] * 255).astype(np.uint8)
    im = Image.fromarray(arr)
    im.thumbnail((width, width), Image.LANCZOS)
    if target_size and target_size != im.size:
        im = im.resize(target_size, Image.LANCZOS)
    return im


def _stretch_params(sample: np.ndarray) -> tuple[float, float]:
    """Same maths as stackqa.stretch, exposed so native channels can be transformed one at a time."""
    s = sample[..., ::4, ::4] if sample.ndim == 3 else sample[::4, ::4]
    med = float(np.median(s))
    mad = float(np.median(np.abs(s - med))) * 1.4826
    lo = max(med - 2.8 * mad, float(s.min()))
    m = np.clip((med - lo) / max(1 - lo, 1e-6), 1e-6, 1)
    b = m * (0.2 - 1) / ((2 * 0.2 - 1) * m - 0.2)
    return lo, float(b)


def _apply_stretch(a: np.ndarray, lo: float, b: float) -> np.ndarray:
    x = np.clip((a - lo) / max(1 - lo, 1e-6), 0, 1)
    return np.clip(((b - 1) * x) / ((2 * b - 1) * x - b), 0, 1)


def _bilinear_from_sites(raw: np.ndarray, oy: int, ox: int) -> np.ndarray:
    """Fill one CFA colour plane at native pixel positions by horizontal then vertical bilinear interpolation."""
    h, w = raw.shape
    known = raw[oy::2, ox::2]
    rows = np.zeros((known.shape[0], w), np.float32)
    rows[:, ox::2] = known
    if ox == 0:
        if known.shape[1] > 1:
            rows[:, 1:-1:2] = (known[:, :-1] + known[:, 1:]) * 0.5
        rows[:, -1] = known[:, -1]
    else:
        rows[:, 0] = known[:, 0]
        if known.shape[1] > 1:
            rows[:, 2:-1:2] = (known[:, :-1] + known[:, 1:]) * 0.5
    out = np.zeros((h, w), np.float32)
    out[oy::2, :] = rows
    if oy == 0:
        if rows.shape[0] > 1:
            out[1:-1:2, :] = (out[0:-2:2, :] + out[2::2, :]) * 0.5
        out[-1, :] = out[-2, :]
    else:
        out[0, :] = out[1, :]
        if rows.shape[0] > 1:
            out[2:-1:2, :] = (out[1:-2:2, :] + out[3::2, :]) * 0.5
    return out


def _cfa_full_image(raw: np.ndarray, bayer: str):
    """True native-resolution CFA debayer: every sensor pixel exists, no half-size upscale."""
    from PIL import Image
    h, w = raw.shape
    h2, w2 = h + (h & 1), w + (w & 1)
    rr = np.pad(raw, ((0, h2 - h), (0, w2 - w)), mode="edge") if (h2, w2) != (h, w) else raw
    (ry, rx), (g1y, g1x), (g2y, g2x), (by, bx) = imageio._OFFSETS[bayer.upper()]

    # White balance and stretch from the cheap half-size planes; the transform is applied per channel.
    k = _step(max(raw.shape) // 2, 640)
    r, g, b = _cfa_planes(raw, bayer, k)
    small = np.stack([r, g, b]).astype(np.float32) / 65535.0
    med = np.median(small[:, ::8, ::8].reshape(3, -1), axis=1)
    gains = med[1] / np.maximum(med, 1e-9)
    small = small * gains[:, None, None]
    lo, beta = _stretch_params(small)

    green = np.zeros_like(rr)
    green[g1y::2, g1x::2] = rr[g1y::2, g1x::2]
    green[g2y::2, g2x::2] = rr[g2y::2, g2x::2]
    mask = np.zeros(rr.shape, dtype=bool)
    mask[g1y::2, g1x::2] = True
    mask[g2y::2, g2x::2] = True
    diag = np.array([[1, 0, 1], [0, 0, 0], [1, 0, 1]], dtype=np.float32)
    num = ndi.convolve(np.where(mask, green, 0), diag, mode="nearest")
    den = ndi.convolve(mask.astype(np.float32), diag, mode="nearest")
    green[~mask] = num[~mask] / np.maximum(den[~mask], 1e-9)

    planes = []
    for native, gain in ((_bilinear_from_sites(rr, ry, rx), gains[0]), (green, gains[1]),
                         (_bilinear_from_sites(rr, by, bx), gains[2])):
        v = native[:h, :w] * gain / 65535.0
        planes.append((_apply_stretch(v, lo, beta) * 255).astype(np.uint8))
    im = Image.fromarray(np.moveaxis(np.stack(planes), 0, -1))
    return im


def _render_full_jpeg(raw: np.ndarray, bayer: str | None) -> tuple[bytes, int, int]:
    """Native-resolution full frame; CFA uses bilinear debayer, never a half-size upscale."""
    if raw.ndim == 2 and bayer and bayer.upper() in imageio._OFFSETS:
        im = _cfa_full_image(raw, bayer)
    else:
        width = max(raw.shape[1:]) if raw.ndim == 3 else max(raw.shape)
        im = _render_image(raw, bayer, width)
    return _jpeg_bytes(im), im.width, im.height


def _render_jpeg(raw: np.ndarray, bayer: str | None, width: int) -> tuple[bytes, int, int]:
    im = _render_image(raw, bayer, width)
    return _jpeg_bytes(im), im.width, im.height


def frame_preview(path: str, cache_dir: str, width: int = 1800) -> str:
    """Downsample before stretching and reject renders whose source changed while loading."""
    sig0 = catalog.file_sig(path)
    key = hashlib.sha1(("%s|%s|%d" % (os.path.abspath(path), sig0, width)).encode()).hexdigest()[:20]
    out = os.path.join(cache_dir, key + ".jpg")
    if os.path.exists(out):
        return out
    with _RENDER_SLOTS:
        if os.path.exists(out):
            return out
        meta = fitsmeta.read_meta(path)
        raw = imageio.load(path)
        data, _, _ = _render_jpeg(raw, meta.bayer, width)
        if catalog.file_sig(path) != sig0:
            raise RuntimeError("Rohbild während des Renderns geändert: %s" % os.path.basename(path))
        _atomic_write(out, data)
    return out


# ------------------------------------------------------------------ persistent cache

def _default_cache_root() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "astrostack", "previews")


def cache_root(preview_cache_dir: str | None = None) -> str:
    """Explicit path, test override, configured setting, then the local OS cache directory."""
    if preview_cache_dir:
        return os.path.abspath(preview_cache_dir)
    env = os.environ.get("ASTROSTACK_PREVIEW_CACHE")
    if env:
        return os.path.abspath(env)
    try:
        from .settings import Settings
        settings_path = Settings.path()
        try:
            st = os.stat(settings_path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with _SETTINGS_LOCK:
            hit = _SETTINGS_CACHE.get(settings_path)
            cached = bool(hit) and hit[0] == stamp
            configured = hit[1] if cached else ""
        if not cached:
            configured = Settings.load().preview_cache_dir
            with _SETTINGS_LOCK:
                _SETTINGS_CACHE[settings_path] = (stamp, configured)
        if configured:
            return os.path.abspath(configured)
    except Exception:  # noqa: BLE001 – a broken settings file must not break previews
        pass
    return _default_cache_root()


def _render_key(sha: str, bayer: str | None) -> str:
    return hashlib.sha1(("%s|%d|%s" % (sha, RENDER_VERSION, bayer or "")).encode()).hexdigest()[:24]


def _variant_rel(key: str, variant: str) -> str:
    return os.path.join("v%d" % RENDER_VERSION, key, variant + ".jpg")


def _safe_variant_path(root: str, key, variant: str):
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        return None
    p = os.path.join(root, _variant_rel(key, variant))
    real, base = os.path.realpath(p), os.path.realpath(root)
    return real if real.startswith(base + os.sep) else None


def _empty_manifest() -> dict:
    return {"version": 1, "render_version": RENDER_VERSION, "frames": {}}


_MANIFEST_LOCK = threading.RLock()
_MANIFEST_CACHE: dict[str, tuple] = {}   # path -> (mtime_ns, size, manifest); UI reads the same file often
_MANIFEST_INDEX: dict[int, tuple] = {}   # id(manifest) -> (frames object, normalized path map)
_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE: dict[str, tuple] = {}   # settings path -> (stat stamp, configured preview dir)


def _read_manifest(analysis_dir: str) -> dict:
    p = os.path.abspath(os.path.join(analysis_dir, MANIFEST_NAME))
    try:
        st = os.stat(p)
    except OSError:
        return _empty_manifest()
    with _MANIFEST_LOCK:
        hit = _MANIFEST_CACHE.get(p)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        with open(p, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return _empty_manifest()
    if not isinstance(m, dict) or m.get("version") != 1 or not isinstance(m.get("frames"), dict):
        return _empty_manifest()
    with _MANIFEST_LOCK:
        _MANIFEST_CACHE[p] = (st.st_mtime_ns, st.st_size, m)
    return m


def _load_manifest_file(analysis_dir: str) -> dict | None:
    """Fresh disk snapshot for the write-side merge; the memoized read copy may predate a concurrent writer."""
    p = os.path.abspath(os.path.join(analysis_dir, MANIFEST_NAME))
    try:
        with open(p, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("version") != 1 or not isinstance(m.get("frames"), dict):
        return None
    return m


def _merge_record(current, incoming, conflict: str):
    """Keep concurrent full records only when both sides describe the same rendered content."""
    if not isinstance(current, dict):
        return incoming if isinstance(incoming, dict) else None
    if not isinstance(incoming, dict):
        return current
    same = (current.get("key") and current.get("key") == incoming.get("key")
            and current.get("sha256") and current.get("sha256") == incoming.get("sha256"))
    if not same:
        return incoming if conflict == "incoming" else current
    merged = dict(current)
    variants = dict(current.get("variants") or {})
    variants.update(incoming.get("variants") or {})
    errors = dict(current.get("errors") or {})
    errors.update(incoming.get("errors") or {})
    for variant in variants:
        errors.pop(variant, None)
    merged["variants"] = variants
    merged["errors"] = errors
    if incoming.get("render"):
        merged["render"] = incoming["render"]
    for field in ("orig_width", "orig_height", "bayer", "file_sig"):
        if merged.get(field) is None and incoming.get(field) is not None:
            merged[field] = incoming[field]
    return merged


def _write_manifest(analysis_dir: str, manifest: dict, frame_keys=None, key_conflict: str = "incoming") -> None:
    manifest["version"] = 1
    manifest["render_version"] = RENDER_VERSION
    os.makedirs(analysis_dir, exist_ok=True)
    p = os.path.abspath(os.path.join(analysis_dir, MANIFEST_NAME))
    tmp = "%s.%d.%d.tmp" % (p, os.getpid(), threading.get_ident())
    with _MANIFEST_LOCK:
        target = manifest
        if frame_keys is not None:
            current = _load_manifest_file(analysis_dir)
            if current is not None:
                current_frames = current.setdefault("frames", {})
                for path in frame_keys:
                    incoming = (manifest.get("frames") or {}).get(path)
                    merged = _merge_record(current_frames.get(path), incoming, key_conflict)
                    if merged is None:
                        current_frames.pop(path, None)
                    else:
                        current_frames[path] = merged
                current["version"] = 1
                current["render_version"] = RENDER_VERSION
                target = current
        _MANIFEST_INDEX.pop(id(manifest), None)
        _MANIFEST_INDEX.pop(id(target), None)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(target, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            _MANIFEST_CACHE.pop(p, None)
            raise
        try:
            st = os.stat(p)
            _MANIFEST_CACHE[p] = (st.st_mtime_ns, st.st_size, target)
        except OSError:
            _MANIFEST_CACHE.pop(p, None)


def _lookup(manifest: dict, path: str):
    frames = manifest.get("frames") or {}
    rec = frames.get(path)
    if isinstance(rec, dict):
        return rec
    # Windows casing and relative call sites still find the prepared entry; index once per manifest.
    want = os.path.normcase(os.path.abspath(path))
    with _MANIFEST_LOCK:
        entry = _MANIFEST_INDEX.get(id(manifest))
    if not entry or entry[0] is not frames:
        index = {os.path.normcase(os.path.abspath(k)): v for k, v in frames.items()}
        with _MANIFEST_LOCK:
            _MANIFEST_INDEX[id(manifest)] = (frames, index)
        entry = (frames, index)
    rec = entry[1].get(want)
    return rec if isinstance(rec, dict) else None


def _variant_ready(rec: dict, root: str, variant: str) -> bool:
    v = (rec.get("variants") or {}).get(variant)
    if not isinstance(v, dict):
        return False
    key = rec.get("key")
    if not _KEY_RE.fullmatch(str(key or "")) or v.get("path") != _variant_rel(key, variant):
        return False
    p = _safe_variant_path(root, key, variant)
    if not p:
        return False
    try:
        with open(p, "rb") as fh:      # cache-only check; a truncated JPEG only heals on explicit prepare
            return fh.read(2) == b"\xff\xd8"
    except OSError:
        return False


def _record_ready(rec: dict | None, root: str) -> bool:
    return bool(rec and _variant_ready(rec, root, "thumb") and _variant_ready(rec, root, "screen"))


def _error_record(sha, bayer, sig, reason) -> dict:
    return {"sha256": sha, "bayer": bayer, "file_sig": sig,
            "key": _render_key(sha, bayer) if sha else None,
            "variants": {}, "errors": {"prepare": str(reason)}}


_MISSING = object()


def _frame_field(frame, name: str, default=None):
    """Accept FrameMeta and the plain dicts persisted in project frame indexes."""
    if isinstance(frame, dict):
        return frame.get(name, default)
    return getattr(frame, name, default)


def _bayer_from_source(path: str, cat) -> str | None:
    """Legacy frame dicts lack bayer; prefer the persisted header cache over a fresh NAS read."""
    if cat:
        try:
            return fitsmeta.from_header(cat.header(path), path).bayer
        except Exception:  # noqa: BLE001 – fall back to a direct header read
            pass
    return fitsmeta.read_meta(path).bayer


def _record_from_raw(path: str, bayer, raw, sha: str, sig: str, root: str) -> dict:
    """Render and store both browse variants; writes happen only after the source check."""
    orig_h, orig_w = (raw.shape[1], raw.shape[2]) if raw.ndim == 3 else (raw.shape[0], raw.shape[1])
    render = "cfa-superpixel" if raw.ndim == 2 and bayer and bayer.upper() in imageio._OFFSETS else "native-downsample"
    rendered = {}
    errors = {}
    with _RENDER_SLOTS:
        for variant in ("thumb", "screen"):
            try:
                rendered[variant] = _render_jpeg(raw, bayer, VARIANTS[variant])
            except Exception as e:  # noqa: BLE001 – one broken variant must not stop the analysis
                errors[variant] = str(e)
        if catalog.file_sig(path) != sig:
            raise _SourceChanged("Rohbild während der Vorschau-Erzeugung geändert")
        key = _render_key(sha, bayer)
        variants = {}
        for variant, (data, w, h) in rendered.items():
            rel = _variant_rel(key, variant)
            _atomic_write(os.path.join(root, rel), data)
            variants[variant] = {"path": rel, "width": w, "height": h, "bytes": len(data), "render": render}
    return {"sha256": sha, "bayer": bayer, "file_sig": sig, "key": key,
            "orig_width": orig_w, "orig_height": orig_h,
            "variants": variants, "errors": errors}


def store_from_raw(path: str, bayer, raw, sha: str, sig: str, root: str) -> dict:
    """Analyse path: reuse pixels already loaded for measurement instead of reading the NAS twice."""
    return _record_from_raw(path, bayer, raw, sha, sig, root)


def failure_record(sha, bayer, sig, reason) -> dict:
    """Preview-only failure kept apart from measurement errors."""
    return _error_record(sha, bayer, sig, reason)


def prepare_frames(frames, analysis_dir: str, preview_cache_dir: str | None = None,
                   catalog_dir: str | None = None, log=None) -> dict:
    """Explicit preparation for browse: read each original at most once and cache thumb + screen."""
    root = cache_root(preview_cache_dir)
    manifest = _read_manifest(analysis_dir)
    cat = catalog.open_catalog(catalog_dir) if catalog_dir else None
    rows = {}
    owned = set()
    if not cat:   # analysis already recorded content hashes; avoid re-reading the original for them
        try:
            from . import analyze as an
            rows = an._read_csv_rows(os.path.join(analysis_dir, "metrics.csv"))
        except Exception:  # noqa: BLE001 – preview preparation must not depend on the metrics format
            rows = {}
    for f in frames:
        path = _frame_field(f, "path")
        bayer = _frame_field(f, "bayer", _MISSING)
        if bayer is None and isinstance(f, dict):
            # Legacy project frame dicts may carry bayer=None even for CFA files; treat as absent.
            bayer = _MISSING
        old = _lookup(manifest, path)
        sha = sig = None
        try:
            if cat:
                sha, sig = cat.resolve(path)
            else:
                sig = catalog.file_sig(path)
                row = rows.get(path) or {}
                sha = row.get("sha256") if row.get("file_sig") == sig else None
                if not sha:
                    sha = catalog.sha256_file(path)
            if sha is None:
                raise _SourceChanged("Datei änderte sich während der Prüfung")
            if bayer is _MISSING:   # project frame indexes omit the Bayer pattern
                if old and old.get("sha256") == sha and old.get("bayer") is not None:
                    bayer = old["bayer"]
                else:
                    bayer = _bayer_from_source(path, cat)
            if (old and old.get("sha256") == sha and old.get("bayer") == bayer
                    and old.get("file_sig") == sig and _record_ready(old, root)):
                continue
            raw = imageio.load(path)
            manifest["frames"][path] = _record_from_raw(path, bayer, raw, sha, sig, root)
            owned.add(path)
        except Exception as e:  # noqa: BLE001 – one unreadable frame stays an error record
            if old and old.get("variants"):
                old["errors"] = dict(old.get("errors") or {}, prepare=str(e))
                manifest["frames"][path] = old
            else:
                manifest["frames"][path] = _error_record(sha, bayer, sig, e)
            owned.add(path)
            if log:
                log("Vorschau nicht möglich: %s (%s)" % (os.path.basename(path), e))
    _write_manifest(analysis_dir, manifest, frame_keys=owned)
    return manifest


def record_media(analysis_dir: str, records: dict) -> dict:
    """Merge records produced during measurement and persist the project manifest."""
    manifest = _read_manifest(analysis_dir)
    for path, rec in (records or {}).items():
        if isinstance(rec, dict):
            manifest["frames"][path] = rec
    _write_manifest(analysis_dir, manifest, frame_keys=set(records or {}))
    return manifest


def missing_frames(frames, analysis_dir: str, preview_cache_dir: str | None = None) -> list:
    """Frames without a complete thumb + screen pair; cached reads never touch originals."""
    root = cache_root(preview_cache_dir)
    manifest = _read_manifest(analysis_dir)
    return [f for f in frames if not _record_ready(_lookup(manifest, _frame_field(f, "path")), root)]


def _media_from_record(rec: dict | None, root: str, variant: str) -> dict | None:
    if not rec or rec.get("errors", {}).get(variant) or not _variant_ready(rec, root, variant):
        return None
    variant_meta = rec["variants"][variant]
    media = _safe_variant_path(root, rec.get("key"), variant)
    if not media:
        return None
    return {"key": rec.get("key"), "path": media, "variant": variant,
            "width": int(variant_meta.get("width") or 0), "height": int(variant_meta.get("height") or 0),
            "orig_width": rec.get("orig_width"), "orig_height": rec.get("orig_height"),
            "render": variant_meta.get("render") or (rec.get("render") if variant == "full" else None),
            "bytes": variant_meta.get("bytes"), "sha256": rec.get("sha256"), "bayer": rec.get("bayer")}


def cached_media(path: str, analysis_dir: str, variant: str = "screen",
                 preview_cache_dir: str | None = None) -> dict | None:
    """Manifest lookup only: no stat, realpath or render of the original file."""
    if variant not in VARIANTS:
        raise ValueError("unbekannte Vorschau-Variante: %s" % variant)
    rec = _lookup(_read_manifest(analysis_dir), path)
    return _media_from_record(rec, cache_root(preview_cache_dir), variant)


def cached_media_bulk(paths, analysis_dir: str, variants=("thumb", "screen", "full"),
                      preview_cache_dir: str | None = None) -> dict[str, dict]:
    """One manifest read for a whole frame list; project_frames avoids 3 lookups per row."""
    bad = [v for v in variants if v not in VARIANTS]
    if bad:
        raise ValueError("unbekannte Vorschau-Variante: %s" % bad[0])
    root = cache_root(preview_cache_dir)
    manifest = _read_manifest(analysis_dir)
    frames = manifest.get("frames") or {}
    fallback = None
    out: dict[str, dict] = {}
    for path in paths:
        rec = frames.get(path)
        if rec is None:   # windows casing/relative call sites; build the normalized map once per bulk call
            if fallback is None:
                fallback = {os.path.normcase(os.path.abspath(k)): v for k, v in frames.items()}
            rec = fallback.get(os.path.normcase(os.path.abspath(path)))
        out[path] = {v: _media_from_record(rec, root, v) for v in variants}
    return out


def full_prepare(path: str, analysis_dir: str, preview_cache_dir: str | None = None) -> dict:
    """Explicit full-resolution render for zoom; offline originals raise a readable PreviewError."""
    root = cache_root(preview_cache_dir)
    hit = cached_media(path, analysis_dir, "full", preview_cache_dir=root)
    if hit:
        return hit
    manifest = _read_manifest(analysis_dir)
    rec = _lookup(manifest, path)
    try:
        sig = catalog.file_sig(path)
    except OSError as e:
        raise PreviewError("Original nicht verfügbar: %s" % os.path.basename(path)) from e
    try:
        raw = imageio.load(path)
    except Exception as e:  # noqa: BLE001
        raise PreviewError("Original nicht lesbar: %s (%s)" % (os.path.basename(path), e)) from e
    if rec and rec.get("file_sig") == sig and rec.get("sha256"):
        sha = rec["sha256"]
    else:
        try:
            sha = catalog.sha256_file(path)
        except OSError as e:
            raise PreviewError("Original nicht verfügbar: %s" % os.path.basename(path)) from e
    bayer = rec.get("bayer") if rec and rec.get("file_sig") == sig else None
    if bayer is None:
        try:
            bayer = fitsmeta.read_meta(path).bayer
        except Exception:  # noqa: BLE001 – header failure only means mono-style rendering
            bayer = None
    key = _render_key(sha, bayer)
    with _RENDER_SLOTS:
        data, w, h = _render_full_jpeg(raw, bayer)
        if catalog.file_sig(path) != sig:
            raise PreviewError("Rohbild während der Vorschau-Erzeugung geändert: %s" % os.path.basename(path))
        rel = _variant_rel(key, "full")
        _atomic_write(os.path.join(root, rel), data)
    mode = "cfa-bilinear-native" if raw.ndim == 2 and bayer and bayer.upper() in imageio._OFFSETS else "native"
    variants = dict(rec.get("variants") or {}) if rec and rec.get("key") == key else {}
    errors = dict(rec.get("errors") or {}) if rec and rec.get("key") == key else {}
    errors.pop("full", None)
    variants["full"] = {"path": rel, "width": w, "height": h, "bytes": len(data), "render": mode}
    orig_w = rec.get("orig_width") if rec and rec.get("key") == key else raw.shape[-1]
    orig_h = rec.get("orig_height") if rec and rec.get("key") == key else raw.shape[-2]
    manifest["frames"][path] = {"sha256": sha, "bayer": bayer, "file_sig": sig, "key": key,
                                "orig_width": orig_w, "orig_height": orig_h, "render": mode,
                                "variants": variants, "errors": errors}
    _write_manifest(analysis_dir, manifest, frame_keys={path}, key_conflict="current")
    return {"key": key, "path": os.path.join(root, rel), "variant": "full",
            "width": w, "height": h, "orig_width": orig_w, "orig_height": orig_h, "render": mode,
            "bytes": len(data), "sha256": sha, "bayer": bayer}


prepare_full = full_prepare   # explicit zoom helper; mirrors the task contract naming


def _valid_analysis_thumb(path: str, analysis_dir: str, sig: str):
    """Path to the analysis thumbnail (.npy), only if metrics.csv matches the current file."""
    from . import analyze as an
    row = an._read_csv_rows(os.path.join(analysis_dir, "metrics.csv")).get(path)
    if not row or row.get("file_sig") != sig or row.get("metrics_ver") != str(catalog.LIGHT_VERSION):
        return None
    npy = os.path.join(analysis_dir, "thumbs", an.thumb_name(path))
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
    """Legacy renderer used by explicit prepare; browse endpoints serve cached_media instead."""
    sig = catalog.file_sig(path)
    out_dir = os.path.join(analysis_dir, "frame-thumbs")
    npy = _valid_analysis_thumb(path, analysis_dir, sig)
    if npy is not None:
        try:
            stamp = catalog.file_sig(npy)
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
        if catalog.file_sig(path) != sig or catalog.file_sig(npy) != stamp:
            raise RuntimeError("Analysis state changed during rendering: %s" % os.path.basename(path))
        _atomic_write(out, data)
    return out
