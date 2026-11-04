"""Siril: reference image (file: / master:) – append as frame, exclude, match geometry."""
import os

import numpy as np
import pytest
from astropy.io import fits

from astrostack import engines
from astrostack.engines import siril
from astrostack.engines.pixinsight import StackError

from .test_siril_run import H, W, cli, make_plan, posix_only, settings  # noqa: F401 – Fixture cli


def _write(path, data):
    fits.PrimaryHDU(np.asarray(data, np.float32)).writeto(str(path), overwrite=True)
    return str(path)


def test_rgb_master_becomes_luminance_for_mono_run(tmp_path):
    rgb = _write(tmp_path / "rgb.fit", np.stack([np.full((H, W), v) for v in (0.1, 0.2, 0.3)]))
    d = siril._reference_files(rgb, osc=False, shape=(H, W), folder=str(tmp_path / "ref"))
    got = fits.getdata(os.path.join(d, "ref_00001.fit"))
    assert got.ndim == 2 and np.allclose(got, 0.2)
    assert os.path.exists(os.path.join(d, "ref_00002.fit"))


def test_mono_master_is_three_channels_for_rgb_run(tmp_path):
    mono = _write(tmp_path / "mono.fit", np.full((H, W), 0.5))
    d = siril._reference_files(mono, osc=True, shape=(H, W), folder=str(tmp_path / "ref"))
    got = fits.getdata(os.path.join(d, "ref_00001.fit"))
    assert got.shape == (3, H, W) and np.allclose(got, 0.5)


def test_reference_size_mismatch_is_clear(tmp_path):
    mono = _write(tmp_path / "mono.fit", np.zeros((30, 40)))
    with pytest.raises(StackError, match="andere Bildgröße"):
        siril._reference_files(mono, osc=False, shape=(H, W), folder=str(tmp_path / "ref"))


@posix_only
def test_file_reference_is_appended_and_excluded_from_stack(tmp_path, cli, monkeypatch):
    plan, run, _ = make_plan(tmp_path)
    run["reference"] = "file:" + _write(tmp_path / "ref.fit", np.full((H, W), 0.1))
    logs = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=logs.append, open_results=False)
    assert st.state == "done", st.error
    assert fits.getheader(st.runs["L"]["master"])["STACKCNT"] == 4          # only the 4 lights
    ssf = open(os.path.join(os.path.dirname(st.runs["L"]["master"]), "siril.ssf"), encoding="utf-8").read()
    assert "setref lights_ 5" in ssf and "unselect r_lights_ 5 6" in ssf and "-filter-included" in ssf
    assert "-2pass" not in ssf
    assert "Sensor-Prüfung nicht prüfbar (externe Referenz)" in "\n".join(logs)


@posix_only
def test_size_mismatch_stops_the_run(tmp_path, cli):
    plan, run, _ = make_plan(tmp_path)
    run["reference"] = "file:" + _write(tmp_path / "ref.fit", np.zeros((10, 10)))
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "failed" and "andere Bildgröße" in st.error


@posix_only
def test_master_reference_uses_previous_run_output(tmp_path, cli):
    plan, run, _ = make_plan(tmp_path)
    second = dict(run, name="R", reference="master:L")
    plan["runs"] = [run, second]
    logs = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=logs.append, open_results=False)
    assert st.state == "done", st.error
    assert st.runs["R"]["reference"] == "master:L"
    assert any("Referenz: master.fit" in ln for ln in logs)
