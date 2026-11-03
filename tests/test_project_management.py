"""Projektverwaltung: Archivieren, Stack-Metadaten, Quellordner-Flag und Stack-Sperre."""
import json
import os

import pytest

from astrostack import project
from astrostack.settings import Settings


def _project(base, name="M31", runs=True, roots=None):
    pdir = os.path.join(base, "projects", name)
    os.makedirs(os.path.join(pdir, "stacks"), exist_ok=True)
    plan = {"target": name, "total_frames": 10 if runs else 0, "options": {},
            "runs": [{"name": "RGB", "lightCount": 10, "enabled": True}] if runs else []}
    json.dump(plan, open(os.path.join(pdir, "plan.json"), "w"))
    json.dump({"roots": roots or [], "target": name}, open(os.path.join(pdir, "sources.json"), "w"))
    return pdir


def _add_stack(pdir, sid, fname):
    f = os.path.join(pdir, "stacks", fname)
    open(f, "wb").write(b"XISF0100")
    project.add_stack(pdir, dict(id=sid, run="RGB", date="2026-11-01T10:00:00", lights=10,
                                 outputs=[dict(name="RGB", file=f, preview=None)]))
    return f


def test_stack_metadata_reads_are_cache_only_and_last_known(tmp_path, monkeypatch):
    base = str(tmp_path / "w")
    source = tmp_path / "source"
    source.mkdir()
    pdir = _project(base, roots=[str(source)])
    archive = tmp_path / "archive" / "s.xisf"
    archive.parent.mkdir()
    archive.write_bytes(b"XISF0100")
    project.add_stack(pdir, dict(id="s1", run="RGB", date="2026-11-01T10:00:00", lights=10,
                                 outputs=[dict(name="RGB", file=str(archive), preview=None)]))
    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        p = os.path.abspath(str(path))
        if p == str(source) or p.startswith(str(source) + os.sep) or p == str(archive):
            raise AssertionError("Metadaten-GET darf Quelle/Stack-Original nicht statten")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    assert project.stacks(pdir)[0]["outputs"][0]["exists"] is True, "last-known state"
    assert project.list_projects(Settings(workdir=base))[0]["title"] == "M31"
    assert project.project_facts(pdir)["stacks"] == 1
    assert project.detail_extras(pdir)["plan"]["total_frames"] == 10
    monkeypatch.setattr(os, "stat", real_stat)
    archive.unlink()
    assert project.stacks(pdir)[0]["outputs"][0]["exists"] is True, "ohne Prüfung bleibt der letzte Stand"
    assert project.stacks(pdir, verify_files=True)[0]["outputs"][0]["exists"] is False, "explizite Prüfung sieht die fehlende Datei"


def test_archive_hides_project_and_keeps_files(tmp_path):
    base = str(tmp_path / "w")
    pdir = _project(base)
    f = _add_stack(pdir, "s1", "a.xisf")
    s = Settings(workdir=base)
    project.archive_project(pdir)
    assert project.list_projects(s) == []
    assert [p["name"] for p in project.list_projects(s, include_archived=True)] == ["M31"]
    assert os.path.exists(f) and os.path.isdir(pdir)
    assert project.meta(pdir)["archived"] is True


def test_archived_flag_survives_meta_save(tmp_path):
    pdir = _project(str(tmp_path / "w"))
    project.archive_project(pdir)
    project.save_meta(pdir, name="Neu")
    assert project.meta(pdir)["archived"] is True


def test_stack_rename_is_metadata_only(tmp_path):
    pdir = _project(str(tmp_path / "w"))
    f = _add_stack(pdir, "s1", "a.xisf")
    project.update_stack_meta(pdir, "s1", name="Schöner Stack")
    st = project.stacks(pdir)
    assert st[0]["name"] == "Schöner Stack"
    assert os.path.exists(f)


