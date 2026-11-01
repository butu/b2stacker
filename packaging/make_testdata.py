"""Erzeugt synthetische NINA-Daten (Lights RGB/HaO3, Flats, Snapshot-Bias) für den Selbsttest der exe."""
import sys

import numpy as np
from astropy.io import fits

NINA = {"IMAGETYP": "LIGHT", "EXPOSURE": 600.0, "EXPTIME": 600.0, "GAIN": 100, "OFFSET": 50,
        "READOUTM": "High Conversion Gain", "BAYERPAT": "RGGB", "INSTRUME": "ATR2600C(USB2.0)",
        "CCD-TEMP": -9.9, "SET-TEMP": -10.0, "OBJECT": "vdB 131", "FOCALLEN": 1200.0, "XPIXSZ": 3.76,
        "XBINNING": 1, "SITELAT": 48.8575, "SITELONG": 15.015, "SWCREATE": "N.I.N.A. 3.2"}


def write(path, hdr, data):
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = fits.PrimaryHDU(data)
    for k, v in hdr.items():
        h.header[k] = v
    h.writeto(path, overwrite=True)


def stars(seed, shape=(256, 384)):
    rng, srng = np.random.default_rng(seed), np.random.default_rng(1)
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    img = np.full(shape, 1200.0)
    for _ in range(60):
        cy, cx = srng.uniform(10, shape[0] - 10), srng.uniform(10, shape[1] - 10)
        img += srng.uniform(2000, 20000) * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * 1.3 ** 2))
    return np.clip(img + rng.normal(0, 15, shape), 0, 65535).astype(np.uint16)


root = sys.argv[1]
for f in ("RGB", "HaO3"):
    for i in range(5):
        write(f"{root}/vdB 131/2026-10-12/{f}/L_{f}_{i:04d}.fits",
              dict(NINA, FILTER=f, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)}), stars(i))
    for i in range(3):
        write(f"{root}/FLAT/FLAT_2026-10-31/{f}/flat_{i:04d}.fits",
              dict(NINA, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0, FILTER=f), np.full((256, 384), 30000, np.uint16))
for i in range(3):
    write(f"{root}/Bias/b_{i:04d}.fits", dict(NINA, EXPOSURE=0.0001, EXPTIME=0.0001, FILTER="RGB"),
          np.full((256, 384), 500, np.uint16))
print("Testdaten in", root)
