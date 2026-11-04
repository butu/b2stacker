import os

import numpy as np
import pytest
from astropy.io import fits


def make_fits(path, header: dict, shape=(32, 48), value=1000, data=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr = data if data is not None else np.full(shape, value, dtype=np.uint16)
    hdu = fits.PrimaryHDU(arr)
    for k, v in header.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)
    return path


NINA_LIGHT = {"IMAGETYP": "LIGHT", "EXPOSURE": 600.0, "EXPTIME": 600.0, "GAIN": 100, "OFFSET": 50,
              "READOUTM": "High Conversion Gain", "BAYERPAT": "RGGB", "INSTRUME": "ATR2600C(USB2.0)",
              "CCD-TEMP": -9.9, "SET-TEMP": -10.0, "FILTER": "HaO3", "OBJECT": "vdB 131",
              "DATE-OBS": "2026-10-12T20:37:00.000", "DATE-LOC": "2026-10-12T22:37:00.000",
              "FOCALLEN": 1200.0, "XPIXSZ": 3.76, "XBINNING": 1, "FOCPOS": 2097, "ROTATOR": 273.6,
              "SITELAT": 48.8575, "SITELONG": 15.015, "SWCREATE": "N.I.N.A. 3.2.0.9001 (x64)"}


@pytest.fixture
def mixed_tree(tmp_path):
    root = tmp_path / "data"
    r = str(root)
    # NINA lights in Objekt/Datum/Filter
    for i in range(3):
        make_fits(f"{r}/vdB 131/2026-10-12/HaO3/2.6{i}_vdB 131_600.00sec_gain100_-9.90c_000{i}.fits", NINA_LIGHT)
    # ausgeschlossener Frame
    make_fits(f"{r}/vdB 131/_rejected/2026-10-12/HaO3/4.13_vdB 131_600.00sec_gain100_-9.90c_0024.fits", NINA_LIGHT)
    # NINA RGB-Light anderer Nacht
    h = dict(NINA_LIGHT, FILTER="RGB", **{"DATE-OBS": "2026-07-20T21:00:00", "DATE-LOC": "2026-07-20T23:00:00"})
    make_fits(f"{r}/vdB 131/2026-07-20/RGB/2.40_vdB 131_600.00sec_gain100_-9.90c_0039.fits", h)
    # NINA Snapshot-Bias: IMAGETYP=LIGHT, 0.1 ms
    hb = dict(NINA_LIGHT, IMAGETYP="LIGHT", EXPOSURE=0.0001, EXPTIME=0.0001, FILTER="RGB")
    hb.pop("OBJECT")
    for i in range(2):
        make_fits(f"{r}/Bias/2024-11-05/0.00_Bias_0.00sec_gain100_23.40c_000{i}.fits", hb)
    # NINA Flats
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(2):
        make_fits(f"{r}/FLAT/FLAT_2026-10-31/HaO3/0.00__1.00sec_gain100_13.10c_000{i}.fits", hf)
    # ASIAIR-Light (andere Schreibweise, kein SWCREATE)
    ha = {"IMAGETYP": "Light", "EXPTIME": 300.0, "GAIN": 100, "OFFSET": 30, "BAYERPAT": "RGGB",
          "INSTRUME": "ZWO ASI2600MC Pro", "CCD-TEMP": -10.0, "FILTER": "L-eXtreme", "OBJECT": "NGC 7000",
          "DATE-OBS": "2026-09-01T22:00:00", "FOCALLEN": 400.0, "XPIXSZ": 3.76}
    make_fits(f"{r}/Autorun/Light/NGC 7000/Light_NGC 7000_300.0s_Bin1_2600MC_gain100_20260902-000000_-10.0C_0001.fit", ha)
    # Ekos-Dark (FRAME statt IMAGETYP)
    he = {"FRAME": "Dark", "EXPTIME": 300.0, "GAIN": 100, "OFFSET": 30, "INSTRUME": "ZWO CCD ASI2600MC Pro",
          "BAYERPAT": "RGGB", "CCD-TEMP": -10.0, "DATE-OBS": "2026-09-02T10:00:00", "SWCREATE": "KStars"}
    make_fits(f"{r}/darks/Dark_300_secs_0001.fits", he)
    # APT-Flat ("Flat Frame")
    hp = {"IMAGETYP": "Flat Frame", "EXPTIME": 2.0, "GAIN": 0, "FILTER": "Ha", "INSTRUME": "ASI1600MM",
          "SWCREATE": "Astro Photography Tool - APT v.4"}
    make_fits(f"{r}/apt/flat_Ha_001.fits", hp)
    # Datei ohne Typ im Header, Typ nur aus Ordner
    make_fits(f"{r}/misc/Darks/frame_0001.fits", {"EXPTIME": 120.0, "GAIN": 100})
    # Snapshot-Ordner wird übersprungen
    make_fits(f"{r}/Snapshot/2026-04-14/RGB/x.fits", NINA_LIGHT)
    return r


os.environ.setdefault("B2_NO_UPDATE_CHECK", "1")   # Tests nie gegen GitHub prüfen
os.environ["B2_APP_URL"] = "http://127.0.0.1:9/"   # nie mit einer echten, laufenden b2stacker-App sprechen


@pytest.fixture(autouse=True)
def _isolated_preview_cache(tmp_path, monkeypatch):
    # Default cache otherwise lives in the user profile; every test gets a throwaway cache.
    monkeypatch.setenv("ASTROSTACK_PREVIEW_CACHE", str(tmp_path / "preview-cache"))