def test_stack_remove_keeps_file_and_blocks_legacy_reimport(tmp_path):
    pdir = _project(str(tmp_path / "w"))
    f = _add_stack(pdir, "s1", "a.xisf")
    project.remove_stack(pdir, "s1")
    assert project.stacks(pdir) == []
    assert os.path.exists(f), "Datei darf nie gelöscht werden"
    assert [r["id"] for r in project.stacks(pdir, include_removed=True)] == ["s1"]
    # erneutes Lesen darf nichts aus Altdateien wieder einblenden
    assert project.stacks(pdir) == []


def test_stack_ready_requires_analysis_and_lights(tmp_path):
    base = str(tmp_path / "w")
    pdir = _project(base, runs=False)
    with pytest.raises(ValueError):
        project.assert_stack_ready(pdir)
    pdir2 = _project(base, name="M42")
    project.assert_stack_ready(pdir2)
    project.set_sources(pdir2, [str(tmp_path / "x")])
    with pytest.raises(ValueError):
        project.assert_stack_ready(pdir2)


def test_changed_roots_require_reanalysis_same_roots_do_not(tmp_path):
    base = str(tmp_path / "w")
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    pdir = _project(base, roots=[a])
    project.set_sources(pdir, [a])
    assert project.sources(pdir).get("analysis_required") in (None, False)
    project.set_sources(pdir, [a, b])
    assert project.sources(pdir)["analysis_required"] is True
    assert project.sources(pdir)["roots"] == [a, b]


def test_legacy_sources_without_flag_stay_valid(tmp_path):
    pdir = _project(str(tmp_path / "w"))
    project.assert_stack_ready(pdir)


def test_create_planning_project_without_lights(tmp_path):
    s = Settings(workdir=str(tmp_path / "w"))
    pdir = project.create_project(s, "  Planung M33  ", [])
    assert os.path.basename(pdir) == "Planung M33"
    assert project.meta(pdir)["name"] == "Planung M33"
    assert project.sources(pdir).get("analysis_required") is False
    lst = project.list_projects(s)
    assert [p["title"] for p in lst] == ["Planung M33"]
    with pytest.raises(ValueError):
        project.assert_stack_ready(pdir)


def test_create_with_roots_requires_analysis_and_rejects_duplicates(tmp_path):
    s = Settings(workdir=str(tmp_path / "w"))
    a = str(tmp_path / "data")
    pdir = project.create_project(s, "M42", [a, a + "/"])
    assert project.sources(pdir)["analysis_required"] is True
    assert project.sources(pdir)["roots"] == [a]
    with pytest.raises(ValueError):
        project.create_project(s, "M42", [])
    with pytest.raises(ValueError):
        project.create_project(s, "   ", [])


def test_empty_project_thumbnail_and_detail_defaults(tmp_path, monkeypatch):
    from PIL import Image
    from astrostack import assets
    s = Settings(workdir=str(tmp_path / "w"))
    pdir = project.create_project(s, "Leer", [])
    os.makedirs(os.path.join(pdir, "final"), exist_ok=True)
    Image.new("RGB", (30, 20), (1, 2, 3)).save(os.path.join(pdir, "final", "x.jpg"))
    assets.refresh(pdir, render_previews=True)      # explicit preparation, not part of a browser GET
    source = tmp_path / "source"
    source.mkdir()
    archive = tmp_path / "archive" / "stack.xisf"
    archive.parent.mkdir()
    archive.write_bytes(b"XISF0100")
    project.add_stack(pdir, dict(id="ext", run="RGB", date="2026-11-01T10:00:00", lights=1,
                                 outputs=[dict(name="RGB", file=str(archive), preview=str(archive))]))
    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        p = os.path.abspath(str(path))
        if p == str(source) or p.startswith(str(source) + os.sep) or p == str(archive):
            raise AssertionError("GET darf Quelle/Stack-Original nicht anfassen")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    t = project.thumbnail(pdir)
    assert t and os.path.basename(t).endswith(".jpg") and "asset-previews" in t
    d = project.detail_extras(pdir)
    assert d["analysis_required"] is False
    assert d["plan"]["runs"] == [] and d["plan"]["options"] == {} and d["plan"]["total_frames"] == 0
    assert d["assets"][0]["name"] == "x.jpg" and d["assets"][0]["exists"]


