"""Shared catalog cache (workdir/cache/catalog) for headers, light metrics, thumbnails and flat statistics.

Items are keyed by the SHA256 of file content. ``index.json`` maps canonical paths to a stat signature
and hash so unchanged files are not re-hashed on every scan. Only content-derived results are cached.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

import numpy as np

HEADER_VERSION = 1      # raw header dict (content only)
LIGHT_VERSION = 1       # light measurement (analyze.measure, without context columns); also the CSV metrics_ver
FLAT_VERSION = 1        # flat statistics (calibcheck.flat_frame_stats, without path)
_HASH_CHUNK = 1 << 20


def open_catalog(root: str):
    """Catalog, or None if the directory is unusable. The cache is best-effort."""
    try:
        return Catalog(root)
    except OSError:
        return None


def file_sig(path: str) -> str:
    """Cheap change check; ctime is included so backdated mtimes are still detected."""
    st = os.stat(path)
    return "%d:%d:%d" % (st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: str, write) -> None:
    # Unique temp name per process/thread; os.replace is atomic, so readers never see partial files.
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "wb") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Catalog:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.items = os.path.join(self.root, "items")
        self._index_path = os.path.join(self.root, "index.json")
        self._lock = threading.Lock()
        self._index = self._load_index()
        self._dirty = False
        self.write_failures = 0     # counted, never raised: a full or read-only cache must not stop analysis
        os.makedirs(self.items, exist_ok=True)

    # ------------------------------------------------------------------ index

    def _load_index(self) -> dict:
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}     # broken index: rebuild it; item contents stay valid

    def resolve(self, path: str) -> tuple[str | None, str]:
        """Return (sha256, stat signature). sha is None if the file changed while being hashed."""
        key = canonical(path)
        sig = file_sig(path)
        with self._lock:
            e = self._index.get(key)
        if isinstance(e, dict) and e.get("sig") == sig and isinstance(e.get("sha"), str):
            return e["sha"], sig
        sha = sha256_file(path)
        if file_sig(path) != sig:
            return None, sig
        with self._lock:
            self._index[key] = {"sig": sig, "sha": sha}
            self._dirty = True
        return sha, sig

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            merged = self._load_index()          # keep entries written by other processes
            merged.update(self._index)
            blob = json.dumps(merged).encode("utf-8")
        try:
            os.makedirs(self.root, exist_ok=True)
            _atomic_write(self._index_path, lambda fh: fh.write(blob))
        except OSError:
            self.write_failures += 1             # stays dirty, next flush retries
            return
        with self._lock:
            self._index = merged
            self._dirty = False

    def prune_missing(self) -> int:
        """Drop index entries for files that no longer exist. Returns the number removed."""
        with self._lock:
            gone = [k for k in self._index if not os.path.exists(k)]
            for k in gone:
                del self._index[k]
            if gone:
                self._dirty = True
        return len(gone)

    # ------------------------------------------------------------------ items

    def _item(self, sha: str, kind: str) -> str:
        return os.path.join(self.items, "%s.%s.json" % (sha, kind))

    def get(self, sha: str, kind: str, version: int):
        """Broken, foreign or outdated items count as a miss, never as an error."""
        try:
            with open(self._item(sha, kind), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("v") != version or d.get("sha256") != sha or d.get("data") is None:
            return None
        return d["data"]

    def put(self, sha: str, kind: str, version: int, data) -> None:
        blob = json.dumps({"v": version, "sha256": sha, "data": data}, default=str).encode("utf-8")
        try:
            _atomic_write(self._item(sha, kind), lambda fh: fh.write(blob))
        except OSError:
            self.write_failures += 1

    def get_thumb(self, sha: str):
        try:
            return np.load(os.path.join(self.items, sha + ".thumb.npy"), allow_pickle=False)
        except Exception:
            return None

    def put_thumb(self, sha: str, arr: np.ndarray) -> None:
        try:
            _atomic_write(os.path.join(self.items, sha + ".thumb.npy"), lambda fh: np.save(fh, arr))
        except OSError:
            self.write_failures += 1

    # ------------------------------------------------------------------ header

    def header(self, path: str) -> dict:
        """Raw header of a file. Type/filter/folder translation is done by the caller on every run."""
        from . import fitsmeta as fm
        sha, sig = self.resolve(path)
        if sha is None:
            return fm.read_header(path)
        hit = self.get(sha, "header", HEADER_VERSION)
        if isinstance(hit, dict):
            return hit
        h = json.loads(json.dumps(dict(fm.read_header(path)), default=str))
        if file_sig(path) == sig:
            self.put(sha, "header", HEADER_VERSION, h)
        return h

    # ------------------------------------------------------------------ light / flat

    def light_get(self, sha: str, bayer):
        d = self.get(sha, "light", LIGHT_VERSION)
        if not isinstance(d, dict) or d.get("bayer") != bayer or not isinstance(d.get("metrics"), dict):
            return None
        thumb = self.get_thumb(sha)
        if thumb is None:
            return None
        return d["metrics"], thumb

    def light_put(self, sha: str, bayer, metrics: dict, thumb: np.ndarray) -> None:
        self.put(sha, "light", LIGHT_VERSION, {"bayer": bayer, "metrics": metrics})
        self.put_thumb(sha, thumb)

    def flat_get(self, sha: str, bayer):
        d = self.get(sha, "flatstats", FLAT_VERSION)
        if not isinstance(d, dict) or d.get("bayer") != bayer or not isinstance(d.get("stats"), dict):
            return None
        return d["stats"]

    def flat_put(self, sha: str, bayer, stats: dict) -> None:
        self.put(sha, "flatstats", FLAT_VERSION, {"bayer": bayer, "stats": stats})
