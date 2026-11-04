"""Auswahlzustand je Projekt: Planmitgliedschaft, projekteigene Kalibriersätze, Offline-Umschalten."""
import json
import os

import pytest

from astrostack import project
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_calibcheck import _flat_header, synth_flat
from .test_e2e import star_field


def _data(tmp_path, extra_flat_set=False, rejected=False, bad_header=False):
    root = tmp_path / "data"
    for i in range(3):
        make_fits(str(root / "vdB 131" / "2026-10-12" / "HaO3" / ("L_%d.fits" % i)),
                  dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)}),
                  data=star_field(i, shape=(64, 64), n=20))
    if rejected:
        make_fits(str(root / "vdB 131" / "_rejected" / "2026-10-12" / "HaO3" / "L_bad.fits"),
                  dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:59:00"}),
                  data=star_field(9, shape=(64, 64), n=20))
    if bad_header:
        (root / "vdB 131" / "2026-10-12" / "HaO3" / "L_broken.fits").write_bytes(b"not a fits file" * 20)
    for i in range(6):
        make_fits(str(root / "FLAT" / "FLAT_2026-10-12" / "HaO3" / ("flat_%d.fits" % i)),
                  _flat_header(FILTER="HaO3", **{"DATE-OBS": "2026-10-12T18:00:%02d" % i}),
                  data=synth_flat(h=64, w=64))
    if extra_flat_set:
        for i in range(4):
            make_fits(str(root / "FLAT" / "FLAT_2026-09-01" / "HaO3" / ("old_%d.fits" % i)),
                      _flat_header(FILTER="HaO3", **{"DATE-OBS": "2026-09-01T18:00:%02d" % i}),
                      data=synth_flat(h=64, w=64))
    for i in range(6):
        make_fits(str(root / "BIAS" / "2026-10-12" / ("b_%d.fits" % i)),
                  dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0001, EXPTIME=0.0001, FILTER="HaO3"),
                  shape=(64, 64), value=500)
    return str(root)


def _analyzed(tmp_path, monkeypatch, name="vdB 131", **kw):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / ("settings-%s.json" % name)))
    root = _data(tmp_path, **kw)
    s = Settings(workdir=str(tmp_path / ("work-%s" % name)), analysis_workers=1)
    if name == "vdB 131":
        res = project.run_report([root], None, s, log=lambda *a: None)
        pdir = project.pdir_of(s, res[0]["project"])
    else:
        project.create_project(s, name, [root])
        pdir = project.pdir_of(s, name)
        res = project.run_report([root], name, s, into=name, log=lambda *a: None)
    return s, root, pdir, res


def _plan(pdir):
    return json.load(open(os.path.join(pdir, "plan.json"), encoding="utf-8"))


def _rows(pdir):
    return {f["path"]: f for f in project.frames(pdir)}


def _checked(pdir):
    return {p for p, f in _rows(pdir).items() if f["type"] == "light" and f["selected"]}


def _plan_lights(plan):
    return {p for r in plan["runs"] for p in r.get("lights", [])}


def test_write_frames_index_persists_bayer_and_raw_size(tmp_path):
    from types import SimpleNamespace as NS
    pdir = str(tmp_path / "w" / "projects" / "Roh")
    os.makedirs(pdir)
    frame = NS(path="/d/L.fits", filter="Ha", night="n", excluded=False, bayer="BGGR", width=6248, height=4176)
    project.write_frames_index(pdir, [NS(frames=[frame])], [])
    row = json.load(open(os.path.join(pdir, "analysis", "frames.json"), encoding="utf-8"))[0]
    assert row["bayer"] == "BGGR" and row["rawWidth"] == 6248 and row["rawHeight"] == 4176


