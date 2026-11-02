"""Frameauswahl pro Projekt: gemeinsame Kalibrierdateien, Plan-Konsistenz, fehlende Messungen."""
import json
import os
from types import SimpleNamespace as NS

import pytest

from astrostack import discover, plan, project
from astrostack.settings import Settings


def _pdir(base, name):
    pdir = os.path.join(base, "projects", name)
    os.makedirs(os.path.join(pdir, "analysis"), exist_ok=True)
    json.dump({"roots": [], "target": name}, open(os.path.join(pdir, "sources.json"), "w"))
    return pdir


def _shared_flat(folder, names=("F1.fits", "F2.fits")):
    os.makedirs(folder, exist_ok=True)
    cs = discover.CalibSet(type="flats", filter="Ha", settings=(), folder=folder, exposure=2.0, session="s1")
    cs.frames = []
    for n in names:
        p = os.path.join(folder, n)
        open(p, "wb").write(b"SIMPLE")
        cs.frames.append(NS(path=p, excluded=False, filter="Ha", night=None))
    return cs


def _plan_base(pdir, flats):
    plan = {"target": "M31", "total_frames": 2, "options": {},
            "runs": [{"name": "Ha", "filter": "Ha", "enabled": True, "lightCount": 2, "flats": list(flats),
                      "flat_set": os.path.dirname(flats[0]) if flats else None}]}
    json.dump(plan, open(os.path.join(pdir, "plan_base.json"), "w"))


def test_two_projects_share_flat_each_keeps_own_selection(tmp_path):
    base = str(tmp_path / "w")
    cs = _shared_flat(str(tmp_path / "flats"))
    inv = NS(lights=[], calib=[cs], unknown=[], errors=[])
    pa, pb = _pdir(base, "A"), _pdir(base, "B")
    project.set_override(pa, cs.frames[0].path, "out")          # A wählt F1 ab
    view_a = project.calib_view(inv, project._read(os.path.join(pa, "overrides.json"), {}))
    view_b = project.calib_view(inv, project._read(os.path.join(pb, "overrides.json"), {}))
    assert [f.path for f in view_a.calib[0].frames] == [cs.frames[1].path]
    assert [f.path for f in view_b.calib[0].frames] == [f.path for f in cs.frames], "B bleibt unverändert"
    assert len(inv.calib[0].frames) == 2, "gemeinsames Inventar wird nicht verändert"
    assert view_a.calib[0].id == view_b.calib[0].id == cs.id, "Set-IDs bleiben gleich"
    _plan_base(pb, [f.path for f in cs.frames])
    assert project.apply_edits(pb)["runs"][0]["flats"] == [f.path for f in cs.frames]


def _light_group(paths):
    frames = [NS(path=p, exposure=300.0, alt=60.0, night="2026-10-12", excluded=False, filter="RGB") for p in paths]
    return NS(target="M31", filter="RGB", filter_kind="broadband", is_osc=True,
              settings=("cam", 100, 50, "High", 1, 6000, 4000), frames=frames)


def test_unmeasured_light_is_fail_closed_in_plan(tmp_path):
    from astrostack import plan as plan_mod
    g = _light_group(["/d/L1.fits", "/d/L2.fits"])
    rows = [dict(path="/d/L1.fits", status="ok", pixscale=1.2, fwhm_as=2.5)]
    p = plan_mod.build_plan("M31", [g], rows, [], [], str(tmp_path), unmeasured={"/d/L2.fits"})
    assert p["runs"][0]["lights"] == ["/d/L1.fits"], "fehlgeschlagene Messung darf nicht still gestackt werden"
    assert p["runs"][0]["lightCount"] == 1


def test_plan_without_measurement_info_keeps_legacy_behaviour(tmp_path):
    from astrostack import plan as plan_mod
    g = _light_group(["/d/L1.fits", "/d/L2.fits"])
    p = plan_mod.build_plan("M31", [g], [], [], [], str(tmp_path))
    assert p["runs"][0]["lights"] == ["/d/L1.fits", "/d/L2.fits"]


