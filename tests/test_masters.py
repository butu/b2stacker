"""Fertige Master-Kalibrierdateien (Bias/Dark/Flat) erkennen, bevorzugen und für WBPP als Master kennzeichnen."""
import numpy as np
from astropy.io import fits

from astrostack import calibcheck, discover, xisf
from astrostack.engines import pixinsight as pi
from astrostack.fitsmeta import BIAS, FLAT

from .conftest import NINA_LIGHT, make_fits
from .test_calibcheck import _flat_header, synth_flat


def test_masters_found_and_preferred(tmp_path):
    r = str(tmp_path)
    make_fits(f"{r}/L/2026-04-08/RGB/l.fits", dict(NINA_LIGHT, FILTER="RGB"), shape=(200, 300))
    hb = dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0, EXPTIME=0.0)
    hb.pop("OBJECT")
    for i in range(4):   # nur 4 rohe Bias …
        make_fits(f"{r}/Bias/raw/b{i}.fits", hb, shape=(200, 300), value=500)
    # … und ein fertiger Master (Dateiname masterBias, IMAGETYP nur „BIAS“)
    make_fits(f"{r}/Masters/masterBias_gain100.fits", hb, shape=(200, 300), value=500)
    # Master-Flat als XISF mit IMAGETYP „Master Flat“
    flat = synth_flat().astype(np.float32) / 65535
    kw = {k: v for k, v in _flat_header(FILTER="RGB").items()}
    kw["IMAGETYP"] = "Master Flat"
    kw["NAXIS1"], kw["NAXIS2"] = 300, 200
    xisf.write_image(f"{r}/Masters/masterFlat_RGB.xisf", flat, kw, color=False)
    inv = discover.scan([r])
    res = calibcheck.check_all(inv)
    m = res["matches"][0]
    assert m["bias"][0]["set"]["is_master"] and "fertiger Master" in m["bias"][0]["reasons"]
    fl = next(c for c in m["flats"] if c["usable"])
    assert fl["set"]["is_master"] and fl["set"]["frames"] == 1
    assert not any(f["title"].startswith("Sehr wenige Flats") for f in m["findings"])


def test_master_marked_for_wbpp(tmp_path):
    hb = dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0)
    p = make_fits(str(tmp_path / "Masters" / "masterBias.fits"), hb, shape=(20, 30), value=500)
    out = pi.fix_calibration([p], BIAS, str(tmp_path / "cache"))
    assert out[0] != p and fits.getheader(out[0])["IMAGETYP"] == "Master Bias"
    # schon korrekt gekennzeichnet → Original bleibt
    q = make_fits(str(tmp_path / "M2" / "masterFlat.fits"), dict(NINA_LIGHT, IMAGETYP="Master Flat"), shape=(20, 30))
    assert pi.fix_calibration([q], FLAT, str(tmp_path / "cache")) == [q]
    # XISF-Master ohne „Master“ im Typ → korrigierte XISF-Kopie
    x = str(tmp_path / "M3" / "masterDark_600s.xisf")
    import os
    os.makedirs(os.path.dirname(x))
    xisf.write_image(x, np.full((1, 20, 30), 0.01, np.float32), {"IMAGETYP": "Dark", "EXPTIME": 600})
    from astrostack.fitsmeta import DARK
    o = pi.fix_calibration([x], DARK, str(tmp_path / "cache"))[0]
    assert xisf.read_header(o)["IMAGETYP"] == "Master Dark" and xisf.read_image(o).shape == (1, 20, 30)
