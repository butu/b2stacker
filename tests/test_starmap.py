"""Bericht 2 §15: Sterne und Hotpixel eines Frames für das Overlay."""
import numpy as np

from astrostack import starmap

from .conftest import NINA_LIGHT, make_fits
from .test_e2e import star_field


def test_stars_in_original_pixels_and_cached(tmp_path):
    p = str(tmp_path / "L.fits")
    data = star_field(3)
    data = np.array(data, copy=True)
    data[100, 101] = 60000                                   # ein Hotpixel
    make_fits(p, dict(NINA_LIGHT, OBJECT="X"), data=data)
    d = starmap.frame_stars(p, str(tmp_path / "c"), "RGGB", hot=True)
    assert d["width"] == data.shape[1] and d["height"] == data.shape[0]
    assert 10 <= len(d["stars"]) <= starmap.MAX_STARS
    x, y, f, e, a = d["stars"][0]
    assert 0 <= x < d["width"] and 0 <= y < d["height"] and f > 0 and 0 <= e < 1
    assert d["hot_count"] >= 1 and [101, 100] in d["hot"]
    again = starmap.frame_stars(p, str(tmp_path / "c"), "RGGB")
    assert again == d                                         # aus dem Cache


def test_distortion_matches_staralignment_rule():
    assert abs(starmap.distortion(0) - 0.785) < 0.001
    assert 0.58 < starmap.distortion(0.66) < 0.60          # NGC 3486: unter WBPP-Grenze 0,6 → verworfen
