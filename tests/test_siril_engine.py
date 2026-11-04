"""Siril engine: detection, version, settings, dispatch, FITS master from a Siril run."""
import os
import subprocess
import sys

import pytest

import numpy as np
from astropy.io import fits

from astrostack import stackqa, xisf
from astrostack.app import server
from astrostack.engines import siril
from astrostack.engines.pixinsight import StackError
from astrostack.settings import Settings

HERE = os.path.dirname(os.path.abspath(__file__))
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Testhilfen sind Shell-Skripte")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Flatpak ist Linux-spezifisch")


def make_cli(path, version_line=None):
    """Shell wrapper that starts fake_siril.py with a real command line (or prints a fixed version)."""
    if version_line:
        body = 'echo "%s"\n' % version_line
    else:
        body = 'exec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(HERE, "fake_siril.py"))
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})


@pytest.fixture
def no_siril(monkeypatch):
    """No Siril installation: no PATH hit, no default paths, no Flatpak."""
    monkeypatch.setattr(siril, "SEARCH_PATHS", [])
    monkeypatch.setattr(siril, "FLATPAK_DIRS", ())
    monkeypatch.setattr(siril.shutil, "which", lambda name: None)
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})


@posix_only
def test_explicit_path_reports_version(tmp_path, fresh_cache):
    cli = make_cli(tmp_path / "siril-cli")
    c = siril.check(cli)
    assert c["ok"] and c["found"] and c["version"] == "1.4.4", c


@posix_only
def test_path_lookup_finds_siril_cli_in_path(tmp_path, fresh_cache, monkeypatch):
    make_cli(tmp_path / "siril-cli")
    monkeypatch.setenv("PATH", str(tmp_path))
    c = siril.check("")
    assert c["ok"] and c["version"] == "1.4.4", c


@posix_only
def test_too_old_siril_is_rejected(tmp_path, fresh_cache):
    cli = make_cli(tmp_path / "siril-cli", version_line="siril 1.0.6")
    c = siril.check(cli)
    assert not c["ok"] and c["found"] and "zu alt" in c["message"], c


@posix_only
def test_gui_binary_selects_sibling_cli(tmp_path, fresh_cache):
    make_cli(tmp_path / "siril-cli")
    gui = tmp_path / "siril"
    gui.write_text("#!/bin/sh\n")
    gui.chmod(0o755)
    inst = siril.locate(str(gui))
    assert inst.cli == (str(tmp_path / "siril-cli"),) and inst.gui == (str(gui),)
    assert siril.check(str(gui))["ok"]


@linux_only
def test_flatpak_install_detected_and_selectable(tmp_path, fresh_cache, monkeypatch):
    (tmp_path / "app" / siril.FLATPAK_ID).mkdir(parents=True)
    monkeypatch.setattr(siril, "SEARCH_PATHS", [])
    monkeypatch.setattr(siril, "FLATPAK_DIRS", (str(tmp_path),))
    monkeypatch.setattr(siril.shutil, "which", lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    monkeypatch.setattr(siril.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, stdout=b"siril 1.4.4\n", stderr=b""))
    inst = siril.locate("")
    assert inst.cli == ("flatpak", "run", "--command=siril-cli", "org.siril.Siril")
    assert inst.gui == ("flatpak", "run", "org.siril.Siril")
    assert inst.label == "flatpak:org.siril.Siril"
    for value in ("", "flatpak:org.siril.Siril"):
        c = siril.check(value)
        assert c["ok"] and c["cmd"] == "flatpak:org.siril.Siril" and c["version"] == "1.4.4", c


def test_not_found_is_clear_and_never_falls_back_to_pixinsight(no_siril, tmp_path):
    c = siril.check("")
    assert not c["ok"] and not c["found"] and "Siril nicht gefunden" in c["message"], c
    assert "Pfad nicht gefunden" in siril.check(str(tmp_path / "gibt-es-nicht"))["message"]
    s = Settings(engine="siril", siril="", workdir=str(tmp_path))
    with pytest.raises(StackError) as exc:
        siril.run_plan({}, s)
    assert "Siril nicht gefunden" in str(exc.value) and "PixInsight" not in str(exc.value)


@posix_only
def test_dispatch_routes_to_siril_and_rejects_unknown_engine(tmp_path, fresh_cache):
    from astrostack import engines
    s = Settings(engine="siril", siril=make_cli(tmp_path / "siril-cli"), workdir=str(tmp_path))
    plan = dict(target="T", workdir=str(tmp_path / "w"), runs=[], options={})
    st = engines.run_plan(plan, s, log=lambda *a: None)              # Siril path reached, plan without runs
    assert st.state == "failed" and "Kein Lauf aktiviert" in st.error
    with pytest.raises(StackError, match="Unbekannte Stacking-Engine"):
        engines.run_plan({}, Settings(engine="foo", workdir=str(tmp_path)))


def test_settings_defaults_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    d = Settings()
    assert d.engine == "pixinsight" and d.siril == ""
    Settings(engine="siril", siril="flatpak:org.siril.Siril", workdir=str(tmp_path)).save()
    loaded = Settings.load()
    assert loaded.engine == "siril" and loaded.siril == "flatpak:org.siril.Siril"


def test_save_settings_validates_engine_and_state_reports_siril(tmp_path, monkeypatch, no_siril):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    Settings(workdir=str(tmp_path)).save()
    with pytest.raises(ValueError):
        server.save_settings({"engine": "foo"})
    st = server.save_settings({"engine": "siril", "siril": ""})
    assert st["settings"]["engine"] == "siril"
    assert st["siril_ok"] is False and "Siril nicht gefunden" in st["siril_message"]
    assert st["pixinsight_ok"] in (True, False)       # still present, just not authoritative


def test_siril_fits_master_is_processed(tmp_path):
    """Siril result: float32 FITS, RGB as NAXIS3=3 → color-fringe/metrics path, keywords kept."""
    master = str(tmp_path / "result.fit")
    rng = np.random.default_rng(0)
    data = np.clip(0.05 + 0.01 * rng.standard_normal((3, 120, 160)), 0, 1).astype(np.float32)
    hdr = fits.Header()
    hdr["OBJECT"] = "fake"
    hdr["STACKCNT"] = 4
    fits.PrimaryHDU(data=data, header=hdr).writeto(master)
    assert stackqa.load_master(master).shape == (3, 120, 160)
    assert stackqa.keywords_of(master)["OBJECT"] == "fake"
    run = dict(name="RGB", mode="debayer", hours=2.0, outputs=["RGB"], channel_map={})
    out = stackqa.process_master(master, run, "vdB 15", str(tmp_path / "final"), str(tmp_path / "pv"),
                                 "2026-11-03", log=lambda *a: None)
    assert os.path.basename(out[0]["file"]) == "vdB15_RGB_2026-11-03_2.0h.xisf"
    assert xisf.read_image(out[0]["file"]).shape == (3, 120, 160)
    assert xisf.read_header(out[0]["file"])["OBJECT"] == "fake"
