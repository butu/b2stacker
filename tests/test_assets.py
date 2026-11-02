"""Assets: automatisch gefundene oder verknüpfte PSD/TIFF/JPEG-Dateien mit Pillow-Vorschau."""
import os

import pytest
from PIL import Image

from astrostack import assets


def _pdir(tmp_path):
    p = tmp_path / "work" / "projects" / "M31"
    (p / "final").mkdir(parents=True)
    (p / "stacks").mkdir()
    (p / "analysis").mkdir()
    return str(p)


def test_auto_discovery_root_and_final_only(tmp_path):
    pdir = _pdir(tmp_path)
    Image.new("RGB", (40, 30), (200, 80, 10)).save(os.path.join(pdir, "final", "m31_rgb.jpg"))
    Image.new("RGB", (40, 30)).save(os.path.join(pdir, "crop.tif"))
    open(os.path.join(pdir, "notes.txt"), "w").write("x")
    open(os.path.join(pdir, "stacks", "hidden.jpg"), "wb").write(b"x")
    names = sorted(a["name"] for a in assets.discover(pdir))
    assert names == ["crop.tif", "m31_rgb.jpg"]


def test_auto_discovery_in_declared_source_roots_only_top_levels(tmp_path):
    import json
    pdir = _pdir(tmp_path)
    data = tmp_path / "data"
    (data / "final").mkdir(parents=True)
    (data / "lights" / "deep").mkdir(parents=True)
    Image.new("RGB", (20, 20)).save(data / "Andromeda.jpg")
    Image.new("RGB", (20, 20)).save(data / "final" / "rgb.tif")
    Image.new("RGB", (20, 20)).save(data / "lights" / "nope.jpg")       # Unterordner: nicht scannen
    Image.new("RGB", (20, 20)).save(data / "lights" / "deep" / "nope.tif")
    open(data / "lights" / "L_0001.fits", "wb").write(b"SIMPLE")         # Lights sind nie Assets
    json.dump({"roots": [str(data)], "target": "M31"}, open(os.path.join(pdir, "sources.json"), "w"))
    found = {a["name"]: a for a in assets.discover(pdir)}
    assert set(found) == {"Andromeda.jpg", "rgb.tif"}
    assert all(a["kind"] == "auto" for a in found.values())
    locs = assets.scan_locations(pdir)
    assert str(data) in locs and str(data / "final") in locs
    assert not any("lights" in loc for loc in locs)


def test_link_outside_project_and_reject_invalid(tmp_path):
    pdir = _pdir(tmp_path)
    ext = tmp_path / "elsewhere"
    ext.mkdir()
    f = ext / "stack.xisf"
    f.write_bytes(b"XISF0100")
    a = assets.link(pdir, str(f))
    assert a["exists"] and a["path"] == str(f)
    assert any(x["id"] == a["id"] for x in assets.discover(pdir))
    with pytest.raises(ValueError):
        assets.link(pdir, str(ext / "fehlt.jpg"))
    with pytest.raises(ValueError):
        assets.link(pdir, str(ext))
    with pytest.raises(ValueError):
        assets.link(pdir, str(ext / "notes.txt"))


def test_preview_is_cached_and_never_changes_original(tmp_path):
    pdir = _pdir(tmp_path)
    src = os.path.join(pdir, "final", "big.jpg")
    Image.new("RGB", (2000, 1000), (10, 20, 30)).save(src)
    before = (open(src, "rb").read(), os.path.getmtime(src))
    a = assets.discover(pdir)[0]
    path, err = assets.preview(pdir, a["id"])
    assert err is None and path.startswith(os.path.join(pdir, "analysis"))
    with Image.open(path) as im:
        assert max(im.size) <= assets.PREVIEW_MAX
    assert (open(src, "rb").read(), os.path.getmtime(src)) == before


def test_16bit_tiff_preview(tmp_path):
    pdir = _pdir(tmp_path)
    Image.new("I", (64, 48), 30000).save(os.path.join(pdir, "stretch.tif"))
    path, err = assets.preview(pdir, assets.discover(pdir)[0]["id"])
    assert err is None and os.path.exists(path)


def test_broken_psd_reports_error_and_keeps_file(tmp_path):
    pdir = _pdir(tmp_path)
    bad = os.path.join(pdir, "final", "kaputt.psd")
    open(bad, "wb").write(b"8BPS-kaputt")
    before = open(bad, "rb").read()
    a = next(x for x in assets.discover(pdir) if x["name"] == "kaputt.psd")
    path, err = assets.preview(pdir, a["id"])
    assert path is None and err
    assert open(bad, "rb").read() == before


PSD_FIXTURE = "/tmp/opencode/b2stacker-smoke-mtkzL4/images/Andromeda.psd"


def test_real_psd_composite_preview_if_fixture_present(tmp_path):
    import shutil
    if not os.path.exists(PSD_FIXTURE):
        pytest.skip("PSD-Fixture nicht vorhanden")
    pdir = _pdir(tmp_path)
    src = os.path.join(pdir, "final", "Andromeda.psd")
    shutil.copy(PSD_FIXTURE, src)        # Kopie nur für den Test – das Original wird nicht angefasst
    before = open(src, "rb").read()
    a = next(x for x in assets.discover(pdir) if x["name"] == "Andromeda.psd")
    path, err = assets.preview(pdir, a["id"])
    assert err is None, err
    with Image.open(path) as im:
        assert im.format == "JPEG" and max(im.size) <= assets.PREVIEW_MAX
    assert open(src, "rb").read() == before


def test_unknown_id_is_not_served(tmp_path):
    pdir = _pdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        assets.preview(pdir, "../../etc/passwd")
