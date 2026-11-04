"""Siril OSC/Bayer: force pattern if header is missing or conflicting; XISF lights; dualband on color camera."""
import os
import sys

import numpy as np
import pytest
from astropy.io import fits

from astrostack import engines, xisf
from astrostack.engines import siril

from .test_siril_run import cli, frames, make_plan, settings  # noqa: F401 – Fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Wrapper ist ein Shell-Skript")
BASE = dict(outdir="/o", lights="/o/raw/light", bias=None, dark=None, flat=None, cosmetic=False, osc=True)


def _log(msgs):
    return lambda *a: msgs.append(" ".join(str(x) for x in a))


def test_script_sets_pattern_only_when_given():
    forced = siril.build_script(dict(BASE, bayer="GRBG"))
    assert "set debayer.use_bayer_header=false" in forced and "set debayer.pattern=3" in forced
    assert forced.index("set debayer.pattern") < forced.index("convert light")
    plain = siril.build_script(dict(BASE, bayer=None))
    assert "set debayer" not in plain
    mono = siril.build_script(dict(BASE, osc=False, bayer="GRBG"))
    assert "set debayer" not in mono                       # mono has no pattern


def test_bayer_index_matches_siril_numbering():
    # Siril 1.4: 0=RGGB, 1=BGGR, 2=GBRG, 3=GRBG
    assert siril.BAYER_INDEX == {"RGGB": 0, "BGGR": 1, "GBRG": 2, "GRBG": 3}


def test_header_consistent_means_no_override(tmp_path):
    paths = frames(tmp_path / "l", 3, "light", osc=True)
    msgs = []
    assert siril.bayer_override(paths, _log(msgs)) is None and msgs == []


def test_missing_header_forces_default_with_warning(tmp_path):
    paths = frames(tmp_path / "l", 2, "light", osc=False)     # without BAYERPAT
    msgs = []
    assert siril.bayer_override(paths, _log(msgs)) == "RGGB"
    assert any("Bayer-Muster fehlt" in m for m in msgs)


def test_mixed_headers_force_majority_pattern(tmp_path):
    a = frames(tmp_path / "a", 2, "light", osc=True)            # RGGB
    b = frames(tmp_path / "b", 1, "light", osc=True)
    with fits.open(b[0], mode="update") as h:
        h[0].header["BAYERPAT"] = "GRBG"
    msgs = []
    assert siril.bayer_override(a + b, _log(msgs)) == "RGGB"
    assert any("uneinheitlich" in m for m in msgs)


def _xisf_lights(folder, osc, n=4):
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    out = []
    for i in range(n):
        p = folder / ("light_%03d.xisf" % i)
        data = (0.1 + 0.01 * rng.standard_normal((60, 80))).astype(np.float32)
        xisf.write_image(str(p), data, {"IMAGETYP": "Light Frame", "FILTER": "L"}, color=False)
        out.append(str(p))
    return out


@posix_only
def test_xisf_lights_are_converted_and_stacked(tmp_path, cli):
    plan, run, _ = make_plan(tmp_path, osc=False)
    run["lights"] = _xisf_lights(tmp_path / "xl", osc=False)
    run["lightCount"] = len(run["lights"])
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    assert fits.getheader(st.runs["L"]["master"])["STACKCNT"] == 4


@posix_only
def test_dualband_on_color_camera_splits_into_channel_finals(tmp_path, cli):
    plan, run, _ = make_plan(tmp_path, osc=True)
    run.update(mode="extract", outputs=["Ha", "OIII"], channel_map={"Ha": "R", "OIII": "GB"}, name="Ha+OIII")
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    names = sorted(os.path.basename(f) for f in st.finals)
    assert len(st.finals) == 2 and any("Ha" in n for n in names) and any("OIII" in n for n in names)
    for f in st.finals:
        assert xisf.read_image(f).shape[0] == 1