def test_toggle_rebuilds_without_stale_guard(tmp_path):
    pdir = _project(str(tmp_path / "w"))
    project.assert_stack_ready(pdir)
    project.set_override(pdir, "/x/a.xisf", "out")
    assert project.sources(pdir).get("plan_stale") in (None, False)
    assert project._read(os.path.join(pdir, "overrides.json"), {}) == {"/x/a.xisf": "out"}
    project.assert_stack_ready(pdir)              # a toggle does not invalidate the plan


def test_frames_index_lists_all_frames_with_selection(tmp_path):
    from types import SimpleNamespace as NS
    pdir = _project(str(tmp_path / "w"))
    lights = [NS(target="M31", frames=[
        NS(path="/d/L1.fits", filter="Ha", night="2026-10-12", excluded=False, type="LIGHT"),
        NS(path="/d/L2.fits", filter="Ha", night="2026-10-12", excluded=True, type="LIGHT")])]
    calibs = [NS(type="flats", frames=[NS(path="/d/F1.fits", filter="Ha", night=None, excluded=False, type="FLAT")])]
    os.makedirs(os.path.join(pdir, "analysis"), exist_ok=True)
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write("path,fwhm,nstars\n/d/L1.fits,2.4,812\n")
    project.write_frames_index(pdir, lights, calibs)
    project.set_override(pdir, "/d/L2.fits", "in")
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert set(fr) == {"/d/L1.fits", "/d/L2.fits", "/d/F1.fits"}
    assert fr["/d/L1.fits"]["selected"] is True and fr["/d/L1.fits"]["selection_source"] == "auto"
    assert fr["/d/L1.fits"]["metrics"]["fwhm"] == 2.4
    # L2 hat keine Messung: ein Override 'in' macht ihn nicht stackbar (fail-closed, siehe test_frame_selection).
    assert fr["/d/L2.fits"]["selected"] is False and fr["/d/L2.fits"]["selection_source"] == "override"
    assert fr["/d/L2.fits"]["stale"] is False and fr["/d/L1.fits"]["stale"] is False, "Toggle ist sofort aktuell"
    assert fr["/d/F1.fits"]["type"] == "flats" and fr["/d/F1.fits"]["attention"] is None
    assert all(v is None for v in fr["/d/F1.fits"]["metrics"].values())


def test_measure_failure_reason_from_analysis_file_is_shown_on_frame(tmp_path):
    from types import SimpleNamespace as NS
    pdir = _project(str(tmp_path / "w"))
    lights = [NS(target="M31", frames=[NS(path="/d/L1.fits", filter="Ha", night="n", excluded=False),
                                       NS(path="/d/L3.fits", filter="Ha", night="n", excluded=False)])]
    project.write_frames_index(pdir, lights, [])
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write("path,nstars\n/d/L1.fits,500\n")
    # Format wie von analyze_group geschrieben: Liste mit vollständigen Pfaden
    json.dump([{"path": "/d/L3.fits", "kind": "measure", "reason": "kein Stern erkannt"}],
              open(os.path.join(pdir, "analysis", "measure_errors.json"), "w"))
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert fr["/d/L3.fits"]["measure_error"].endswith("kein Stern erkannt")
    assert fr["/d/L3.fits"]["status"] == "error"
    summ = project.frames_summary(list(fr.values()))
    assert summ["error"] == 1 and summ["total"] == 2 and summ["selected"] == 1


def test_scan_and_calibration_receive_shared_catalog_dir(tmp_path, monkeypatch):
    from astrostack import discover
    s = Settings(workdir=str(tmp_path / "w"))
    seen = {}
    monkeypatch.setattr(discover, "scan", lambda roots, opt, progress=None: seen.setdefault("scan", opt) and None)
    project.scan([str(tmp_path)], s)
    assert seen["scan"].catalog_dir == os.path.join(s.workdir, "cache", "catalog")
    assert project.catalog_dir(s) == os.path.join(s.workdir, "cache", "catalog")


