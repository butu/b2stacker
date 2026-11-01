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
    """Nachbau von FLAT_2026-04-14: zwei Panel-Serien + Himmelsflats mit wechselnder Belichtung im selben Ordner."""
    r = str(tmp_path)
    make_fits(f"{r}/L/2026-04-08/RGB/l.fits", dict(NINA_LIGHT, FILTER="RGB", **{"DATE-OBS": "2026-04-08T21:00:00"}), shape=(200, 300))
    f = "%s/FLAT/FLAT_2026-04-14/RGB" % r
    for i in range(6):   # Panel 0,006 s um 15:23
        make_fits(f"{f}/p1_{i}.fits", _flat_header(FILTER="RGB", EXPOSURE=0.006, EXPTIME=0.006,
                                                   **{"DATE-OBS": "2026-04-14T15:23:%02d" % (i * 5)}), data=synth_flat())
    for i in range(8):   # Panel 0,062 s um 17:20
        make_fits(f"{f}/p2_{i}.fits", _flat_header(FILTER="RGB", EXPOSURE=0.062, EXPTIME=0.062,
                                                   **{"DATE-OBS": "2026-04-14T17:20:%02d" % (i * 5)}), data=synth_flat())
    for i in range(12):  # Himmelsflats: Belichtung steigt mit der Dämmerung, jede anders
        make_fits(f"{f}/s_{i}.fits", _flat_header(FILTER="RGB", EXPOSURE=0.69 * 1.12 ** i, EXPTIME=0.69 * 1.12 ** i,
                                                  **{"DATE-OBS": "2026-04-14T19:%02d:00" % (48 + i)}), data=synth_flat())
    inv = discover.scan([r])
    flats = [c for c in inv.calib if c.type == "flat"]
    assert sorted(len(c.frames) for c in flats) == [6, 8, 12], "Himmelsflats müssen eine Sitzung bilden"
    res = calibcheck.check_all(inv)
    m = res["matches"][0]
    best = m["flats"][0]
    assert best["usable"] and best["set"]["frames"] == 12 and best["set"]["sky"]
    titles = [f["title"] for f in m["findings"]]
    assert "Mehrere Flat-Sitzungen im gewählten Ordner" in titles


def test_single_frame_loses(tmp_path):
    r = str(tmp_path)
    make_fits(f"{r}/L/2026-04-08/RGB/l.fits", dict(NINA_LIGHT, FILTER="RGB"), shape=(200, 300))
    make_fits(f"{r}/FLAT/a/RGB/one.fits", _flat_header(FILTER="RGB", **{"DATE-OBS": "2026-10-13T10:00:00"}), data=synth_flat())
    for i in range(20):
        make_fits(f"{r}/FLAT/b/RGB/f{i}.fits", _flat_header(FILTER="RGB", **{"DATE-OBS": "2026-10-14T10:00:%02d" % i}), data=synth_flat())
    for i in range(20):  # verworfener Ordner zählt nicht
        make_fits(f"{r}/FLAT/c_verworfen/RGB/f{i}.fits", _flat_header(FILTER="RGB", **{"DATE-OBS": "2026-10-12T10:00:%02d" % i}), data=synth_flat())
    res = calibcheck.check_all(discover.scan([r]))
    best = res["matches"][0]["flats"][0]
    assert best["set"]["frames"] == 20 and "/b/" in best["set"]["folder"].replace("\\", "/")
    verw = next(c for c in res["matches"][0]["flats"] if "verworfen" in c["set"]["folder"])
    assert not verw["usable"]


def test_bias_from_other_driver_name_is_used(tmp_path):
    """Bias 2024 mit nativem ToupTek-Treiber (ATR3CMOS26000KPA), Lights 2026 mit ATR2600C(USB2.0): dieselbe Kamera."""
    r = str(tmp_path)
    make_fits(f"{r}/vdB 15/2026-04-08/RGB/l.fits", dict(NINA_LIGHT, FILTER="RGB"), shape=(200, 300))
    hb = dict(NINA_LIGHT, INSTRUME="ATR3CMOS26000KPA", EXPOSURE=0.0001, EXPTIME=0.0001, FILTER="RGB")
    hb.pop("OBJECT")
    for i in range(5):
        make_fits(f"{r}/Bias/SNAPSHOT/RGB/2024-11-05/b{i}.fits", hb, shape=(200, 300), value=500)
    hx = dict(hb, GAIN=0)          # falscher Gain bleibt unbrauchbar
    make_fits(f"{r}/Bias2/b.fits", hx, shape=(200, 300), value=500)
    res = calibcheck.check_all(discover.scan([r]))
    m = res["matches"][0]
    best = m["bias"][0]
    assert best["usable"] and best["set"]["frames"] == 5
    other = next(c for c in m["bias"] if "Bias2" in c["set"]["folder"])
    assert not other["usable"]
    assert "Kein Bias und keine Dark-Flats" not in [f["title"] for f in m["findings"]]
