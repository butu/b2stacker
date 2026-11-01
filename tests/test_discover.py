import os

from astrostack import discover, fitsmeta as fm, filters as flt


def test_type_from_string():
    assert fm.type_from_string("LIGHT") == (fm.LIGHT, False)
    assert fm.type_from_string("Light Frame") == (fm.LIGHT, False)
    assert fm.type_from_string("'Master Flat'") == (fm.FLAT, True)
    assert fm.type_from_string("Flat Dark") == (fm.DARKFLAT, False)
    assert fm.type_from_string("DARKFLAT") == (fm.DARKFLAT, False)
    assert fm.type_from_string("Bias Frame") == (fm.BIAS, False)
    assert fm.type_from_string("Offset") == (fm.BIAS, False)


def test_scan_groups(mixed_tree):
    inv = discover.scan([mixed_tree])
    s = inv.summary()
    lights = {(g["target"], g["filter"]): g for g in s["lights"]}
    assert ("vdB 131", "HaO3") in lights
    g = lights[("vdB 131", "HaO3")]
    assert g["frames"] == 4 and g["excluded"] == 1 and g["filter_kind"] == flt.DUAL_HA_OIII and g["is_osc"]
    assert g["nights"] == {"2026-10-12": 4}
    assert ("vdB 131", "RGB") in lights
    assert ("NGC 7000", "L-eXtreme") in lights
    # Snapshot übersprungen
    assert not any("Snapshot" in f.path for grp in inv.lights for f in grp.frames)
    types = {(c["type"], c["filter"]) for c in s["calib"]}
    assert ("bias", None) in types          # aus IMAGETYP=LIGHT + 0,1 ms erkannt
    assert ("flat", "HaO3") in types
    assert ("flat", "Ha") in types          # APT "Flat Frame"
    assert ("dark", None) in types          # Ekos FRAME=Dark und Ordner "Darks"
    bias = next(c for c in s["calib"] if c["type"] == "bias")
    assert bias["type_reasons"] == ["exposure"] and bias["warnings"]
    assert not inv.errors


def test_sources_and_meta(mixed_tree):
    inv = discover.scan([mixed_tree])
    srcs = {f.source for grp in inv.lights for f in grp.frames}
    assert "nina" in srcs and "asiair" in srcs
    m = next(f for grp in inv.lights for f in grp.frames if f.source == "nina" and f.filter == "HaO3")
    assert m.hfr_from_name is not None
    assert abs(m.pixel_scale - 0.646) < 0.01
    assert m.night == "2026-10-12"
    assert m.focus_pos == 2097 and m.rotator == 273.6


def test_target_guess_without_object(tmp_path):
    from tests.conftest import make_fits
    p = make_fits(str(tmp_path / "M 42" / "2026-01-01" / "RGB" / "a.fits"), {"IMAGETYP": "LIGHT", "EXPTIME": 60.0})
    inv = discover.scan([str(tmp_path)])
    assert inv.lights[0].target == "M 42"
    assert os.path.exists(p)


def test_bias_in_snapshot_folder_found(tmp_path):
    """NINA legt Bias oft unter Bias/SNAPSHOT/… ab – die müssen gefunden werden, Test-Lights dort nicht."""
    from .conftest import NINA_LIGHT, make_fits
    r = str(tmp_path)
    hb = dict(NINA_LIGHT, EXPOSURE=0.0001, EXPTIME=0.0001)
    hb.pop("OBJECT")
    for i in range(4):
        make_fits(f"{r}/Bias/SNAPSHOT/RGB/2024-11-05/0.00_Bias_0.00sec_gain100_22.{i}0c_000{i}.fits", hb)
    make_fits(f"{r}/Snapshot/2026-04-14/RGB/test.fits", NINA_LIGHT)
    inv = discover.scan([r])
    bias = [c for c in inv.calib if c.type == "bias"]
    assert len(bias) == 1 and len(bias[0].frames) == 4
    assert not inv.lights
