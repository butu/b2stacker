"""Finished masters as calibration: use directly instead of restacking (Siril run against fake_siril)."""
import os
import sys

import numpy as np
import pytest
from astropy.io import fits

from astrostack import engines
from astrostack.engines import siril

from .test_siril_run import cli, digest, frames, make_plan, settings  # noqa: F401 – Fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Wrapper ist ein Shell-Skript")


def _master(path, kind_header=None):
    h = fits.Header()
    if kind_header:
        h["IMAGETYP"] = kind_header
    fits.PrimaryHDU(data=np.full((60, 80), 0.1, np.float32), header=h).writeto(str(path), overwrite=True)
    return str(path)


def test_app_master_names_are_recognised(tmp_path):
    # APP masters are named MB-…/MD-… and often lack IMAGETYP → still masters
    mb = _master(tmp_path / "MB-2026-E_0s.fit")
    md = _master(tmp_path / "MD-2026-E_300s.fit")
    assert siril._is_master(mb) and siril._is_master(md)


def test_raw_frames_are_not_taken_for_masters(tmp_path):
    raw = frames(tmp_path / "bias", 1, "bias")[0]
    assert not siril._is_master(raw)
    assert siril._calib_spec([raw], "bias", str(tmp_path), lambda *a: None)["raw"] == str(tmp_path)


@posix_only
def test_master_dark_and_bias_are_used_without_restacking(tmp_path, cli):
    plan, run, lights = make_plan(tmp_path, bias=0, darks=0, flats=3)
    master_bias = _master(tmp_path / "MB-2026-E_0s.fit")
    master_dark = _master(tmp_path / "masterDark.fit", "Master Dark")
    run["bias"], run["darks"] = [master_bias], [master_dark]
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    log = open(os.path.join(os.path.dirname(st.runs["L"]["master"]), "siril.ssf"), encoding="utf-8").read()
    # APP bias without IMAGETYP is provided as a corrected cache copy → check prefix/name only
    assert '"-bias=' in log and "MB-2026-E_0s.fit" in log
    assert '"-dark=%s"' % master_dark.replace("\\", "/") in log
    assert "convert bias" not in log and "convert dark" not in log
    assert "stack bias_" not in log and "stack dark_" not in log
    assert fits.getheader(st.runs["L"]["master"])["STACKCNT"] == 4


@posix_only
def test_master_inputs_are_not_modified(tmp_path, cli):
    plan, run, lights = make_plan(tmp_path, bias=0, darks=0, flats=3)
    master_dark = _master(tmp_path / "masterDark.fit", "Master Dark")
    run["darks"] = [master_dark]
    before = digest([master_dark] + lights)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    assert digest([master_dark] + lights) == before
