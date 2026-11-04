"""Siril run against tests/fake_siril.py: script, mono/OSC, errors, preflight, originals untouched."""
import hashlib
import os
import sys

import numpy as np
import pytest
from astropy.io import fits

from astrostack import engines, xisf
from astrostack.engines import siril
from astrostack.engines.pixinsight import StackError
from astrostack.settings import Settings

HERE = os.path.dirname(os.path.abspath(__file__))
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Wrapper ist ein Shell-Skript")
H, W = 60, 80


def make_cli(path):
    path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(HERE, "fake_siril.py")))
    path.chmod(0o755)
    return str(path)


def frames(folder, n, kind, osc=False, seed=0):
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        p = folder / ("%s_%03d.fit" % (kind, i))
        data = (0.1 + 0.01 * rng.standard_normal((H, W))).astype(np.float32)
        h = fits.Header()
        h["IMAGETYP"] = {"light": "Light Frame", "bias": "Bias Frame", "dark": "Dark Frame", "flat": "Flat Frame"}[kind]
        h["FILTER"] = "L"
        if osc:
            h["BAYERPAT"] = "RGGB"
        fits.PrimaryHDU(data=data, header=h).writeto(str(p), overwrite=True)
        out.append(str(p))
    return out


def digest(paths):
    return {p: hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    return make_cli(tmp_path / "siril-cli")


def make_plan(tmp_path, osc=False, bias=3, flats=3, darks=3):
    data = tmp_path / "data"
    lights = frames(data / "light", 4, "light", osc=osc, seed=1)
    run = dict(name="RGB" if osc else "L", mode="debayer" if osc else "mono", outputs=["RGB" if osc else "L"],
               channel_map={}, enabled=True, is_osc=osc, lights=lights,
               bias=frames(data / "bias", bias, "bias", seed=2) if bias else [],
               darks=frames(data / "dark", darks, "dark", seed=3) if darks else [],
               flats=frames(data / "flat", flats, "flat", osc=osc, seed=4) if flats else [],
               darkflats=[], reference="auto", hours=0.1, gb=0.01, ram_gb=0.1, lightCount=len(lights),
               star_detection=dict(why="test", params=dict(sensitivity=0.9)))
    work = str(tmp_path / "work")
    plan = dict(target="Test", workdir=work, runs=[run], options=dict(fix_fringes=False), pixscale=1.0)
    return plan, run, lights


def settings(tmp_path, cli, **kw):
    return Settings(engine="siril", siril=cli, workdir=str(tmp_path / "work"), keep_awake=False,
                    open_results=False, cleanup_intermediate=True, **kw)


def test_script_for_mono_and_osc():
    base = dict(outdir="/o", lights="/o/raw/light", bias=dict(raw="/o/raw/bias", prefix="bias"),
                dark=dict(master="/m/dark.fit"), flat=dict(raw="/o/raw/flat", prefix="flat"), cosmetic=True)
    mono = siril.build_script(dict(base, osc=False, sigma=0.7))
    assert mono.startswith("requires 1.4.0\nsetext fit\n")
    assert "convert light \"-out=/o/seq_light\"" in mono
    assert "calibrate light_ " in mono and '"-cc=dark 3 3"' not in mono and "-cc=dark 3.0 3.0" in mono
    assert "-cfa" not in mono and "-debayer" not in mono
    assert "setfindstar -sigma=0.70" in mono and "stack r_pp_light_" in mono and "-32b -out=result" in mono
    osc = siril.build_script(dict(base, osc=True))
    assert "-cfa -equalize_cfa -debayer" in osc and "-rgb_equal" in osc


def test_quote_in_path_is_rejected():
    with pytest.raises(StackError):
        siril.build_script(dict(outdir='/x"y', lights="/l", osc=False))


@posix_only
def test_mono_run_produces_final_and_keeps_originals(tmp_path, cli):
    plan, run, lights = make_plan(tmp_path)
    originals = digest(lights + run["bias"] + run["darks"] + run["flats"])
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    assert len(st.finals) == 1 and os.path.exists(st.finals[0])
    assert xisf.read_image(st.finals[0]).shape[0] == 1
    assert fits.getheader(st.runs["L"]["master"])["STACKCNT"] == 4          # alle gewählten Lights gestapelt
    assert digest(lights + run["bias"] + run["darks"] + run["flats"]) == originals


@posix_only
def test_cleanup_removes_cosmetic_intermediates(tmp_path, cli):
    plan, _, _ = make_plan(tmp_path, darks=0)                 # ohne Darks → eigene Hotpixel-Vorstufe läuft
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    leftovers = [root for root, dirs, _ in os.walk(str(tmp_path / "work")) if os.path.basename(root) == "_cosmetic"]
    assert leftovers == []


@posix_only
def test_osc_run_produces_rgb_final(tmp_path, cli):
    plan, run, _ = make_plan(tmp_path, osc=True)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    assert xisf.read_image(st.finals[0]).shape[0] == 3


@posix_only
def test_registration_failure_is_diagnosed(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("FAKESIRIL_FAIL_REGISTER", "1")
    plan, _, _ = make_plan(tmp_path)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "failed"
    assert "Sternpaare" in st.error and st.causes


@posix_only
def test_missing_output_is_failure_not_success(tmp_path, cli, monkeypatch):
    monkeypatch.setenv("FAKESIRIL_NO_OUTPUT", "1")
    plan, _, _ = make_plan(tmp_path)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "failed" and "keinen Stack" in st.error


@posix_only
def test_two_raw_biases_stop_before_siril(tmp_path, cli):
    plan, _, _ = make_plan(tmp_path, bias=2)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "failed" and "Nur 2 Bias-Bild" in st.error


@posix_only
def test_missing_light_is_reported(tmp_path, cli):
    plan, run, lights = make_plan(tmp_path)
    os.remove(lights[0])
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "failed" and "Lights fehlen" in st.error


def test_siril_run_without_install_is_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "SEARCH_PATHS", [])
    monkeypatch.setattr(siril, "FLATPAK_DIRS", ())
    monkeypatch.setattr(siril.shutil, "which", lambda name: None)
    plan, _, _ = make_plan(tmp_path)
    with pytest.raises(StackError, match="nicht gefunden"):
        engines.run_plan(plan, settings(tmp_path, "/nicht/da/siril-cli"), log=lambda *a: None)