def test_frames_index_creates_analysis_dir(tmp_path):
    from types import SimpleNamespace as NS
    pdir = str(tmp_path / "w" / "projects" / "Neu")
    os.makedirs(pdir)
    project.write_frames_index(pdir, [], [NS(type="bias", frames=[NS(path="/c/B1.fits", filter=None, night=None, excluded=False)])])
    assert project.frames(pdir)[0]["type"] == "bias"


def test_frame_api_metrics_status_and_failed_measurements(tmp_path):
    from types import SimpleNamespace as NS
    pdir = _project(str(tmp_path / "w"))
    lights = [NS(target="M31", frames=[
        NS(path="/d/L1.fits", filter="Ha", night="n", excluded=False),
        NS(path="/d/L2.fits", filter="Ha", night="n", excluded=False),
        NS(path="/d/L3.fits", filter="Ha", night="n", excluded=False)])]
    project.write_frames_index(pdir, lights, [])
    # L3 fehlt in metrics.csv = Messung fehlgeschlagen; hfr_name fehlt bei L2 (None statt Erfindung)
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write(
        "path,nstars,ecc,fwhm,hfr_name\n/d/L1.fits,812,0.41,2.4,\n/d/L2.fits,640,0.52,2.9,2.7\n")
    json.dump({"/d/L1.fits": {"auto": "warn", "status": "warn", "flags": [["warn", "elongierte Sterne"]]},
               "/d/L2.fits": {"auto": "suggest", "status": "suggest", "flags": [["reject", "unscharf (FWHM)"]]}},
              open(os.path.join(pdir, "analysis", "assessment.json"), "w"))
    fr = {f["path"]: f for f in project.frames(pdir)}
    m1 = fr["/d/L1.fits"]["metrics"]
    assert m1["nstars"] == 812 and m1["ecc"] == 0.41 and m1["fwhm"] == 2.4 and m1["hfr_name"] is None
    assert fr["/d/L2.fits"]["metrics"]["hfr_name"] == 2.7
    assert fr["/d/L1.fits"]["status"] == "warn" and fr["/d/L1.fits"]["flags"] == [{"level": "warn", "text": "elongierte Sterne"}]
    assert fr["/d/L2.fits"]["status"] == "suggest" and fr["/d/L2.fits"]["selected"] is False
    f3 = fr["/d/L3.fits"]
    assert f3["status"] == "error" and f3["attention"] == "error" and f3["measure_error"]
    assert f3["metrics"]["nstars"] is None and f3["selected"] is False, "unvermessen wird nicht still übernommen"
    assert fr["/d/L1.fits"]["attention"] == "warn"


def test_calib_override_filters_out_frames_keeps_set_id_and_index(tmp_path):
    from types import SimpleNamespace as NS
    from astrostack import discover
    cs = discover.CalibSet(type="flats", filter="Ha", settings=(), folder="/c", exposure=2.0, session="s1")
    cs.frames = [NS(path="/c/F1.fits", excluded=True, filter="Ha", night=None),
                 NS(path="/c/F2.fits", excluded=False, filter="Ha", night=None)]
    sid = cs.id
    project.apply_frame_overrides([cs], {"/c/F1.fits": "in", "/c/F2.fits": "out"})
    assert [f.path for f in cs.frames] == ["/c/F1.fits"] and cs.frames[0].excluded is False
    assert cs.id == sid, "Set-ID (Ordner/Belichtung/Session) bleibt beim Filtern erhalten"
    pdir = _project(str(tmp_path / "w"))
    project.set_override(pdir, "/c/F1.fits", "in")
    project.set_override(pdir, "/c/F2.fits", "out")
    project.write_frames_index(pdir, [], [cs])
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert set(fr) == {"/c/F1.fits", "/c/F2.fits"}, "Anzeigeindex behält alle Calibframes"
    assert fr["/c/F2.fits"]["selected"] is False and fr["/c/F2.fits"]["selection_source"] == "override"


