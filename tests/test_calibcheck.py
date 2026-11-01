import numpy as np

from astrostack import calibcheck, discover
from tests.conftest import NINA_LIGHT, make_fits


def synth_flat(h=200, w=300, g_center=33000, rb=(0.45, 0.55), vign=0.75, rb_extra_falloff=0.0):
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(yy - h / 2, xx - w / 2) / np.hypot(h / 2, w / 2)
    prof = 1 - (1 - vign) * r ** 2
    img = np.zeros((h, w), np.float32)
    G = g_center * prof
    R = G * rb[0] * (1 - rb_extra_falloff * r ** 2)
    B = G * rb[1] * (1 - rb_extra_falloff * r ** 2)
    img[0::2, 0::2] = R[0::2, 0::2]
    img[0::2, 1::2] = G[0::2, 1::2]
    img[1::2, 0::2] = G[1::2, 0::2]
    img[1::2, 1::2] = B[1::2, 1::2]
    return np.clip(img, 0, 65535).astype(np.uint16)


def _flat_header(**kw):
    h = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    h.pop("OBJECT")
    h.update(kw)
    return h


def test_good_and_bad_flats(tmp_path):
    r = str(tmp_path)
    for i in range(3):
        make_fits(f"{r}/L/2026-10-12/HaO3/l{i}.fits", NINA_LIGHT, shape=(200, 300))
        make_fits(f"{r}/FLAT/good/HaO3/f{i}.fits", _flat_header(**{"DATE-LOC": "2026-10-31T18:00:00"}),
                  data=synth_flat())
        make_fits(f"{r}/FLAT/bright/HaO3/f{i}.fits", _flat_header(EXPOSURE=1.4, EXPTIME=1.4, **{"DATE-LOC": "2026-10-31T16:00:00"}),
                  data=synth_flat(g_center=50500, rb_extra_falloff=0.06))
        make_fits(f"{r}/FLAT/lcg/HaO3/f{i}.fits", _flat_header(READOUTM="Low Conversion Gain"), data=synth_flat())
    inv = discover.scan([r])
    res = calibcheck.check_all(inv)
    v = {k.split("|")[0].split("FLAT")[1]: c["verdict"] for k, c in res["flat_checks"].items()}
    assert any("good" in k and val == "good" for k, val in v.items())
    assert any("bright" in k and val == "critical" for k, val in v.items())
    m = res["matches"][0]
    best = m["flats"][0]
    assert "good" in best["set"]["folder"] and best["usable"]
    lcg = next(c for c in m["flats"] if "lcg" in c["set"]["folder"])
    assert not lcg["usable"] and any("Auslesemodus" in x for x in lcg["reasons"])


def test_mixed_series_warning(tmp_path):
    r = str(tmp_path)
    make_fits(f"{r}/L/2026-10-12/HaO3/l.fits", NINA_LIGHT, shape=(200, 300))
    make_fits(f"{r}/FLAT/x/HaO3/a.fits", _flat_header(EXPOSURE=1.0, EXPTIME=1.0), data=synth_flat())
    make_fits(f"{r}/FLAT/x/HaO3/b.fits", _flat_header(EXPOSURE=1.37, EXPTIME=1.37), data=synth_flat(g_center=47000))
    inv = discover.scan([r])
    res = calibcheck.check_all(inv)
    titles = [f["title"] for f in res["matches"][0]["findings"]]
    assert "Mehrere Flat-Serien im selben Ordner" in titles
