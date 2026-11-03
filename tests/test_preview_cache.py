"""Persistent, offline-capable preview cache: prepare once, then serve without touching originals."""
import json
import os

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image

from astrostack import analyze, catalog, previews
from astrostack.catalog import file_sig


def _light(path, value=20000, shape=(64, 96)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = np.full(shape, value, dtype=np.uint16)
    data[::7, ::5] += 5000
    hdr = fits.Header({"IMAGETYP": "LIGHT", "EXPTIME": 60.0, "BAYERPAT": "RGGB"})
    fits.PrimaryHDU(data, header=hdr).writeto(path, overwrite=True)
    return path


def _frame(path, bayer="RGGB"):
    from astrostack.fitsmeta import FrameMeta
    return FrameMeta(path=path, bayer=bayer)


def _manifest(adir):
    with open(os.path.join(adir, "previews.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _record(adir, path):
    return _manifest(adir)["frames"][path]


def _jpeg_size(p):
    with Image.open(p) as im:
        assert im.format == "JPEG"
        return im.size


def _no_original_io(monkeypatch, path):
    real_stat = os.stat

    def guard(p, *a, **k):
        if os.path.abspath(str(p)) == os.path.abspath(path):
            raise AssertionError("cached read touched the original")
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "stat", guard)
    monkeypatch.setattr(previews.catalog, "file_sig", lambda p: (_ for _ in ()).throw(AssertionError("file_sig")))
    monkeypatch.setattr(previews.imageio, "load", lambda p: (_ for _ in ()).throw(AssertionError("imageio.load")))


def test_prepare_frames_records_thumb_screen_and_dimensions(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"), shape=(400, 600))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)

    rec = _record(adir, path)
    assert rec["variants"]["thumb"]["width"] <= 320
    assert rec["variants"]["screen"]["width"] <= 1920
    assert (rec["orig_width"], rec["orig_height"]) == (600, 400)
    for variant in ("thumb", "screen"):
        v = rec["variants"][variant]
        assert not os.path.isabs(v["path"]), "manifest paths stay cache-relative"
        media = previews.cached_media(path, adir, variant, preview_cache_dir=cache)
        assert media["key"] == rec["key"]
        assert os.path.getsize(media["path"]) > 0
        assert _jpeg_size(media["path"]) == (media["width"], media["height"])
        assert (media["width"], media["height"]) == (v["width"], v["height"])
        assert media["render"] == "cfa-superpixel"


def test_large_frame_screen_is_bounded_to_display_size(tmp_path):
    path = _light(str(tmp_path / "lights" / "big.fit"), shape=(2400, 3200))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    thumb = previews.cached_media(path, adir, "thumb", preview_cache_dir=cache)
    screen = previews.cached_media(path, adir, "screen", preview_cache_dir=cache)
    assert max(thumb["width"], thumb["height"]) == 320
    assert max(screen["width"], screen["height"]) == 1920
    assert (screen["orig_width"], screen["orig_height"]) == (3200, 2400)


def test_prepare_frames_accepts_project_frame_dicts(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([{"path": path}], adir, preview_cache_dir=cache)   # frame index has no bayer
    media = previews.cached_media(path, adir, preview_cache_dir=cache)
    assert media is not None and media["bayer"] == "RGGB"


def test_legacy_frame_dict_reuses_manifest_bayer_without_header_read(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    monkeypatch.setattr(previews.fitsmeta, "read_meta",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("header read")))
    previews.prepare_frames([{"path": path}], adir, preview_cache_dir=cache)
    assert previews.cached_media(path, adir, preview_cache_dir=cache)["bayer"] == "RGGB"


def test_legacy_frame_dict_uses_cached_header_bayer(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    cat_dir = str(tmp_path / "catalog")
    catalog.Catalog(cat_dir).header(path)      # persisted header cache from the scan
    monkeypatch.setattr(previews.fitsmeta, "read_meta",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("header read")))
    previews.prepare_frames([{"path": path}], adir, preview_cache_dir=cache, catalog_dir=cat_dir)
    assert previews.cached_media(path, adir, preview_cache_dir=cache)["bayer"] == "RGGB"


def test_cached_media_after_original_root_renamed_does_no_original_io(tmp_path, monkeypatch):
    root = tmp_path / "nas"
    path = _light(str(root / "night" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    os.rename(str(root), str(tmp_path / "nas-renamed"))

    _no_original_io(monkeypatch, path)
    for variant in ("thumb", "screen"):
        media = previews.cached_media(path, adir, variant, preview_cache_dir=cache)
        assert media and os.path.isfile(media["path"])
    assert previews.cached_media(str(tmp_path / "elsewhere.fit"), adir, preview_cache_dir=cache) is None


def test_same_content_on_another_path_shares_cache_entry(tmp_path):
    path = _light(str(tmp_path / "one" / "a.fit"))
    copy = str(tmp_path / "two" / "copy.fit")
    os.makedirs(os.path.dirname(copy), exist_ok=True)
    with open(path, "rb") as src, open(copy, "wb") as dst:
        dst.write(src.read())
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path), _frame(copy)], adir, preview_cache_dir=cache)
    a, b = _record(adir, path), _record(adir, copy)
    assert a["key"] == b["key"]
    assert previews.cached_media(path, adir, preview_cache_dir=cache)["path"] == \
        previews.cached_media(copy, adir, preview_cache_dir=cache)["path"]


def test_missing_cache_is_none_and_not_rendered_lazily(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    other = _light(str(tmp_path / "lights" / "b.fit"), value=1000)
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    monkeypatch.setattr(previews.imageio, "load", lambda p: (_ for _ in ()).throw(AssertionError("lazy read")))
    assert previews.cached_media(other, adir, preview_cache_dir=cache) is None
    assert previews.cached_media(path, adir, "full", preview_cache_dir=cache) is None


def test_changed_content_gets_new_key_on_next_explicit_prepare(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"), value=20000, shape=(64, 96))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    old = dict(_record(adir, path))
    _light(path, value=5000, shape=(80, 120))
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    new = _record(adir, path)
    assert new["key"] != old["key"]
    assert new["variants"]["thumb"]["width"] != old["variants"]["thumb"]["width"]
    assert os.path.exists(os.path.join(cache, old["variants"]["thumb"]["path"]))
    assert previews.cached_media(path, adir, preview_cache_dir=cache)["key"] == new["key"]


def test_corrupt_manifest_is_miss_and_repair_needs_explicit_prepare(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    with open(os.path.join(adir, "previews.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert previews.cached_media(path, adir, preview_cache_dir=cache) is None
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    assert previews.cached_media(path, adir, preview_cache_dir=cache) is not None


def test_truncated_cached_jpeg_is_a_miss_and_repairs_on_prepare(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    media = previews.cached_media(path, adir, "thumb", preview_cache_dir=cache)
    with open(media["path"], "wb") as fh:
        fh.write(b"kaputt")
    assert previews.cached_media(path, adir, "thumb", preview_cache_dir=cache) is None
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    assert previews.cached_media(path, adir, "thumb", preview_cache_dir=cache) is not None


def test_broken_frame_is_recorded_without_aborting_others(tmp_path):
    good = _light(str(tmp_path / "lights" / "good.fit"))
    bad = str(tmp_path / "lights" / "bad.fit")
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    with open(bad, "wb") as fh:
        fh.write(b"not a fits file")
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(good), _frame(bad)], adir, preview_cache_dir=cache)
    assert previews.cached_media(good, adir, preview_cache_dir=cache) is not None
    assert previews.cached_media(bad, adir, preview_cache_dir=cache) is None
    assert _record(adir, bad)["errors"]


def test_tampered_manifest_cannot_escape_cache_root(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    man = _manifest(adir)
    man["frames"][path]["key"] = "../../outside"
    with open(os.path.join(adir, "previews.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    assert previews.cached_media(path, adir, preview_cache_dir=cache) is None


def _interleave_full(monkeypatch, trigger, target, adir, cache):
    """Run full_prepare between prepare's manifest read and its write, with a separate manifest snapshot."""
    real = previews._record_from_raw
    fired = []

    def wrapper(*args, **kwargs):
        record = real(*args, **kwargs)
        if args[0] == trigger and not fired:
            fired.append(True)
            previews._MANIFEST_CACHE.clear()
            previews._MANIFEST_INDEX.clear()
            previews.full_prepare(target, adir, preview_cache_dir=cache)
        return record
    monkeypatch.setattr(previews, "_record_from_raw", wrapper)


def _drop_screen(path, adir, cache):
    media = previews.cached_media(path, adir, "screen", preview_cache_dir=cache)
    os.remove(media["path"])   # force the next prepare to render the frame again


def test_interleaved_full_prepare_same_frame_survives_prepare_write(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    frame = {"path": path, "bayer": None}   # legacy frame dict shape from the repro
    previews.prepare_frames([frame], adir, preview_cache_dir=cache)
    _drop_screen(path, adir, cache)
    _interleave_full(monkeypatch, path, path, adir, cache)
    previews.prepare_frames([frame], adir, preview_cache_dir=cache)

    os.remove(path)
    full = previews.cached_media(path, adir, "full", preview_cache_dir=cache)
    assert full is not None and os.path.isfile(full["path"])
    assert previews.cached_media(path, adir, "screen", preview_cache_dir=cache) is not None


def test_interleaved_full_prepare_other_frame_survives_prepare_write(tmp_path, monkeypatch):
    a = _light(str(tmp_path / "lights" / "a.fit"))
    b = _light(str(tmp_path / "lights" / "b.fit"), value=30000)
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(a), _frame(b)], adir, preview_cache_dir=cache)
    _drop_screen(a, adir, cache)
    _interleave_full(monkeypatch, a, b, adir, cache)
    previews.prepare_frames([_frame(a)], adir, preview_cache_dir=cache)

    os.remove(a)
    os.remove(b)
    full_b = previews.cached_media(b, adir, "full", preview_cache_dir=cache)
    assert full_b is not None and os.path.isfile(full_b["path"])
    assert previews.cached_media(a, adir, "screen", preview_cache_dir=cache) is not None


def test_changed_content_does_not_inherit_old_full_record(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"), value=20000, shape=(64, 96))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    old_full = previews.full_prepare(path, adir, preview_cache_dir=cache)
    old_key = old_full["key"]

    _light(path, value=5000, shape=(80, 120))
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    rec = _record(adir, path)
    assert rec["key"] != old_key
    assert "full" not in rec["variants"], "old content-key full media must not be merged"
    assert previews.cached_media(path, adir, "full", preview_cache_dir=cache) is None
    os.remove(path)
    assert previews.cached_media(path, adir, "screen", preview_cache_dir=cache) is not None


def test_bulk_cached_media_parses_manifest_once(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    man = _manifest(adir)
    missing = ["/missing/%04d.fit" % i for i in range(1000)]
    for name in missing:
        man["frames"][name] = {"key": "0" * 24, "variants": {}, "errors": {"prepare": "fehlt"}}
    with open(os.path.join(adir, "previews.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    previews._MANIFEST_CACHE.clear()

    calls = []
    real_load = previews.json.load

    def counted_load(fh, *a, **k):
        calls.append(1)
        return real_load(fh, *a, **k)
    monkeypatch.setattr(previews.json, "load", counted_load)

    media = previews.cached_media_bulk([path] + missing, adir, preview_cache_dir=cache)
    assert len(calls) == 1, "one manifest parse for the whole frame list"
    assert media[path]["thumb"] is not None and media[path]["screen"] is not None
    assert media[path]["full"] is None
    assert all(media[name]["screen"] is None for name in missing)
    for name in missing[:20]:
        assert previews.cached_media(name, adir, preview_cache_dir=cache) is None
    assert len(calls) == 1, "single lookups reuse the memoized manifest"


def test_full_prepare_is_explicit_and_works_offline(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"), shape=(64, 96))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    assert previews.cached_media(path, adir, "full", preview_cache_dir=cache) is None

    rec = previews.full_prepare(path, adir, preview_cache_dir=cache)
    assert rec["variant"] == "full" and rec["width"] == 96 and rec["height"] == 64
    assert (rec["orig_width"], rec["orig_height"]) == (96, 64)
    assert rec["render"] == "cfa-bilinear-native"

    os.remove(path)  # NAS/OneDrive asleep or drive removed
    media = previews.cached_media(path, adir, "full", preview_cache_dir=cache)
    assert media and os.path.isfile(media["path"])
    assert media["render"] == "cfa-bilinear-native"
    with pytest.raises(previews.PreviewError):
        previews.full_prepare(str(tmp_path / "gone.fit"), adir, preview_cache_dir=cache)


def test_full_zoom_keeps_odd_native_dimensions(tmp_path):
    path = _light(str(tmp_path / "lights" / "odd.fit"), shape=(65, 97))
    adir, cache = str(tmp_path / "analysis"), str(tmp_path / "cache")
    previews.prepare_frames([_frame(path)], adir, preview_cache_dir=cache)
    rec = previews.full_prepare(path, adir, preview_cache_dir=cache)
    assert (rec["width"], rec["height"]) == (97, 65)
    with Image.open(rec["path"]) as im:
        assert im.size == (97, 65) and im.mode == "RGB"


def test_cfa_full_resolution_keeps_native_green_detail():
    raw = np.zeros((16, 16), dtype=np.float32)
    raw[0::2, 0::2] = 5000       # R sites
    raw[1::2, 1::2] = 5000       # B sites
    raw[0::2, 1::2] = 1000       # G1: would be flattened by superpixel averaging
    raw[1::2, 0::2] = 30000      # G2
    im = previews._cfa_full_image(raw, "RGGB")
    arr = np.asarray(im)
    assert arr.shape == (16, 16, 3)
    assert arr[0, 1, 1] != arr[1, 0, 1], "native G sites must not be averaged away"


def test_full_stretch_matches_shared_stretch():
    from astrostack.stackqa import stretch
    rng = np.random.default_rng(7)
    a = np.clip(rng.normal(0.2, 0.08, (3, 32, 48)), 0, 1).astype(np.float32)
    lo, beta = previews._stretch_params(a)
    assert np.allclose(previews._apply_stretch(a, lo, beta), stretch(a, target=0.2), atol=1e-6)


def _inline_pool(monkeypatch):
    """Run analyze workers in-process so monkeypatched decoders actually apply."""
    class Inline:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, items):
            return map(fn, items)
    monkeypatch.setattr(analyze, "ProcessPoolExecutor", Inline)


def test_analyze_group_prepares_previews_with_single_original_load(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    cat_dir = str(tmp_path / "catalog")
    cat = catalog.Catalog(cat_dir)
    cat.resolve(path)  # warm the content hash like a scan does
    _inline_pool(monkeypatch)

    loads = []
    real = previews.imageio.load

    def counted(p):
        loads.append(p)
        return real(p)
    monkeypatch.setattr(previews.imageio, "load", counted)

    analyze.analyze_group([_frame(path)], adir, workers=1, catalog_dir=cat_dir)
    assert loads.count(path) == 1, "previews must share the measurement read"
    assert previews.cached_media(path, adir, "thumb") is not None
    assert previews.cached_media(path, adir, "screen") is not None


def test_old_npy_only_analysis_needs_one_explicit_prepare(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    cat_dir = str(tmp_path / "catalog")
    cache = str(tmp_path / "cache")
    analyze.analyze_group([_frame(path)], adir, workers=1, catalog_dir=cat_dir, preview_cache_dir=cache)
    os.remove(os.path.join(adir, "previews.json"))   # 0.6-era project: metrics + npy, no browse manifest
    assert previews.cached_media(path, adir, preview_cache_dir=cache) is None

    loads = []
    real = previews.imageio.load

    def counted(p):
        loads.append(p)
        return real(p)
    monkeypatch.setattr(previews.imageio, "load", counted)
    analyze.analyze_group([_frame(path)], adir, workers=1, catalog_dir=cat_dir, preview_cache_dir=cache)
    assert loads == [path], "one deliberate preparation read, then cached"
    assert previews.cached_media(path, adir, preview_cache_dir=cache) is not None


def test_analyze_group_keeps_measurement_when_preview_render_fails(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _inline_pool(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("renderer kaputt")
    monkeypatch.setattr(previews, "_render_image", boom)
    analyze.analyze_group([_frame(path)], adir, workers=1)
    errs = json.load(open(os.path.join(adir, "measure_errors.json"), encoding="utf-8"))
    assert errs == []
    assert _record(adir, path)["errors"]