def test_override_in_cannot_heal_measure_error(tmp_path):
    pdir = _pdir(str(tmp_path / "w"), "M31")
    project.write_frames_index(pdir, [_light_group(["/d/L9.fits"])], [])
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write("path,nstars\n")
    project.set_override(pdir, "/d/L9.fits", "in")
    f = project.frames(pdir)[0]
    assert f["status"] == "error" and f["selected"] is False and f["selection_source"] == "override"


def test_legacy_project_without_frames_json_lists_lights_from_metrics(tmp_path):
    pdir = _pdir(str(tmp_path / "w"), "Alt")
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write(
        "path,night,excluded,filter,nstars\n/d/L1.fits,2026-10-12,0,Ha,500\n/d/L2.fits,2026-10-12,1,Ha,\n")
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert set(fr) == {"/d/L1.fits", "/d/L2.fits"}, "alte Projekte dürfen in der Ansicht nicht leer sein"
    assert fr["/d/L1.fits"]["type"] == "light" and fr["/d/L1.fits"]["selected"] is True
    assert fr["/d/L2.fits"]["selected"] is False


def test_corrupt_header_is_indexed_with_full_path_and_reason(tmp_path):
    from astrostack import discover as disc
    base = tmp_path / "data" / "Ha"
    base.mkdir(parents=True)
    bad = base / "L_bad.fits"
    bad.write_bytes(b"this is not a fits header" * 10)
    inv = disc.scan([str(tmp_path / "data")], disc.ScanOptions())
    assert any(p == str(bad) and reason for p, reason in inv.errors), "Header-Fehler muss vollen Pfad + Grund liefern"
    pdir = _pdir(str(tmp_path / "w"), "Hdr")
    project.write_frames_index(pdir, [], [], errors=[e for e in inv.errors if e[0] == str(bad)])
    f = next(x for x in project.frames(pdir) if x["path"] == str(bad))
    assert f["type"] == "unknown" and f["status"] == "error" and f["selected"] is False
    assert "nicht lesbar" in f["measure_error"]


def test_detail_analysis_required_mirrors_plan_stale(tmp_path):
    pdir = _pdir(str(tmp_path / "w"), "Stale")
    assert project.detail_extras(pdir)["analysis_required"] is False
    project.set_override(pdir, "/d/x.fits", "out")
    d = project.detail_extras(pdir)
    assert d["analysis_required"] is True and d["plan_stale"] is True


def test_empty_roots_can_be_saved_and_block_stacking(tmp_path):
    pdir = _pdir(str(tmp_path / "w"), "Leer")
    json.dump({"roots": [str(tmp_path / "a")], "target": "Leer"}, open(os.path.join(pdir, "sources.json"), "w"))
    project.set_sources(pdir, [])
    assert project.sources(pdir)["roots"] == [] and project.sources(pdir)["analysis_required"] is True
    try:
        project.assert_stack_ready(pdir)
        raise AssertionError("Stacking ohne Quellordner muss gesperrt sein")
    except ValueError as e:
        assert "Quellordner" in str(e) or "Analyse" in str(e)


def test_mutations_are_refused_while_a_job_runs(monkeypatch):
    from astrostack.app import server

    class Busy:
        def active(self):
            return object()
    monkeypatch.setattr(server, "JOBS", Busy())
    for action in ("override", "sources", "delete", "stack-meta", "stack-delete", "asset-link", "stack", "plan"):
        try:
            server.check_idle(action)
            raise AssertionError(action + " muss während eines Auftrags gesperrt sein")
        except RuntimeError:
            pass
    server.check_idle("frames")                     # Lesen bleibt frei


