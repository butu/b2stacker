import math

import numpy as np

from astrostack import rotation


def field(angle_deg=0.0, shift=(0, 0), n=70, shape=(260, 390), seed=3, noise=2.0):
    rng = np.random.default_rng(seed)
    pts = rng.uniform([20, 20], [shape[0] - 20, shape[1] - 20], size=(n, 2))
    flux = rng.uniform(200, 3000, n)
    c = np.array(shape) / 2
    a = math.radians(angle_deg)
    R = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    p2 = (pts - c) @ R.T + c + shift
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    img = np.full(shape, 100.0)
    for (py, px), f in zip(p2, flux):
        if 3 < py < shape[0] - 3 and 3 < px < shape[1] - 3:
            img += f * np.exp(-((y - py) ** 2 + (x - px) ** 2) / (2 * 1.1 ** 2))
    return img + np.random.default_rng(seed + 1).normal(0, noise, shape)


def test_rotation_estimate():
    a = rotation.stars_from_thumb(field(0))
    for ang in (0.0, 3.0, -12.5, 91.0, 178.0):
        b = rotation.stars_from_thumb(field(ang, shift=(4, -6)))
        est, conf = rotation.estimate_rotation(a, b)
        assert est is not None and conf > 0.3
        assert abs(rotation.fold180(est + ang)) < 0.6, (ang, est)   # Vorzeichen umgekehrt, ±180° egal (gefaltet)


def test_meridian_flip_is_same_position():
    assert abs(rotation.fold180(179.5)) < 1 and abs(rotation.fold180(-178.2) - 1.8) < 1e-6


def test_group_by_header_and_sky():
    nights = ["2026-04-07", "2026-04-08", "2026-04-20"]
    g = rotation.group_nights(nights, {"2026-04-07": 273.5, "2026-04-08": 273.6, "2026-04-20": 93.4}, {})
    assert [x["nights"] for x in g] == [["2026-04-07", "2026-04-08"], ["2026-04-20"]] and g[0]["source"] == "rotator"
    meas = {"2026-04-07": dict(angle=0.0, conf=1), "2026-04-08": dict(angle=0.4, conf=.9), "2026-04-20": dict(angle=-25.0, conf=.8)}
    g = rotation.group_nights(nights, {}, meas)
    assert len(g) == 2 and g[1]["nights"] == ["2026-04-20"] and g[1]["source"] == "sky"
    # Meridian-Flip (≈180°) bleibt dieselbe Gruppe
    meas["2026-04-20"] = dict(angle=rotation.fold180(179.6), conf=.9)
    assert len(rotation.group_nights(nights, {}, meas)) == 1