def test_auto_selection_follows_report_assessment_and_override_wins(tmp_path):
    from types import SimpleNamespace as NS
    pdir = _project(str(tmp_path / "w"))
    lights = [NS(target="M31", frames=[NS(path="/d/L1.fits", filter="Ha", night="n", excluded=False),
                                       NS(path="/d/L2.fits", filter="Ha", night="n", excluded=False)])]
    project.write_frames_index(pdir, lights, [])
    open(os.path.join(pdir, "analysis", "metrics.csv"), "w").write("path,nstars\n/d/L1.fits,500\n/d/L2.fits,600\n")
    json.dump({"/d/L1.fits": "suggest", "/d/L2.fits": "warn"}, open(os.path.join(pdir, "analysis", "assessment.json"), "w"))
    fr = {f["path"]: f for f in project.frames(pdir)}
    assert fr["/d/L1.fits"]["selected"] is False and fr["/d/L1.fits"]["auto_status"] == "suggest"
    assert fr["/d/L2.fits"]["selected"] is True
    project.set_override(pdir, "/d/L1.fits", "in")
    assert {f["path"]: f for f in project.frames(pdir)}["/d/L1.fits"]["selected"] is True


def test_project_facts_prefers_hours_total_over_used_hours(tmp_path):
    pdir = _project(str(tmp_path / "w"))
    json.dump({"lights": [dict(target="M31", filter="Ha", frames=3, hours=1.0, hours_total=2.5, nights={})]},
              open(os.path.join(pdir, "inventory.json"), "w"))
    assert project.project_facts(pdir, {"runs": []}, [])["hours_total"] == 2.5
    json.dump({"lights": [dict(target="M31", filter="Ha", frames=3, hours=1.0, nights={})]},
              open(os.path.join(pdir, "inventory.json"), "w"))
    assert project.project_facts(pdir, {"runs": []}, [])["hours_total"] == 1.0


def test_server_stack_start_is_blocked_without_valid_analysis(tmp_path, monkeypatch):
    from astrostack.app import server
    base = str(tmp_path / "w")
    s = Settings(workdir=base)
    project.create_project(s, "Planung", [str(tmp_path / "data")])
    monkeypatch.setattr(server, "settings", lambda: s)
    with pytest.raises(ValueError):
        server.start_stack("Planung")


def test_delete_is_archive_only(tmp_path):
    s = Settings(workdir=str(tmp_path / "w"))
    pdir = project.create_project(s, "Test", [])
    f = os.path.join(pdir, "final", "x.jpg")
    os.makedirs(os.path.dirname(f))
    open(f, "wb").write(b"jpg")
    project.archive_project(pdir)
    assert os.path.exists(f) and os.path.isdir(pdir)
    assert project.list_projects(s) == []


# ------------------------------------------------------------ Fail-closed bei Reanalyse

def _analyzed_project(tmp_path, monkeypatch, obj="M 31"):
    """Kleines, echt analysiertes Projekt als Ausgangslage für Reanalyse-Fehlerpfade."""
    from tests.conftest import NINA_LIGHT, make_fits
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    data = tmp_path / "lights"
    for i in range(2):
        make_fits(str(data / ("L_%d.fits" % i)), dict(NINA_LIGHT, OBJECT=obj), shape=(32, 48))
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    res = project.run_report([str(data) + os.sep, str(data)], None, s, log=lambda *a: None)
    assert len(res) == 1
    pdir = project.project_dir(s, res[0]["project"])
    return s, str(data), pdir


def _blocked(pdir):
    with pytest.raises(ValueError):
        project.assert_stack_ready(pdir)


def test_successful_analysis_releases_guard_only_with_canonical_roots(tmp_path, monkeypatch):
    s, data, pdir = _analyzed_project(tmp_path, monkeypatch)
    src = project.sources(pdir)
    assert src["roots"] == [os.path.realpath(data)], "doppelte Aliase dürfen nicht in sources.json landen"
    assert src.get("analysis_required") is False and src.get("plan_stale") in (None, False)
    project.assert_stack_ready(pdir)


