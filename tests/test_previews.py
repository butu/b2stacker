import csv
import hashlib
import os

import numpy as np
import pytest
from astropy.io import fits
from PIL import Image

from astrostack import analyze, catalog, previews  # noqa: F401  (analyze kept for thumb_name)
from astrostack.catalog import file_sig


def _light(path, value=20000, shape=(64, 96)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = np.full(shape, value, dtype=np.uint16)
    hdr = fits.Header({"IMAGETYP": "LIGHT", "EXPTIME": 60.0, "BAYERPAT": "RGGB"})
    fits.PrimaryHDU(data, header=hdr).writeto(path, overwrite=True)
    return path


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _write_analysis(adir, path, npy_value=20000):
    """Simulates a valid analysis result: metrics.csv row + thumbs/*.npy."""
    os.makedirs(os.path.join(adir, "thumbs"), exist_ok=True)
    cols = ["path", "file_sig", "metrics_ver"]
    with open(os.path.join(adir, "metrics.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerow({"path": path, "file_sig": file_sig(path), "metrics_ver": str(catalog.LIGHT_VERSION)})
    np.save(os.path.join(adir, "thumbs", analyze.thumb_name(path)),
            np.full((8, 12), npy_value, dtype=np.float32))


def _thumb_file(adir, path):
    return os.path.join(adir, "thumbs", analyze.thumb_name(path))


def _jpeg_size(p):
    with Image.open(p) as im:
        assert im.format == "JPEG"
        return im.size, im.mode


def test_npy_fast_path_does_not_load_original(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)

    def boom(*a, **k):
        raise AssertionError("imageio.load darf im npy-Pfad nicht laufen")
    monkeypatch.setattr(previews.imageio, "load", boom)

    out = previews.frame_thumbnail(path, adir, width=320)
    assert out.startswith(os.path.join(adir, "frame-thumbs"))
    size, mode = _jpeg_size(out)
    assert max(size) <= 320 and mode == "L"


def test_calibration_fallback_without_analysis_uses_preview(tmp_path):
    path = _light(str(tmp_path / "darks" / "d.fit"))
    adir = str(tmp_path / "analysis")
    out = previews.frame_thumbnail(path, adir, width=320)
    assert out.startswith(os.path.join(adir, "frame-thumbs"))
    size, mode = _jpeg_size(out)
    assert max(size) <= 320 and mode == "RGB"


def test_stale_metrics_signature_falls_back(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)
    _light(path, value=5000)  # file changed -> file_sig no longer matches
    calls = []
    real = previews.frame_preview
    monkeypatch.setattr(previews, "frame_preview", lambda *a, **k: calls.append(a) or real(*a, **k))
    previews.frame_thumbnail(path, adir, width=320)
    assert calls, "stale analysis thumbnail must not be used"


def test_same_path_changed_gets_new_output(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"), value=20000)
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path, npy_value=20000)
    first = previews.frame_thumbnail(path, adir, width=320)
    # Reanalysis with new content: new analysis thumbnail and new file signature.
    _light(path, value=40000, shape=(64, 100))
    _write_analysis(adir, path, npy_value=60000)
    second = previews.frame_thumbnail(path, adir, width=320)
    assert first != second
    assert os.path.exists(first) and os.path.exists(second)


def test_width_is_part_of_key(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)
    a = previews.frame_thumbnail(path, adir, width=320)
    b = previews.frame_thumbnail(path, adir, width=160)
    assert a != b


def test_failed_render_propagates_and_leaves_no_output(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)

    def fail(*a, **k):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(previews, "_jpeg_from_thumb", fail)
    with pytest.raises(RuntimeError):
        previews.frame_thumbnail(path, adir, width=320)
    tdir = os.path.join(adir, "frame-thumbs")
    assert not os.path.isdir(tdir) or os.listdir(tdir) == []


def test_change_during_render_stores_nothing_stale(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)
    real = previews._jpeg_from_thumb

    def render_then_change(arr, width):
        data = real(arr, width)
        _light(path, value=1000, shape=(64, 98))  # original changed while rendering
        return data
    monkeypatch.setattr(previews, "_jpeg_from_thumb", render_then_change)
    with pytest.raises(RuntimeError):
        previews.frame_thumbnail(path, adir, width=320)
    tdir = os.path.join(adir, "frame-thumbs")
    assert not os.path.isdir(tdir) or not [f for f in os.listdir(tdir) if f.endswith(".jpg")]


def test_original_file_unchanged(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)
    before = _sha(path)
    previews.frame_thumbnail(path, adir, width=320)
    assert _sha(path) == before


def test_cache_hit_returns_same_path(tmp_path):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)
    assert previews.frame_thumbnail(path, adir) == previews.frame_thumbnail(path, adir)


@pytest.mark.parametrize("payload", ["garbage", "wrong-shape"])
def test_corrupt_or_wrong_shape_npy_is_a_miss(tmp_path, payload):
    path = _light(str(tmp_path / "lights" / "a.fit"))
    adir = str(tmp_path / "analysis")
    _write_analysis(adir, path)
    if payload == "garbage":
        with open(_thumb_file(adir, path), "wb") as fh:
            fh.write(b"not a npy file at all")
    else:
        np.save(_thumb_file(adir, path), np.zeros(10, dtype=np.float32))  # 1-D, not an image
    out = previews.frame_thumbnail(path, adir, width=320)
    assert out.startswith(os.path.join(adir, "frame-thumbs"))
    size, mode = _jpeg_size(out)
    assert max(size) <= 320 and mode == "RGB"  # raw fallback


def test_frame_preview_downsamples_before_stretch(tmp_path, monkeypatch):
    path = _light(str(tmp_path / "lights" / "big.fit"), shape=(2400, 3200))
    seen = []
    real = previews.stretch

    def spy(a, *args, **kwargs):
        seen.append(a.shape)
        return real(a, *args, **kwargs)
    monkeypatch.setattr(previews, "stretch", spy)
    out = previews.frame_preview(path, str(tmp_path / "c"), width=320)
    assert seen and max(seen[0][-2:]) <= 2 * 320
    size, _ = _jpeg_size(out)
    assert max(size) <= 320


def test_cfa_planes_keep_phase_when_strided():
    raw = np.zeros((40, 60), dtype=np.float32)
    raw[0::2, 0::2] = 1000   # R sites for RGGB
    raw[1::2, 1::2] = 3000   # B sites
    raw[0::2, 1::2] = 2000   # G sites
    raw[1::2, 0::2] = 2000
    r, g, b = previews._cfa_planes(raw, "RGGB", k=3)
    assert np.all(r == 1000) and np.all(g == 2000) and np.all(b == 3000)
