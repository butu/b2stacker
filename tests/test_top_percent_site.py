"""Regler „Nur die besten Frames“ je Lauf und Ersatz-Standort aus den Einstellungen."""
import json
import os

import pytest

from astrostack import fitsmeta, project
from astrostack.app import server


def _plan(tmp_path, monkeypatch, n=10):
    lights = [str(tmp_path / ("L%02d.fits" % i)) for i in range(n)]
    scores = {p: (float(i), 300.0) for i, p in enumerate(lights)}           # L09 ist der beste
    monkeypatch.setattr(project, "_frame_scores", lambda pdir: scores)
    return lights, dict(options={}, runs=[dict(name="RGB", lights=list(lights), lightCount=n, hours=n * 300 / 3600),
                                          dict(name="Ha", lights=list(lights), lightCount=n, hours=n * 300 / 3600)])


def test_top_percent_per_run(tmp_path, monkeypatch):
    lights, plan = _plan(tmp_path, monkeypatch)
    plan["runs"][0]["top_percent"] = 50
    lines = project.apply_top_percent(str(tmp_path), plan)
    rgb, ha = plan["runs"]
    assert rgb["lights"] == lights[5:] and rgb["lightCount"] == 5 and rgb["hours"] == round(5 * 300 / 3600, 2)
    assert ha["lightCount"] == 10, "anderer Lauf unverändert"
    assert len(lines) == 1 and "5 von 10" in lines[0]


def test_old_global_value_still_applies(tmp_path, monkeypatch):
    _l, plan = _plan(tmp_path, monkeypatch)
    plan["options"]["top_percent"] = 30
    plan["runs"][1]["top_percent"] = 100                                      # Lauf-Wert schlägt den alten globalen
    project.apply_top_percent(str(tmp_path), plan)
    assert [r["lightCount"] for r in plan["runs"]] == [3, 10]


def test_ranking_for_live_hours(tmp_path, monkeypatch):
    lights, plan = _plan(tmp_path, monkeypatch)
    project.with_ranking(str(tmp_path), plan)
    assert plan["runs"][0]["rank_exp"] == [300.0] * 10
    assert project.keep_count(10, 50) == 5 and project.keep_count(2, 10) == 2 and project.keep_count(40, 5) == 3


def test_fallback_site(tmp_path, monkeypatch):
    sp = tmp_path / "s.json"
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(sp))
    monkeypatch.setattr(server, "settings", lambda: __import__("astrostack.settings", fromlist=["Settings"]).Settings.load())
    monkeypatch.setattr(server, "state", lambda: {})
    monkeypatch.setattr(server.logbook, "setup", lambda *a: None)
    monkeypatch.setattr(server.logbook, "event", lambda *a, **k: None)
    h = {"IMAGETYP": "LIGHT", "DATE-OBS": "2026-01-10T20:00:00"}
    assert fitsmeta.from_header(h, "x.fits").site_lat is None
    server.save_settings({"workdir": str(tmp_path / "w"), "site_lat": "48,86", "site_lon": "15.01", "site_elev": ""})
    assert json.load(open(sp))["site_lat"] == 48.86
    m = fitsmeta.from_header(h, "x.fits")
    assert (m.site_lat, m.site_lon, m.site_elev, m.site_fallback) == (48.86, 15.01, 0.0, True)
    m2 = fitsmeta.from_header(dict(h, SITELAT=30.0, SITELONG=-110.0), "x.fits")
    assert (m2.site_lat, m2.site_fallback) == (30.0, False), "Header geht vor"
    with pytest.raises(ValueError, match="Breitengrad"):
        server.save_settings({"site_lat": "95"})
    with pytest.raises(ValueError, match="beide"):
        server.save_settings({"site_lat": "", "site_lon": "15"})
    os.utime(sp, None)
