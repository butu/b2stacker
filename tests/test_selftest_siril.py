"""Selbsttest mit Siril-Engine: Siril-Schritt, Stacking über engines.run_plan, Bericht mit engine + Siril-Version."""
import os
import sys

import pytest

from astrostack import engines, selftest
from astrostack.engines import siril
from astrostack.settings import Settings

HERE = os.path.dirname(os.path.abspath(__file__))
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Wrapper ist ein Shell-Skript")


def _siril_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    cli = tmp_path / "siril-cli"
    cli.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(HERE, "fake_siril.py")))
    cli.chmod(0o755)
    folder = tmp_path / "nas"
    folder.mkdir()
    s = Settings(engine="siril", siril=str(cli), workdir=str(tmp_path / "work"), analysis_workers=1,
                 keep_awake=False, open_results=False, min_free_ram_gb=0.1)
    return s, str(folder)


@posix_only
def test_selftest_siril_dispatches_to_siril_and_reports_version(tmp_path, monkeypatch):
    s, folder = _siril_settings(tmp_path, monkeypatch)
    res = selftest.run(s, folder, log=lambda *a: None)

    names = {st["name"]: st for st in res["steps"]}
    assert names["Siril gefunden"]["ok"] and "1.4.4" in names["Siril gefunden"]["detail"], res["steps"]
    assert "PixInsight gefunden" not in names and "Stacking mit PixInsight" not in names
    assert res["engine"] == "siril" and res["environment"]["siril_version"] == "1.4.4"
    assert any(st["name"] == "Analyse" and "2 Flat-Gruppen" in st["detail"] for st in res["steps"]), res["steps"]
    assert "Stacking mit Siril" in names                       # lief über engines.run_plan → Siril
    assert res["stack"]["state"] in ("done", "failed")
    assert "PixInsight" not in (res["stack"]["error"] or "")
    assert os.path.exists(res["report"]) and "," in os.listdir(folder)[0]
    assert "wbpp_causes" not in res and "cosmetic" not in res
    assert engines.LABELS["siril"] == "Siril"


@posix_only
def test_selftest_siril_full_stack_done_with_fake_cli(tmp_path, monkeypatch):
    s, folder = _siril_settings(tmp_path, monkeypatch)
    res = selftest.run(s, folder, log=lambda *a: None)
    assert res["stack"]["state"] == "done", res["stack"]
    assert res["ok"] is True


def test_selftest_siril_missing_cli_reports_siril_step(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    monkeypatch.setattr(siril.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(siril, "SEARCH_PATHS", [])
    monkeypatch.setattr(siril, "_flatpak_install", lambda: None)
    s = Settings(engine="siril", siril=str(tmp_path / "gibt-es-nicht"), workdir=str(tmp_path / "work"),
                 analysis_workers=1, keep_awake=False, open_results=False, min_free_ram_gb=0.1)
    folder = tmp_path / "nas"
    folder.mkdir()
    res = selftest.run(s, str(folder), log=lambda *a: None)
    step = {st["name"]: st for st in res["steps"]}["Siril gefunden"]
    assert step["ok"] is False and "nicht gefunden" in step["detail"]
    assert res["environment"]["siril_ok"] is False
    assert res["ok"] is False