def test_truncated_pixels_plan_and_frames_agree(tmp_path):
    from astrostack import analyze
    from astrostack.report import assess as A
    from tests.conftest import NINA_LIGHT, make_fits
    data = tmp_path / "data"
    good = make_fits(str(data / "L_good.fits"), NINA_LIGHT, shape=(64, 64))
    bad = make_fits(str(data / "L_trunc.fits"), NINA_LIGHT, shape=(64, 64))
    with open(bad, "r+b") as fh:                       # gültiger Header, Pixeldaten abgeschnitten
        fh.truncate(os.path.getsize(bad) // 2)
    inv = discover.scan([str(data)], discover.ScanOptions())
    groups = inv.lights
    frames_all = [f for g in groups for f in g.frames]
    pdir = _pdir(str(tmp_path / "w"), "vdB 131")
    analyze.analyze_group(frames_all, os.path.join(pdir, "analysis"), workers=1)
    errs = json.load(open(os.path.join(pdir, "analysis", "measure_errors.json")))
    assert any(e["path"] == bad for e in errs), "defekte Pixel müssen mit vollem Pfad gemeldet werden"
    rows = A.load_rows(os.path.join(pdir, "analysis", "metrics.csv"))
    _filters, ntab = A.assess(rows)
    assessment = {r["path"]: dict(auto=r["auto_status"], status=r["status"], flags=r["flags"]) for r in rows}
    json.dump(assessment, open(os.path.join(pdir, "analysis", "assessment.json"), "w"))
    project.write_frames_index(pdir, groups, [])
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert fr[bad]["status"] == "error" and fr[bad]["selected"] is False and fr[bad]["measure_error"]
    unmeasured = {f.path for f in frames_all} - {r["path"] for r in rows}
    p = plan.build_plan("vdB 131", groups, rows, ntab, [], str(tmp_path / "w"), unmeasured=unmeasured)
    stacked = {x for run in p["runs"] for x in run["lights"]}
    selected = {x for x, f in fr.items() if f["type"] == "light" and f["selected"]}
    assert bad not in stacked and good in stacked
    assert stacked == selected, "Plan und Frameansicht müssen dieselben Lights wählen"


def test_reanalyze_fills_existing_planning_project_with_other_object_name(tmp_path):
    from tests.conftest import NINA_LIGHT, make_fits
    data = tmp_path / "lights"
    for i in range(2):
        make_fits(str(data / "night" / ("light_%d.fits" % i)), dict(NINA_LIGHT, OBJECT="Linux smoke"), shape=(64, 64))
    s = Settings(workdir=str(tmp_path / "w"))
    project.create_project(s, "API smoke", [])
    pdir = project.project_dir(s, "API smoke")
    project.set_sources(pdir, [str(data)])
    logs = []
    res = project.run_report([str(data)], "API smoke", s, log=logs.append, into="API smoke")
    assert not any("Ziel nicht gefunden" in x for x in logs), logs
    assert [p["name"] for p in project.list_projects(s)] == ["API smoke"], "kein zweites Projekt"
    assert res and res[0]["project"] == "API smoke"
    assert project.sources(pdir)["analysis_required"] is False
    assert project.project_facts(pdir)["hours_total"] > 0


def test_known_frame_is_index_membership_not_full_assessment(tmp_path, monkeypatch):
    from astrostack.app import server
    pdir = _pdir(str(tmp_path / "w"), "Thumb")
    project.write_frames_index(pdir, [_light_group(["/d/L1.fits"])], [])
    monkeypatch.setattr(project, "frames", lambda p: (_ for _ in ()).throw(AssertionError("kein Vollassessment")))
    assert server._known_frame(pdir, "/d/L1.fits") is True
    assert server._known_frame(pdir, "/etc/passwd") is False


def test_frame_thumb_refuses_unknown_and_renders_known_into_analysis(tmp_path, monkeypatch):
    from astrostack.app import server
    from astrostack import previews
    from tests.conftest import NINA_LIGHT, make_fits
    pdir = _pdir(str(tmp_path / "w"), "Thumb2")
    good = make_fits(str(tmp_path / "data" / "L_thumb.fits"), NINA_LIGHT, shape=(64, 64))
    project.write_frames_index(pdir, [_light_group([good])], [])
    with pytest.raises(PermissionError):
        server.frame_thumb(pdir, "/etc/passwd")
    seen = {}
    real = previews.frame_thumbnail

    def spy(path, analysis_dir, width=320):
        seen["args"] = (path, analysis_dir)
        return real(path, analysis_dir, width=width)
    monkeypatch.setattr(previews, "frame_thumbnail", spy)
    out = server.frame_thumb(pdir, good)
    assert seen["args"] == (good, os.path.join(pdir, "analysis"))
    assert open(out, "rb").read(2) == b"\xff\xd8", "Vorschau ist ein JPEG"
    assert os.path.dirname(out).startswith(os.path.join(pdir, "analysis")), "Cache liegt im Projekt, Original unberührt"


def test_frames_payload_carries_thumb_url_for_every_frame(tmp_path, monkeypatch):
    from astrostack.app import server
    pdir = _pdir(str(tmp_path / "w"), "Payload")
    project.write_frames_index(pdir, [_light_group(["/d/L1.fits", "/d/L2.fits"])], [])
    monkeypatch.setattr(server, "settings", lambda: Settings(workdir=str(tmp_path / "w")))
    body = server.project_frames("Payload")
    assert len(body["frames"]) == 2
    assert all(f["thumb_url"].startswith("/api/frame-thumb/Payload?path=") for f in body["frames"])
    assert "summary" in body and "plan_stale" in body


def test_measure_error_kind_is_shown_per_frame(tmp_path):
    pdir = _pdir(str(tmp_path / "w"), "Kinds")
    project.write_frames_index(pdir, [_light_group(["/d/A.fits", "/d/B.fits", "/d/C.fits"])], [])
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write("path,nstars\n")
    # Format von analyze_group: Liste mit vollständigen Pfaden und kind stat|measure|changed
    json.dump([{"path": "/d/A.fits", "kind": "stat", "reason": "Permission denied"},
               {"path": "/d/B.fits", "kind": "measure", "reason": "kein Stern erkannt"},
               {"path": "/d/C.fits", "kind": "changed", "reason": "während der Messung geändert"}],
              open(os.path.join(pdir, "analysis", "measure_errors.json"), "w"))
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert fr["/d/A.fits"]["measure_error"].startswith("Datei nicht lesbar")
    assert fr["/d/B.fits"]["measure_error"].startswith("nicht vermessen")
    assert fr["/d/C.fits"]["measure_error"].startswith("während der Messung geändert")
    assert [fr[p]["measure_kind"] for p in ("/d/A.fits", "/d/B.fits", "/d/C.fits")] == ["stat", "measure", "changed"]
    assert all(fr[p]["status"] == "error" and fr[p]["selected"] is False for p in fr)


def test_user_exclusion_survives_manual_folder_choice(tmp_path):
    base = str(tmp_path / "w")
    cs = _shared_flat(str(tmp_path / "flats"))
    pdir = _pdir(base, "A")
    _plan_base(pdir, [f.path for f in cs.frames])
    project.set_override(pdir, cs.frames[0].path, "out")
    plan = project.save_edits(pdir, {"options": {}, "runs": {"Ha": {"calib": {"flats": {"folder": cs.folder}}}}})
    assert plan["runs"][0]["flats"] == [cs.frames[1].path], "Nicht-Verwendung wird durch den Plan nicht überschrieben"


def _two_object_root(tmp_path):
    from tests.conftest import NINA_LIGHT, make_fits
    data = tmp_path / "data"
    for obj in ("M 31", "M 42"):
        for i in range(2):
            make_fits(str(data / obj / ("L_%d.fits" % i)), dict(NINA_LIGHT, OBJECT=obj), shape=(32, 48))
    return str(data)


def test_reanalyze_of_shared_root_uses_only_its_own_object(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    data = _two_object_root(tmp_path)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    res = project.run_report([data], None, s, log=lambda *a: None)
    assert sorted(r["project"] for r in res) == ["M 31", "M 42"]
    pdir = project.project_dir(s, "M 31")
    logs = []
    again = project.run_report(project.sources(pdir)["roots"], "M 31", s, into="M 31", log=logs.append)
    assert [r["project"] for r in again] == ["M 31"]
    lights = {f["path"] for f in project.frames(pdir) if f["type"] == "light"}
    assert lights and all(os.path.basename(os.path.dirname(p)) == "M 31" for p in lights), lights
    assert all("M 42" not in p for p in lights)
    assert project.sources(pdir)["analysis_required"] is False


def test_new_planning_project_rejects_ambiguous_shared_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    data = _two_object_root(tmp_path)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    project.create_project(s, "Planung Mix", [data])
    with pytest.raises(ValueError, match="mehrere Ziele"):
        project.run_report([data], "Planung Mix", s, into="Planung Mix", log=lambda *a: None)
    assert project.sources(project.project_dir(s, "Planung Mix"))["analysis_required"] is True
