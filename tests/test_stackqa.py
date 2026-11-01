"""Farbsaum-Korrektur, Aufteilen und Kennzahlen an einem synthetischen Master."""
import math
import os

import numpy as np

from astrostack import stackqa, xisf


def synth_master(shape=(600, 900), shift_r=(0.6, 0.3), shift_b=(-0.5, 0.0), fwhm=3.0, noise=0.0008, seed=0):
    rng = np.random.default_rng(seed)
    srng = np.random.default_rng(42)
    H, W = shape
    y, x = np.mgrid[0:H, 0:W]
    s = fwhm / 2.355
    chans = []
    stars = [(srng.uniform(20, H - 20), srng.uniform(20, W - 20), srng.uniform(0.02, 0.3)) for _ in range(150)]
    for (dy, dx) in (shift_r, (0, 0), shift_b):
        img = np.full(shape, 0.05)
        for cy, cx, a in stars:
            img += a * np.exp(-((y - cy - dy) ** 2 + (x - cx - dx) ** 2) / (2 * s * s))
        chans.append(img + rng.normal(0, noise, shape))
    return np.array(chans, dtype=np.float32)


def test_fringe_correction_and_kpis(tmp_path):
    m = str(tmp_path / "master.xisf")
    xisf.write_image(m, synth_master(), {"OBJECT": "vdB 15", "FILTER": "RGB"})
    run = dict(name="RGB", mode="debayer", hours=9.2, outputs=["RGB"], channel_map={})
    out = stackqa.process_master(m, run, "vdB 15", str(tmp_path / "final"), str(tmp_path / "pv"), "2026-11-01",
                                 pixscale=0.65, log=lambda *a: None)
    o = out[0]
    assert os.path.basename(o["file"]) == "vdB15_RGB_2026-11-01_9.2h.xisf"
    k = o["kpi"]
    assert k["fringe_before_px"] > 0.5 and k["fringe_px"] < 0.08, k
    assert 2.5 < k["fwhm_px"] < 3.8 and k["fwhm_as"] and k["stars"] > 50
    assert 0.5 < k["noise"] / (0.0008 * 65535) < 1.6
    assert os.path.exists(o["preview"])
    img = xisf.read_image(o["file"])
    assert img.shape == (3, 600, 900)
    assert "vdB 15" in xisf.read_header(o["file"]).get("OBJECT", "")


def test_split_haoiii_aligned(tmp_path):
    m = str(tmp_path / "master.xisf")
    xisf.write_image(m, synth_master(shift_r=(0.4, -0.4), shift_b=(0.0, 0.0)))
    run = dict(name="HaO3", mode="extract", hours=18.8, outputs=["Ha", "OIII"], channel_map={"Ha": "R", "OIII": "GB"})
    out = stackqa.process_master(m, run, "vdB 131", str(tmp_path / "final"), str(tmp_path / "pv"), "2026-11-01",
                                 log=lambda *a: None)
    names = [os.path.basename(o["file"]) for o in out]
    assert names == ["vdB131_Ha_2026-11-01_18.8h.xisf", "vdB131_OIII_2026-11-01_18.8h.xisf"]
    ha = xisf.read_image(out[0]["file"])[0]
    o3 = xisf.read_image(out[1]["file"])[0]
    _, S = stackqa.star_points(o3)
    c1 = stackqa._centroids(ha, S[:, :2])
    c2 = stackqa._centroids(o3, S[:, :2])
    d = np.nanmedian(c1 - c2, axis=0)
    assert math.hypot(*d) < 0.08, d


def test_no_correction_when_aligned(tmp_path):
    m = str(tmp_path / "master.xisf")
    xisf.write_image(m, synth_master(shift_r=(0.03, 0.0), shift_b=(0.0, -0.04)))
    run = dict(name="RGB", mode="debayer", hours=None, outputs=["RGB"], channel_map={})
    out = stackqa.process_master(m, run, "X", str(tmp_path / "f"), str(tmp_path / "p"), "2026-11-01", log=lambda *a: None)
    assert out[0]["kpi"]["fringe_px"] < 0.15 and out[0]["file"].endswith("X_RGB_2026-11-01.xisf")