def test_all_lights_visible_but_only_assigned_calibration_sets(tmp_path, monkeypatch):
    _s, root, pdir, _res = _analyzed(tmp_path, monkeypatch, extra_flat_set=True, rejected=True, bad_header=True)
    rows = _rows(pdir)
    plan = _plan(pdir)
    lights = {p for p, f in rows.items() if f["type"] == "light"}
    assert len(lights) == 4, "alle nutzbaren Lights bleiben sichtbar"
    rejected = [p for p in lights if os.sep + "_rejected" + os.sep in p]
    assert len(rejected) == 1 and rows[rejected[0]]["selected"] is False
    assert rows[rejected[0]]["status"] == "rejected"
    broken = [p for p in rows if p.endswith("L_broken.fits")]
    assert broken and rows[broken[0]]["status"] == "error" and rows[broken[0]]["selected"] is False
    light_row = next(f for p, f in rows.items() if f["type"] == "light" and "_rejected" not in p)
    assert light_row["bayer"] == "RGGB" and light_row["rawWidth"] == 64 and light_row["rawHeight"] == 64

    assigned = {r.get("flat_set") for r in plan["runs"] if r.get("flat_set")}
    flat_rows = {p for p, f in rows.items() if f["type"] == "flat"}
    assert flat_rows, "der zugeordnete Flat-Satz bleibt sichtbar"
    assert all(any(os.path.dirname(p) == folder for folder in assigned) for p in flat_rows)
    assert not any("FLAT_2026-09-01" in p for p in flat_rows), "nicht zugeordnete globale Flats bleiben draußen"
    # Der andere Satz existiert weiterhin auf der Platte, wird aber nur auf bewusste Wahl sichtbar.
    assert (tmp_path / "data" / "FLAT" / "FLAT_2026-09-01").is_dir()
    facts = project.project_facts(pdir, plan)
    assert facts["lights_total"] == len(lights), "Gesamt bleibt alle Lights, nicht nur die genutzten"
    assert facts["lights_used"] == sum(r.get("lightCount") or 0 for r in plan["runs"])


