"""Report snapshot: cached rebuild follows the actual plan without touching originals."""
import json
import os
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from astrostack import discover, fitsmeta, imageio, project
from astrostack.app import server
from astrostack.report import build
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_e2e import star_field


@contextmanager
def _client():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


@pytest.fixture
def analysed_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    root = str(tmp_path / "data")
    for i in range(4):
        h = dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i),
                                "DATE-LOC": "2026-10-12T22:%02d:00" % (10 * i)})
        make_fits(f"{root}/vdB 131/HaO3/2.6{i}_vdB 131_600.00sec_gain100_000{i}.fits", h, data=star_field(i))
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    project.run_report([root], None, s, log=lambda *a: None)
    pdir = os.path.join(s.workdir, "projects", "vdB 131")
    return pdir


def test_snapshot_written_and_cached_html_follows_plan(analysed_project, monkeypatch):
    pdir = analysed_project
    snap = os.path.join(pdir, "analysis", "report-data.json")
    assert os.path.isfile(snap)
    generated = json.load(open(snap, encoding="utf-8"))["data"]["meta"]["generated"]
    plan_path = os.path.join(pdir, "plan.json")
    plan = json.load(open(plan_path, encoding="utf-8"))
    run = plan["runs"][0]
    removed = run["lights"].pop()
    run["lightCount"] = len(run["lights"])
    run["hours"] = round(sum(1 for _ in run["lights"]) * 600 / 3600, 2)
    json.dump(plan, open(plan_path, "w", encoding="utf-8"))
    selected = {p for r in plan["runs"] for p in r["lights"]}

    # Cached rebuild must not read originals, headers, or render thumbnails.
    monkeypatch.setattr(build.fm, "read_meta", lambda *a, **k: (_ for _ in ()).throw(AssertionError("original")))
    monkeypatch.setattr(build, "thumb_b64", lambda *a, **k: (_ for _ in ()).throw(AssertionError("render")))
    monkeypatch.setattr(build, "rig_diagnostics", lambda *a, **k: (_ for _ in ()).throw(AssertionError("diag")))
    monkeypatch.setattr(build, "sky_tracks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sky")))

    data = build.cached_data(pdir)
    report_selected = {f["path"] for f in data["frames"] if f["status"] not in ("rejected", "error")}
    assert report_selected == selected
    assert next(f for f in data["frames"] if f["path"] == removed)["status"] == "rejected"
    assert sum(k["n_used"] for k in data["kpi"]) == len(selected)
    assert sum(k["used_h"] for k in data["kpi"]) == pytest.approx(sum(r["hours"] for r in plan["runs"]), abs=0.02)
    html = build.cached_html(pdir)
    assert isinstance(html, str) and "/*__DATA__*/null" not in html
    rendered = build.embedded_report_data(html)
    assert rendered["meta"]["generated"] == generated, "GET darf die Analysezeit nicht überschreiben"


def test_disabled_run_frames_are_not_reported_as_used(analysed_project):
    pdir = analysed_project
    plan_path = os.path.join(pdir, "plan.json")
    plan = json.load(open(plan_path, encoding="utf-8"))
    plan["runs"][0]["enabled"] = False
    json.dump(plan, open(plan_path, "w", encoding="utf-8"))
    data = build.cached_data(pdir)
    assert {f["path"] for f in data["frames"] if f["status"] not in ("rejected", "error")} == set()
    assert sum(k["n_used"] for k in data["kpi"]) == 0


def test_missing_report_snapshot_is_a_clear_migration_error(tmp_path):
    pdir = tmp_path / "projects" / "Alt"
    pdir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="Reportdaten fehlen"):
        build.cached_html(str(pdir))


def test_legacy_report_bootstraps_from_embedded_data_without_original_io(analysed_project, monkeypatch, tmp_path):
    pdir = analysed_project
    snapshot = os.path.join(pdir, "analysis", "report-data.json")
    os.remove(snapshot)
    plan_path = os.path.join(pdir, "plan.json")
    plan = json.load(open(plan_path, encoding="utf-8"))
    run = plan["runs"][0]
    removed = run["lights"].pop()
    run["lightCount"] = len(run["lights"])
    run["hours"] = round(len(run["lights"]) * 600 / 3600, 2)
    json.dump(plan, open(plan_path, "w", encoding="utf-8"))

    def forbidden(*_a, **_k):
        raise AssertionError("Legacy-Bootstrap darf keine Quellen, Header oder Pixel lesen")

    monkeypatch.setattr(discover, "scan", forbidden)
    monkeypatch.setattr(fitsmeta, "read_meta", forbidden)
    monkeypatch.setattr(imageio, "load", forbidden)
    monkeypatch.setattr(build, "thumb_b64", forbidden)
    data_root = os.path.abspath(str(tmp_path / "data"))
    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        if os.path.abspath(os.fspath(path)).startswith(data_root):
            raise AssertionError("Legacy-Bootstrap darf Originale nicht staten")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    workdir = os.path.dirname(os.path.dirname(pdir))
    monkeypatch.setattr(server, "settings", lambda: Settings(workdir=workdir))
    with _client() as url:
        with urllib.request.urlopen(url + "/p/" + urllib.parse.quote("vdB 131") + "/report.html", timeout=5) as r:
            assert r.status == 200
            html = r.read().decode("utf-8")
    data = build.embedded_report_data(html)
    assert data is not None
    assert {f["path"] for f in data["frames"] if f["status"] not in ("rejected", "error")} == {p for r in plan["runs"] for p in r["lights"]}
    assert next(f for f in data["frames"] if f["path"] == removed)["status"] == "rejected"
    assert os.path.isfile(snapshot), "Bootstrap-Snapshot wird lokal gespeichert"


def test_unknown_legacy_report_stays_readable_as_archive(tmp_path):
    pdir = tmp_path / "projects" / "Archiv"
    (pdir / "analysis").mkdir(parents=True)
    (pdir / "report.html").write_text("<html><body><h1>Alter Bericht</h1></body></html>", encoding="utf-8")
    html = build.cached_html(str(pdir))
    assert "Alter Bericht" in html and "Archivbericht" in html and "schreibgeschützt" in html
    assert not os.path.exists(pdir / "analysis" / "report-data.json")