def test_failed_report_keeps_old_plan_blocked(tmp_path, monkeypatch):
    s, data, pdir = _analyzed_project(tmp_path, monkeypatch)
    extra = tmp_path / "extra"
    extra.mkdir()
    project.set_sources(pdir, [data, str(extra)])
    monkeypatch.setattr(project.report_build, "build",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulierter Berichtsfehler")))
    with pytest.raises(RuntimeError):
        project.run_report(project.sources(pdir)["roots"], "M 31", s, into="M 31", log=lambda *a: None)
    assert project.detail_extras(pdir)["analysis_required"] is True
    _blocked(pdir)


def test_failed_apply_edits_keeps_old_plan_blocked(tmp_path, monkeypatch):
    s, data, pdir = _analyzed_project(tmp_path, monkeypatch)
    project.set_override(pdir, str(tmp_path / "L_0.fits"), "out")
    monkeypatch.setattr(project, "apply_edits",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulierter Planfehler")))
    with pytest.raises(RuntimeError):
        project.run_report([data], "M 31", s, into="M 31", log=lambda *a: None)
    assert project.detail_extras(pdir)["analysis_required"] is True
    _blocked(pdir)


def test_cancel_after_measure_keeps_guard_blocked(tmp_path, monkeypatch):
    import threading
    s, data, pdir = _analyzed_project(tmp_path, monkeypatch)
    cancel = threading.Event()
    real = project.analyze.analyze_group

    def measure_then_cancel(frames, adir, workers, **kw):
        real(frames, adir, workers, **kw)
        cancel.set()

    monkeypatch.setattr(project.analyze, "analyze_group", measure_then_cancel)
    res = project.run_report([data], "M 31", s, into="M 31", cancel=cancel, log=lambda *a: None)
    assert res == []
    assert project.detail_extras(pdir)["analysis_required"] is True
    _blocked(pdir)


def test_source_race_during_analysis_keeps_guard_blocked(tmp_path, monkeypatch):
    s, data, pdir = _analyzed_project(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    real = project.analyze.analyze_group

    def race(frames, adir, workers, **kw):
        real(frames, adir, workers, **kw)
        project.set_sources(pdir, [str(other)])

    monkeypatch.setattr(project.analyze, "analyze_group", race)
    project.run_report([data], "M 31", s, into="M 31", log=lambda *a: None)
    assert project.detail_extras(pdir)["analysis_required"] is True
    _blocked(pdir)


def test_override_race_during_analysis_keeps_guard_blocked(tmp_path, monkeypatch):
    s, data, pdir = _analyzed_project(tmp_path, monkeypatch)
    real = project.analyze.analyze_group

    def race(frames, adir, workers, **kw):
        real(frames, adir, workers, **kw)
        project.set_override(pdir, str(tmp_path / "race.fits"), "out")

    monkeypatch.setattr(project.analyze, "analyze_group", race)
    project.run_report([data], "M 31", s, into="M 31", log=lambda *a: None)
    assert project.detail_extras(pdir)["analysis_required"] is True
    _blocked(pdir)


def test_toggle_is_atomic_when_override_write_fails(tmp_path, monkeypatch):
    pdir = _project(str(tmp_path / "w"))
    plan_before = project._read(os.path.join(pdir, "plan.json"), {})
    real_write = project._write

    def fail_overrides(path, data):
        if path.endswith("overrides.json"):
            raise OSError("simulierter Absturz")
        return real_write(path, data)

    monkeypatch.setattr(project, "_write", fail_overrides)
    with pytest.raises(OSError):
        project.set_override(pdir, "/x/a.xisf", "out")
    monkeypatch.setattr(project, "_write", real_write)
    assert project._read(os.path.join(pdir, "overrides.json"), {}) == {}
    assert project._read(os.path.join(pdir, "plan.json"), {}) == plan_before
    project.assert_stack_ready(pdir)