def test_checked_frames_follow_actual_enabled_plan_membership(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    plan = _plan(pdir)
    name = plan["runs"][0]["name"]
    lights = set(plan["runs"][0]["lights"])
    assert _checked(pdir) == lights
    project.save_edits(pdir, {"runs": {name: {"enabled": False}}})
    plan = _plan(pdir)
    assert plan["runs"][0]["enabled"] is False
    assert set(plan["runs"][0]["lights"]) == lights, "Planpfade bleiben für spätere Aktivierung erhalten"
    assert plan["total_frames"] == 0 and plan["planned_frames"] == len(lights)
    rows = _rows(pdir)
    assert _checked(pdir) == set(), "deaktivierte Läufe werden von der Engine übersprungen"
    assert all(rows[p]["disabled_run"] is True for p in lights)
    assert all("deaktiviert" in (rows[p]["reason"] or "") for p in lights)
    assert all(f["selected"] is False for f in rows.values() if f["type"] != "light"), \
        "auch Kalibrierframes eines deaktivierten Laufs sind nicht gecheckt"
    facts = project.project_facts(pdir, plan)
    assert facts["lights_used"] == 0 and facts["hours_used"] == 0
    delta = project.selection_delta(pdir)
    assert delta["facts"]["lights_used"] == 0 and delta["facts"]["lights_planned"] == len(lights)
    project.save_edits(pdir, {"runs": {name: {"enabled": True}}})
    assert _checked(pdir) == lights
    assert _plan(pdir)["total_frames"] == len(lights)
    light_rows = [f for f in _rows(pdir).values() if f["type"] == "light"]
    assert project.frames_summary(light_rows)["selected"] == len(lights)
    assert project.frames_summary(list(_rows(pdir).values()))["selected"] > len(lights), "Kalibrierframes bleiben sichtbar"


def test_parallel_toggles_and_edits_share_one_locked_state(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    lights = _plan(pdir)["runs"][0]["lights"]

    def work(i):
        if i % 2:
            project.set_override(pdir, lights[i % len(lights)], "out")
        else:
            opt = project.BOOL_OPTIONS[(i // 2) % len(project.BOOL_OPTIONS)]
            project.save_edits(pdir, {"options": {opt: bool(i % 4)}})

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, range(12)))
    plan = _plan(pdir)
    json.load(open(os.path.join(pdir, "overrides.json"), encoding="utf-8"))
    json.load(open(os.path.join(pdir, "selection_context.json"), encoding="utf-8"))
    assert plan["selection_revision"] == project.selection_delta(pdir)["revision"]
    assert _checked(pdir) == _plan_lights(plan)
    leftovers = [os.path.join(r, f) for r, _d, files in os.walk(pdir) for f in files if f.endswith(".tmp")]
    assert leftovers == [], "eindeutige Temp-Dateien hinterlassen keine Kollisionen"


def test_reanalysis_survives_all_assigned_flats_out_and_prepares_media(tmp_path, monkeypatch):
    from astrostack import previews
    from astrostack.report import build
    s, root, pdir, _res = _analyzed(tmp_path, monkeypatch, extra_flat_set=True, bad_header=True)
    assigned_folder = _plan(pdir)["runs"][0]["flat_set"]
    flats = {p for p, f in _rows(pdir).items() if f["type"] == "flat"}
    for p in flats:
        project.set_override(pdir, p, "out")
    assert _plan(pdir)["runs"][0]["flats"] == []
    before_ov = json.load(open(os.path.join(pdir, "overrides.json"), encoding="utf-8"))
    res = project.run_report([root], "vdB 131", s, into="vdB 131", log=lambda *a: None)
    assert res and res[0]["project"] == "vdB 131"
    assert project.sources(pdir).get("analysis_required") is False
    assert json.load(open(os.path.join(pdir, "overrides.json"), encoding="utf-8")) == before_ov
    plan = _plan(pdir)
    assert all(not r.get("flats") for r in plan["runs"]), "kein Ersatz-Flatsatz nach erneuter Analyse"
    assert plan["runs"][0]["flat_set"] == assigned_folder
    assert json.load(open(os.path.join(pdir, "plan_base.json"), encoding="utf-8"))["runs"][0]["flat_set"] == assigned_folder
    report = build.embedded_report_data(open(os.path.join(pdir, "report.html"), encoding="utf-8").read())
    assert report["plan"]["runs"][0]["flat_set"] == assigned_folder, \
        "Bericht trägt den zugeordneten Satz, nicht einen Fremdsatz"
    rows = _rows(pdir)
    visible = {p for p, f in rows.items() if f["type"] == "flat"}
    assert flats <= visible, "abgewählte, zugeordnete Flats bleiben sichtbar"
    assert all(rows[p]["selected"] is False for p in flats)
    adir = os.path.join(pdir, "analysis")
    for p, f in rows.items():
        if f["type"] == "unknown" or f["status"] == "error":
            continue
        assert previews.cached_media(p, adir, "screen") is not None, "Vorschau fehlt für %s" % p
    unknown = [f for f in rows.values() if f["type"] == "unknown"]
    assert unknown and unknown[0]["status"] == "error", "UNKNOWN bleibt getrennt von lesbaren Frames"
    unassigned = [r["path"] for r in json.load(open(os.path.join(adir, "frames.json"), encoding="utf-8"))
                  if "FLAT_2026-09-01" in r.get("path", "")]
    assert unassigned and all(previews.cached_media(p, adir, "screen") is None for p in unassigned), \
        "nur zugeordnete Kalibriersätze werden vorbereitet"
    keep = sorted(flats)[0]
    project.set_override(pdir, keep, "in")
    run = _plan(pdir)["runs"][0]
    assert run["flat_set"] == assigned_folder and run["flats"] == [keep], \
        "Template/Plan bleiben auf dem zugeordneten Satz"


def test_empty_foreign_sets_do_not_displace_matching_calibration(tmp_path):
    from astrostack import calibcheck, discover, plan as plan_mod
    root = tmp_path / "data"

    def light(i):
        return make_fits(str(root / "vdB 131" / "2026-10-12" / "HaO3" / ("L_%d.fits" % i)), NINA_LIGHT,
                         shape=(200, 300))

    def flat(folder, prefix, i, date):
        return make_fits(str(root / "FLAT" / folder / "HaO3" / ("%s_%d.fits" % (prefix, i))),
                         _flat_header(**{"DATE-OBS": date + "T18:00:%02d" % i}), data=synth_flat())

    def dark(folder, i, exposure, date):
        return make_fits(str(root / "DARK" / folder / ("D_%d.fits" % i)),
                         dict(NINA_LIGHT, IMAGETYP="DARK", OBJECT=None, EXPOSURE=exposure, EXPTIME=exposure,
                              **{"DATE-OBS": date + "T23:00:%02d" % i}), shape=(200, 300), value=500)

    def bias(folder, i, date):
        return make_fits(str(root / "BIAS" / folder / ("B_%d.fits" % i)),
                         dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0001, EXPTIME=0.0001,
                              **{"DATE-OBS": date + "T10:00:%02d" % i}), shape=(200, 300), value=500)

    lights = [light(i) for i in range(3)]
    good_flats = [flat("FLAT_2026-10-12", "g", i, "2026-10-12") for i in range(6)]
    old_flats = [flat("FLAT_2026-09-01", "o", i, "2026-09-01") for i in range(6)]
    good_darks = [dark("DARK_600", i, 600.0, "2026-10-12") for i in range(6)]
    old_darks = [dark("DARK_300", i, 300.0, "2026-10-12") for i in range(6)]
    good_bias = [bias("BIAS_2026-10-12", i, "2026-10-12") for i in range(6)]
    old_bias = [bias("BIAS_2025-01-01", i, "2025-01-01") for i in range(6)]
    ov = {p: "out" for p in old_flats + old_darks + old_bias}
    inv = discover.scan([str(root)])
    view = project.calib_view(inv, ov)
    calib = calibcheck.check_all(view)
    matches = [m for m in calib["matches"] if m["group"]["target"] == "vdB 131"]
    rows = [dict(path=p, status="ok", pixscale=1.0, fwhm_as=2.0) for p in lights]
    built = plan_mod.build_plan("vdB 131", inv.lights, rows, [], matches, str(tmp_path / "work"), unmeasured=set())
    run = built["runs"][0]
    assert "FLAT_2026-10-12" in run["flat_set"] and len(run["flats"]) == 6
    assert "DARK_600" in run["dark_set"] and len(run["darks"]) == 6
    assert "BIAS_2026-10-12" in run["bias_set"] and len(run["bias"]) == 6
    assert not any(c.get("emptied") for c in matches[0]["flats"] + matches[0]["darks"] + matches[0]["bias"])


def test_toggle_rebuilds_plan_without_reading_originals(tmp_path, monkeypatch):
    _s, root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    plan = _plan(pdir)
    victim = plan["runs"][0]["lights"][0]
    before = plan["runs"][0]["lightCount"]

    def forbidden(*_a, **_k):
        raise AssertionError("Umschalten darf keine Originale lesen")

    real_stat = os.stat
    root_real = os.path.realpath(root)

    def guarded_stat(path, *a, **k):
        p = os.path.realpath(str(path))
        if p == root_real or p.startswith(root_real + os.sep):
            raise AssertionError("kein stat auf Originaldaten beim Umschalten/Anzeigen")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(project.discover, "scan", forbidden)
    monkeypatch.setattr(project.analyze, "analyze_group", forbidden)
    monkeypatch.setattr(os, "stat", guarded_stat)
    project.set_override(pdir, victim, "out")
    rows = _rows(pdir)
    assert rows[victim]["selected"] is False and rows[victim]["selection_source"] == "override"
    plan = _plan(pdir)
    assert plan["runs"][0]["lightCount"] == before - 1
    assert victim not in _plan_lights(plan)
    assert project.sources(pdir).get("plan_stale") in (None, False)
    assert project.sources(pdir).get("analysis_required") is False
    delta = project.selection_delta(pdir, victim)
    assert delta["selection_ready"] is True and delta["analysis_required"] is False
    assert delta["frame"]["selected"] is False and delta["revision"] >= 1
    assert _plan(pdir)["selection_revision"] == delta["revision"], "Plan und Status tragen dieselbe Revision"
    status = project.selection_delta(pdir)
    assert status["frame"] is None and status["revision"] == delta["revision"]
    assert status["facts"]["lights_used"] == sum(r["lightCount"] for r in delta["facts"]["runs"])
    # Wieder aufnehmen ist derselbe lokale Weg.
    project.set_override(pdir, victim, "in")
    assert victim in _plan_lights(_plan(pdir))


def test_manual_reinclude_wins_for_auto_rejected_but_not_for_unreadable(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch, rejected=True, bad_header=True)
    rows = _rows(pdir)
    rejected = next(p for p, f in rows.items() if os.sep + "_rejected" + os.sep in p)
    broken = next(p for p in rows if p.endswith("L_broken.fits"))
    project.set_override(pdir, rejected, "in")
    rows = _rows(pdir)
    assert rows[rejected]["selected"] is True and rejected in _plan_lights(_plan(pdir))
    project.set_override(pdir, broken, "in")
    rows = _rows(pdir)
    assert rows[broken]["selected"] is False, "Header-/Messfehler lässt sich nicht überstimmen"
    assert broken not in _plan_lights(_plan(pdir))


def test_all_frames_of_assigned_flat_set_out_do_not_select_another_set(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch, extra_flat_set=True)
    plan = _plan(pdir)
    assigned_folder = plan["runs"][0]["flat_set"]
    assigned = {p for p, f in _rows(pdir).items() if f["type"] == "flat"}
    for p in assigned:
        project.set_override(pdir, p, "out")
    run = _plan(pdir)["runs"][0]
    assert run["flats"] == [] and run["flat_set"] == assigned_folder, "bewusst leerer Satz behält seine Herkunft"
    assert not any("FLAT_2026-09-01" in p for p in run["flats"])


def test_explicit_empty_calibration_choice_stays_empty(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch, extra_flat_set=True)
    project.save_edits(pdir, {"runs": {_plan(pdir)["runs"][0]["name"]: {"calib": {"flats": "none"}}}})
    run = _plan(pdir)["runs"][0]
    assert run["flats"] == [] and run["flat_set"] is None
    project.set_override(pdir, "/does/not/exist.fits", "out")
    assert _plan(pdir)["runs"][0]["flats"] == [], "bewusst leere Wahl bleibt auch nach Toggle leer"


def test_two_projects_share_flats_but_not_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    root = _data(tmp_path)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    project.create_project(s, "A", [root])
    project.create_project(s, "B", [root])
    project.run_report([root], "A", s, into="A", log=lambda *a: None)
    project.run_report([root], "B", s, into="B", log=lambda *a: None)
    pa, pb = project.pdir_of(s, "A"), project.pdir_of(s, "B")
    victim = _plan(pa)["runs"][0]["lights"][0]
    project.set_override(pa, victim, "out")
    assert victim not in _plan_lights(_plan(pa))
    assert victim in _plan_lights(_plan(pb)), "B behält seine eigene Auswahl"
    assert _rows(pb)[victim]["selected"] is True


def test_toggle_keeps_source_guard_for_changed_roots(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    project.set_sources(pdir, [str(other)])
    project.set_override(pdir, _plan(pdir)["runs"][0]["lights"][0], "out")
    assert project.sources(pdir)["analysis_required"] is True
    assert project.selection_delta(pdir, "/x")["analysis_required"] is True


def test_legacy_project_bootstraps_from_local_artifacts_without_scan(tmp_path, monkeypatch):
    pdir = tmp_path / "w" / "projects" / "Alt"
    os.makedirs(pdir / "analysis")
    lights = [str(tmp_path / "data" / ("L_%d.fits" % i)) for i in range(2)]
    flats = [str(tmp_path / "flats" / ("F_%d.fits" % i)) for i in range(2)]
    plan = {"target": "Alt", "total_frames": 2, "options": {}, "created": "2026-01-01T00:00:00",
            "runs": [{"name": "Ha", "filter": "Ha", "settings_key": [], "enabled": True,
                      "lights": lights, "lightCount": 2, "hours": 0.5, "flats": flats,
                      "flat_set": str(tmp_path / "flats"), "bias": [], "darks": [], "darkflats": []}]}
    json.dump(plan, open(pdir / "plan.json", "w"))
    json.dump({"roots": [str(tmp_path / "data")], "target": "Alt"}, open(pdir / "sources.json", "w"))
    rows = [dict(path=p, type="light", filter="Ha", night="2026-01-01", excluded=False) for p in lights]
    rows += [dict(path=p, type="flat", filter="Ha", night=None, excluded=(i == 1)) for i, p in enumerate(flats)]
    json.dump(rows, open(pdir / "analysis" / "frames.json", "w"))
    (pdir / "analysis" / "metrics.csv").write_text(
        "path,filter,night,exp,status\n" + "\n".join("%s,Ha,2026-01-01,900,ok" % p for p in lights) + "\n")
    json.dump({lights[1]: "out"}, open(pdir / "overrides.json", "w"))

    def forbidden(*_a, **_k):
        raise AssertionError("Öffnen darf keine Quellen scannen")

    monkeypatch.setattr(project.discover, "scan", forbidden)
    fr = _rows(str(pdir))
    assert set(fr) >= set(lights) | set(flats)
    assert fr[lights[0]]["selected"] is True
    assert fr[lights[1]]["selected"] is False, "Legacy-Wahl ohne Fingerprint bleibt erhalten"
    ctx = json.load(open(pdir / "selection_context.json", encoding="utf-8"))
    assert ctx["legacy"] is True and ctx["ready"] is True and ctx["fingerprints"] == {}
    assert project.selection_delta(str(pdir), lights[0])["selection_ready"] is True
    project.set_override(str(pdir), lights[0], "out")
    assert lights[0] not in _plan_lights(_plan(str(pdir)))
    assert _plan(str(pdir))["runs"] == [], "ohne Lights verschwindet der Lauf"
    project.set_override(str(pdir), lights[0], "in")
    assert lights[0] in _plan_lights(_plan(str(pdir))), "Template stellt den Lauf lokal wieder her"


def test_content_change_revalidates_manual_choice(tmp_path, monkeypatch):
    s, root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    victim = _plan(pdir)["runs"][0]["lights"][0]
    project.set_override(pdir, victim, "out")
    assert victim not in _plan_lights(_plan(pdir))
    # Inhaltlich veränderte Datei: die alte Entscheidung darf nicht blind weiter gelten.
    make_fits(victim, dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T22:00:00"}),
              data=star_field(7, shape=(64, 64), n=20))
    project.run_report([root], "vdB 131", s, into="vdB 131", log=lambda *a: None)
    assert victim not in json.load(open(os.path.join(pdir, "overrides.json"), encoding="utf-8"))
    assert victim in _plan_lights(_plan(pdir))
    assert project.sources(pdir).get("analysis_required") is False, "eigene Fingerprint-Bereinigung ist kein User-Race"
    project.assert_stack_ready(pdir)


def test_full_index_preserves_unassigned_sets_for_legacy_bootstrap(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch, extra_flat_set=True)
    raw = json.load(open(os.path.join(pdir, "analysis", "frames.json"), encoding="utf-8"))
    old_paths = {r["path"] for r in raw if "FLAT_2026-09-01" in r.get("path", "")}
    assert old_paths, "der vollständige Index behält auch nicht zugeordnete Sätze"
    assert not (old_paths & set(_rows(pdir))), "sichtbar bleibt nur der zugeordnete Satz"
    os.remove(os.path.join(pdir, "selection_context.json"))
    assert _rows(pdir), "Legacy-Bootstrap liest den vollständigen Index"
    ctx = json.load(open(os.path.join(pdir, "selection_context.json"), encoding="utf-8"))
    restored = {f["path"] for s in ctx["calib"] for f in s["frames"]}
    assert old_paths <= restored, "Bootstrap verliert keine Sätze aus dem Index"


def test_current_plan_revision_matches_selection_delta_after_each_toggle(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    lights = _plan(pdir)["runs"][0]["lights"]
    seen = []
    for path in lights[:2]:
        project.set_override(pdir, path, "out")
        rev = _plan(pdir)["selection_revision"]
        assert rev == project.selection_delta(pdir)["revision"]
        seen.append(rev)
    assert seen == sorted(seen) and len(set(seen)) == len(seen), "Revision steigt monoton und ist im Plan aktuell"


def test_all_lights_out_leave_no_phantom_checked_frames(tmp_path, monkeypatch):
    _s, _root, pdir, _res = _analyzed(tmp_path, monkeypatch)
    for path in _plan(pdir)["runs"][0]["lights"]:
        project.set_override(pdir, path, "out")
    plan = _plan(pdir)
    assert plan["runs"] == [] and plan["total_frames"] == 0
    rows = _rows(pdir)
    lights = {p: f for p, f in rows.items() if f["type"] == "light"}
    assert all(f["selected"] is False for f in lights.values()), "leerer Plan bedeutet keine Häkchen"
    assert project.frames_summary(list(lights.values()))["selected"] == 0


def test_legacy_project_without_artifacts_reports_not_ready_and_does_not_scan(tmp_path, monkeypatch):
    pdir = tmp_path / "w" / "projects" / "Leer"
    os.makedirs(pdir)
    json.dump({"target": "Leer", "total_frames": 0, "options": {}, "runs": []}, open(pdir / "plan.json", "w"))
    json.dump({"roots": [], "target": "Leer"}, open(pdir / "sources.json", "w"))

    def forbidden(*_a, **_k):
        raise AssertionError("kein Scan beim Öffnen")

    monkeypatch.setattr(project.discover, "scan", forbidden)
    assert project.frames(str(pdir)) == []
    d = project.selection_delta(str(pdir), "/x/L.fits")
    assert d["selection_ready"] is False and d["frame"] is None
