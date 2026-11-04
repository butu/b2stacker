"""Regressionen aus der Härtungsrunde 0.9.3: komische Daten, kaputte Einstellungen, API-Eingaben, Messfehler."""
import json
import os
import subprocess
import sys

import numpy as np
import pytest

from astrostack import analyze, cli, filters, fitsmeta as fm, plan, project
from astrostack.app import server
from astrostack.report import assess
from astrostack.settings import LOAD_PROBLEMS, Settings

from .conftest import NINA_LIGHT, make_fits

H, W = 400, 600


def star_field(seed=0, fwhm=2.6, ecc=0.0, trail=False, n=60):
    """Sternfeld mit festen Sternpositionen; optional Nachführfehler (ecc) oder Satellitenspur."""
    rng, srng = np.random.default_rng(seed), np.random.default_rng(42)
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.full((H, W), 1500.0)
    s = fwhm / 2.355
    for _ in range(n):
        cy, cx, a = srng.uniform(12, H - 12), srng.uniform(12, W - 12), srng.uniform(3000, 25000)
        img += a * np.exp(-((yy - cy) ** 2 / (2 * s ** 2) + (xx - cx) ** 2 / (2 * (s * (1 + ecc)) ** 2)))
    if trail:
        for t in range(W):
            y = int(20 + t * 0.5)
            if 0 <= y < H:
                img[max(y - 1, 0):y + 2, t] += 8000
    return np.clip(img + rng.normal(0, 15, img.shape), 0, 65535).astype(np.uint16)


# ------------------------------------------------------------------ Filter

@pytest.mark.parametrize("name,kind", [
    ("Ha/OIII 7nm", filters.DUAL_HA_OIII), ("OIII/Ha", filters.DUAL_HA_OIII), ("Ha 7nm + OIII 7nm", filters.DUAL_HA_OIII),
    ("HaO3", filters.DUAL_HA_OIII), ("Optolong L-eXtreme 2\"", filters.DUAL_HA_OIII),
    ("OIII-SII", filters.DUAL_SII_OIII), ("SII/OIII", filters.DUAL_SII_OIII),
    ("Antlia 3nm Ha Pro", filters.NARROW_HA), ("H-alpha", filters.NARROW_HA), ("OIII 3nm", filters.NARROW_OIII),
    ("Astronomik SII 6nm", filters.NARROW_SII), ("Ha SII OIII", filters.UNKNOWN), ("Baader UV/IR Cut", filters.BROADBAND),
    ("L", filters.MONO_L), ("", filters.BROADBAND), ("Quatsch", filters.UNKNOWN),
    ("SII 6nm + OIII 6nm", filters.DUAL_SII_OIII), ("Baader HOO", filters.DUAL_HA_OIII),
    ("Ha 3nm", filters.NARROW_HA),
])
def test_filter_names_with_several_lines(name, kind):
    assert filters.classify(name) == kind


# ------------------------------------------------------------------ Header

def test_garbage_date_is_dropped_and_night_comes_from_path():
    h = dict(NINA_LIGHT, **{"DATE-OBS": "gestern abend"})
    h.pop("DATE-LOC")
    m = fm.from_header(h, "/d/M 31/2026-10-20/RGB/x.fits")
    assert m.date_obs is None
    assert m.night == "2026-10-20"
    assert any("unlesbar" in w for w in m.warnings) and any("Ordnername" in w for w in m.warnings)


@pytest.mark.parametrize("path,night", [
    ("/x/2026-10-21_02-15-00__2.10__RGB_300.00s_0001.fits", "2026-10-20"),    # NINA, nach Mitternacht
    ("/x/Light_NGC7000_180.0s_Bin1_gain100_20261021-213000_-10.0C_0001.fit", "2026-10-21"),  # ASIAIR
    ("/x/2026-10-21_12-30-00__RGB_0001.fits", "2026-10-21"),                     # Grenze: genau 12 h abziehen
    ("/x/2026-10-21_11-59-00__RGB_0001.fits", "2026-10-20"),
    ("/a/2026-02-30/x.fits", None), ("/a/b/x.fits", None),
])
def test_night_from_path(path, night):
    assert fm.night_from_path(path) == night


def test_negative_exposure_is_not_a_bias():
    m = fm.from_header(dict(NINA_LIGHT, EXPOSURE=-5.0, EXPTIME=-5.0), "/x/L.fits")
    assert m.type == fm.LIGHT and m.exposure is None
    assert any("ungültig" in w for w in m.warnings)


def test_iso_accepts_timezone_suffix_and_rejects_garbage():
    assert fm._iso("2026-10-20T21:00:00Z") == "2026-10-20T21:00:00"
    assert fm._iso("2026-10-20T21:00:00+00:00") == "2026-10-20T21:00:00"
    assert fm._iso("kaputt") is None


@pytest.mark.parametrize("exc,part", [
    (FileNotFoundError("x"), "nicht mehr vorhanden"), (PermissionError("x"), "Lesezugriff"),
    (OSError("Empty or corrupt FITS file"), "leer oder beschädigt"),
    (OSError("No SIMPLE card found, this file does not appear to be a valid FITS file"), "keine gültige FITS"),
    (ValueError("cannot reshape array of size 10 into shape (5,5)"), "unvollständig"),
    (fm.FrameUnusable("Bild ist leer"), "Bild ist leer"), (MemoryError(), "Arbeitsspeicher"),
    (OSError("File may have been truncated"), "unvollständig"), (ValueError("buffer is too small"), "unvollständig"),
    (OSError("does not appear to be a valid FITS"), "keine gültige FITS"),
    (ValueError("XISF: Block fehlt"), "XISF: Block fehlt"), (ValueError("unbekanntes Format: x"), "unbekanntes Format"),
    (RuntimeError("etwas"), "Datei nicht lesbar: etwas"),
])
def test_friendly_errors(exc, part):
    msg = fm.friendly_error(exc)
    assert part in msg
    if isinstance(exc, fm.FrameUnusable):
        assert msg == str(exc)


# ------------------------------------------------------------------ Messung

def _meta():
    return fm.FrameMeta(path="x", bayer="RGGB")


@pytest.mark.parametrize("data,part", [
    (np.full((4, 4), 1000.0), "zu klein"), (np.full((H, W), np.nan), "keine gültigen"),
    (np.zeros((H, W)), "leer"), (np.full((H, W), 65535.0), "leer"),
    (np.full((3, H, W), 1000.0), "3 Bildebenen"),        # Bericht 0.9.3: Würfel ließ später das Stacken scheitern
])
def test_unusable_images_raise_clear_errors(data, part):
    with pytest.raises(fm.FrameUnusable, match=part):
        analyze._measure_raw(_meta(), data.astype(np.float32))


def test_single_plane_cube_is_a_normal_frame():
    row, _ = analyze._measure_raw(_meta(), star_field()[None].astype(np.float32))
    assert row["fwhm"]


def test_partial_nan_is_measured():
    img = star_field().astype(np.float32)
    img[:10] = np.nan
    row, _ = analyze._measure_raw(_meta(), img)
    assert row["fwhm"] and np.isfinite(row["noise"])


def test_satellite_trail_does_not_inflate_fwhm_but_tracking_errors_stay_visible():
    normal, _ = analyze._measure_raw(_meta(), star_field().astype(np.float32))
    trail, _ = analyze._measure_raw(_meta(), star_field(trail=True).astype(np.float32))
    track, _ = analyze._measure_raw(_meta(), star_field(ecc=0.9).astype(np.float32))
    assert abs(trail["fwhm"] - normal["fwhm"]) < 0.15 * normal["fwhm"], (trail["fwhm"], normal["fwhm"])
    assert abs(trail["nstars"] - normal["nstars"]) <= 0.1 * normal["nstars"]
    assert track["ecc"] > normal["ecc"] + 0.3, "Nachführfehler müssen länglich bleiben"


def test_trail_mask_needs_collinear_points():
    line = np.array([[10 + i * 5, 10 + i * 10, 3, 0.95, np.arctan2(5, 10)] for i in range(5)])
    scattered = line.copy()
    scattered[:, 0] = [10, 80, 30, 150, 60]                 # parallel, aber verstreut (Nachführung)
    assert analyze._trail_mask(line).all()
    assert not analyze._trail_mask(scattered).any()
    assert not analyze._trail_mask(line[:3]).any(), "weniger als 4 Punkte sind keine Spur"
    assert analyze._trail_mask(line[:4]).all(), "genau 4 kollineare Punkte sind eine Spur"


def _write_lights(folder, n, **kw):
    paths = []
    for i in range(n):
        hdr = dict(NINA_LIGHT, FILTER="RGB", **{"DATE-OBS": "2026-10-12T20:%02d:00" % i})
        hdr.pop("DATE-LOC")
        paths.append(make_fits(os.path.join(folder, "L_%04d.fits" % i), hdr, data=star_field(i, **kw)))
    return paths


def _metas(paths):
    return [fm.read_meta(p) for p in paths]


def test_duplicate_content_is_measured_once_and_flagged(tmp_path):
    orig = _write_lights(str(tmp_path / "data" / "a"), 3)
    copy = str(tmp_path / "data" / "backup" / "L_0000.fits")
    os.makedirs(os.path.dirname(copy))
    with open(orig[0], "rb") as a, open(copy, "wb") as b:
        b.write(a.read())
    logs = []
    out = str(tmp_path / "an")
    analyze.analyze_group(_metas(orig + [copy]), out, workers=1, catalog_dir=str(tmp_path / "cat"), log=logs.append)
    assert any("identischem Inhalt" in x and x.startswith("1 ") for x in logs), logs
    rows = assess.load_rows(os.path.join(out, "metrics.csv"))
    assess.assess(rows)
    flagged = [r for r in rows if any("Duplikat" in f[1] for f in r["flags"])]
    assert len(flagged) == 1 and flagged[0]["status"] == "suggest"


def test_broken_files_are_not_reread_until_they_change(tmp_path):
    good = _write_lights(str(tmp_path / "d"), 2)
    bad = make_fits(str(tmp_path / "d" / "black.fits"), dict(NINA_LIGHT, FILTER="RGB"), data=np.zeros((H, W), np.uint16))
    frames = _metas(good + [bad])
    out = str(tmp_path / "an")
    analyze.analyze_group(frames, out, workers=1, catalog_dir=str(tmp_path / "cat"))
    logs = []
    analyze.analyze_group(frames, out, workers=1, catalog_dir=str(tmp_path / "cat"), log=logs.append)
    assert any("1 unveränderte defekte" in x for x in logs), logs
    errs = json.load(open(os.path.join(out, analyze.ERRORS_FILE)))
    assert [e["path"] for e in errs] == [bad], "Fehler bleibt sichtbar"
    make_fits(bad, dict(NINA_LIGHT, FILTER="RGB"), data=star_field(9))     # repariert
    os.utime(bad, (1, 1))
    analyze.analyze_group(_metas(good + [bad]), out, workers=1, catalog_dir=str(tmp_path / "cat"))
    assert json.load(open(os.path.join(out, analyze.ERRORS_FILE))) == []


def test_old_trail_measurements_are_remeasured_once(tmp_path):
    paths = _write_lights(str(tmp_path / "d"), 4) + _write_lights(str(tmp_path / "t"), 1, trail=True)
    frames = _metas(paths)
    out = str(tmp_path / "an")
    analyze.analyze_group(frames, out, workers=1)
    p = os.path.join(out, "metrics.csv")
    import csv
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    for r in rows:                                   # Zustand wie aus Version 0.9.2: alter Algorithmus, Spur = unscharf
        r["star_algo"] = ""
        if "/t/" in r["path"].replace("\\", "/"):
            r["fwhm"] = "9.9"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    logs = []
    analyze.analyze_group(frames, out, workers=1, log=logs.append)
    assert any("1 nachgemessen" in x for x in logs), logs
    fixed = {r["path"]: r for r in csv.DictReader(open(p, encoding="utf-8"))}
    assert float(fixed[paths[-1]]["fwhm"]) < 4
    logs = []
    analyze.analyze_group(frames, out, workers=1, log=logs.append)
    assert not any("nachgemessen" in x for x in logs), "nur einmal nachmessen"


# ------------------------------------------------------------------ Bericht/Plan

def test_report_survives_frame_without_any_date(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    data = str(tmp_path / "data")
    _write_lights(data, 3)
    hdr = dict(NINA_LIGHT, FILTER="RGB", **{"DATE-OBS": "kaputt"})
    hdr.pop("DATE-LOC")
    make_fits(os.path.join(data, "ohne_datum.fits"), hdr, data=star_field(7))
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    res = project.run_report([data], None, s, log=lambda *a: None)
    p = json.load(open(os.path.join(project.project_dir(s, res[0]["project"]), "plan.json")))
    assert p["runs"] and p["runs"][0]["lightCount"] >= 3


class _G:
    def __init__(self, filt, frames, settings):
        self.filter, self.frames, self.settings = filt, [None] * frames, settings


def test_run_names_stay_unique_for_same_filter_with_other_settings():
    a = _G("RGB", 50, ("cam", 100.0, 50.0, "hcg", 1, 6000, 4000))
    b = _G("RGB", 5, ("cam", 0.0, 50.0, "hcg", 1, 6000, 4000))
    c = _G("RGB", 3, ("cam", 100.0, 50.0, "hcg", 2, 3000, 2000))
    d = _G("Ha/OIII", 9, ("cam", 100.0, 50.0, "hcg", 1, 6000, 4000))
    names = [plan._run_name(g, [a, b, c, d]) for g in (a, b, c, d)]
    assert names == ["RGB", "RGB (Gain 0)", "RGB (Bin 2, 3000×2000)", "Ha_OIII"]
    assert len(set(names)) == 4


@pytest.mark.parametrize("other,label", [
    (("cam", 100.0, 50.0, "lcg", 1, 6000, 4000), "RGB (LCG)"),
    (("cam", 100.0, 50.0, "hcg", 1, 6000, 3000), "RGB (6000×3000)"),
    (("cam", 100.0, 50.0, "hcg", 1, 5000, 4000), "RGB (5000×4000)"),
    (("cam", 100.0, 30.0, "hcg", 1, 6000, 4000), "RGB (Offset 30)"),
    (("cam2", 100.0, 50.0, "hcg", 1, 6000, 4000), "RGB (cam2)"),
    (("cam", 100.0, 50.0, "Extended Fullwell", 1, 6000, 4000), "RGB (Extended Fullwell)"),
])
def test_two_groups_same_filter(other, label):
    a = _G("RGB", 20, ("cam", 100.0, 50.0, "hcg", 1, 6000, 4000))
    b = _G("RGB", 4, other)
    assert [plan._run_name(g, [a, b]) for g in (a, b)] == ["RGB", label]
    assert plan._run_name(a, [a]) == "RGB"


# ------------------------------------------------------------------ Einstellungen

def test_corrupt_settings_never_block_start(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(p))
    LOAD_PROBLEMS.clear()
    p.write_text('{"workdir": "/x", ', encoding="utf-8")
    s = Settings.load()
    assert s.workdir and not p.exists()
    assert list(tmp_path.glob("settings.json.kaputt-*")), "kaputte Datei wird gesichert, nicht gelöscht"
    assert LOAD_PROBLEMS and "unlesbar" in LOAD_PROBLEMS[0]


def test_wrong_setting_types_fall_back_to_defaults(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(p))
    LOAD_PROBLEMS.clear()
    p.write_text(json.dumps({"workdir": 5, "analysis_workers": "viele", "sources": "abc", "site_lat": 48,
                             "keep_awake": False}), encoding="utf-8")
    s = Settings.load()
    assert len(LOAD_PROBLEMS) == 3
    LOAD_PROBLEMS.clear()
    p.write_text(json.dumps({"analysis_workers": True, "site_lon": True, "min_free_ram_gb": True}), encoding="utf-8")
    b = Settings.load()
    assert b.analysis_workers == 2 and b.site_lon is None and b.min_free_ram_gb == 8.0, "true ist keine Zahl"
    LOAD_PROBLEMS.clear()
    p.write_text(json.dumps({"workdir": 5, "analysis_workers": "viele", "sources": "abc", "site_lat": 48,
                             "keep_awake": False}), encoding="utf-8")
    s = Settings.load()
    assert s.analysis_workers == 2 and s.sources == [] and s.site_lat == 48.0 and s.keep_awake is False
    assert isinstance(s.workdir, str)
    assert len(LOAD_PROBLEMS) == 3


def test_settings_null_site_and_int_for_float(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(p))
    LOAD_PROBLEMS.clear()
    p.write_text(json.dumps({"site_lat": None, "site_lon": 15, "min_free_ram_gb": 4, "custom_filters": {"X": "broadband"},
                             "timezone": None}), encoding="utf-8")
    s = Settings.load()
    assert s.site_lat is None and s.site_lon == 15.0 and isinstance(s.site_lon, float)
    assert s.min_free_ram_gb == 4.0 and isinstance(s.min_free_ram_gb, float)
    assert s.custom_filters == {"X": "broadband"} and s.timezone == "Europe/Vienna"
    assert len(LOAD_PROBLEMS) == 1 and "timezone" in LOAD_PROBLEMS[0]
    Settings(workdir="x").save()
    assert "ü" in json.dumps({"ü": 1}, ensure_ascii=False) and json.load(open(p))["workdir"] == "x"


def test_settings_save_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    Settings(workdir=str(tmp_path)).save()
    assert json.load(open(tmp_path / "settings.json"))["workdir"] == str(tmp_path)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("key,cur,val,want", [
    ("analysis_workers", 2, "3", 3), ("keep_awake", True, "false", False), ("keep_awake", True, "nein", False),
    ("min_free_ram_gb", 8.0, "4,5", 4.5), ("workdir", "", " /a ", "/a"),
    ("analysis_workers", 2, 1, 1), ("analysis_workers", 2, 32, 32), ("min_free_ram_gb", 8.0, 0, 0.0),
    ("keep_awake", True, False, False), ("keep_awake", False, 1, True), ("custom_filters", {}, {"a": "b"}, {"a": "b"}),
    ("sources", [], ["a"], ["a"]),
])
def test_setting_values_from_ui(key, cur, val, want):
    assert server._setting_value(key, cur, val) == want


@pytest.mark.parametrize("key,cur,val", [
    ("analysis_workers", 2, 0), ("analysis_workers", 2, "zwei"), ("keep_awake", True, "vielleicht"),
    ("workdir", "", 5), ("sources", [], "abc"), ("min_free_ram_gb", 8.0, float("nan")),
    ("analysis_workers", 2, 33), ("min_free_ram_gb", 8.0, -1), ("custom_filters", {}, ["a"]), ("analysis_workers", 2, None),
])
def test_setting_values_from_ui_rejected(key, cur, val):
    with pytest.raises(ValueError):
        server._setting_value(key, cur, val)


@pytest.mark.parametrize("name,want", [("CON", "_CON"), ("com1", "_com1"), ("../../böse", "_böse"),
                                       ("x" * 300, "x" * project.MAX_NAME), ("", "unbenannt")])
def test_safe_project_names(name, want):
    assert project.safe_name(name) == want


# ------------------------------------------------------------------ Plan-Bearbeitung und API

@pytest.fixture
def small_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    data = str(tmp_path / "data")
    for i in range(3):
        make_fits(os.path.join(data, "L_%d.fits" % i), dict(NINA_LIGHT, OBJECT="M 31"), shape=(32, 48), value=1000 + i)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    res = project.run_report([data], None, s, log=lambda *a: None)
    return s, project.pdir_of(s, res[0]["project"])


@pytest.mark.parametrize("edit", [
    {"options": {"drizzle": "vielleicht"}}, {"options": {"gibtsnicht": True}}, {"options": {"weighting": "bunt"}},
    {"runs": {"Gibtsnicht": {"enabled": False}}}, {"runs": {"HaO3": {"enabled": "jein"}}},
    {"runs": {"HaO3": {"wbpp": {"a": "1,2"}}}}, {"runs": {"HaO3": {"wbpp": {"bad name": "1"}}}},
    {"runs": {"HaO3": {"reference": "irgendwas"}}}, {"runs": {"HaO3": {"top_percent": 0}}},
    {"runs": {"HaO3": {"farbe": "rot"}}}, {"runs": "x"},
])
def test_invalid_plan_edits_are_rejected_and_not_stored(small_project, edit):
    _s, pdir = small_project
    before = open(os.path.join(pdir, "plan_edits.json")).read() if os.path.exists(os.path.join(pdir, "plan_edits.json")) else None
    with pytest.raises(ValueError):
        project.save_edits(pdir, edit)
    after = open(os.path.join(pdir, "plan_edits.json")).read() if os.path.exists(os.path.join(pdir, "plan_edits.json")) else None
    assert before == after


def test_valid_plan_edits_are_stored_normalized(small_project):
    _s, pdir = small_project
    project.save_edits(pdir, {"options": {"weighting": "snr", "top_percent": "80", "hot_pixels": 0},
                              "runs": {"HaO3": {"enabled": 1, "top_percent": 1, "wbpp": {"a": " 1 "},
                                                "reference": "master:RGB", "label": "Ha+OIII"}}})
    ed = json.load(open(os.path.join(pdir, "plan_edits.json")))
    assert ed["options"] == {"weighting": "snr", "top_percent": 80, "hot_pixels": False}
    r = ed["runs"]["HaO3"]
    assert r["enabled"] is True and r["top_percent"] == 1 and r["wbpp"] == {"a": "1"} and r["reference"] == "master:RGB"
    project.save_edits(pdir, {"runs": {"HaO3": {"top_percent": 100, "enabled": "nein"}}})
    r = json.load(open(os.path.join(pdir, "plan_edits.json")))["runs"]["HaO3"]
    assert r["top_percent"] == 100 and r["enabled"] is False
    project.save_edits(pdir, {"options": {"top_percent": "", "cleanup": True}})
    assert json.load(open(os.path.join(pdir, "plan_edits.json")))["options"]["cleanup"] is True
    assert json.load(open(os.path.join(pdir, "plan_edits.json")))["options"]["top_percent"] is None


@pytest.mark.parametrize("edit", [
    {"runs": {"HaO3": {"top_percent": 101}}}, {"runs": {"HaO3": {"top_percent": "viel"}}},
    {"runs": {"HaO3": {"enabled": 2}}}, {"runs": {"HaO3": {"wbpp": "x"}}}, {"runs": {"HaO3": {"wbpp": {"a": 'x"y'}}}},
    {"runs": {"HaO3": {"wbpp": {"a": "x=y"}}}}, {"runs": {"HaO3": {"calib": "x"}}}, {"runs": {"HaO3": {"label": 5}}},
])
def test_more_invalid_plan_edits(small_project, edit):
    _s, pdir = small_project
    with pytest.raises(ValueError):
        project.save_edits(pdir, edit)


def test_text_booleans_are_understood(small_project):
    _s, pdir = small_project
    p = project.save_edits(pdir, {"options": {"local_normalization": "false"}, "runs": {"HaO3": {"enabled": "ja"}}})
    assert p["options"]["local_normalization"] is False


def test_override_only_for_image_files_and_known_values(small_project):
    _s, pdir = small_project
    with pytest.raises(ValueError):
        project.set_override(pdir, "/etc/passwd", "in")
    with pytest.raises(ValueError):
        project.set_override(pdir, "/x/a.fits", "quatsch")
    assert "/etc/passwd" not in json.load(open(os.path.join(pdir, "overrides.json"))) \
        if os.path.exists(os.path.join(pdir, "overrides.json")) else True


def test_sources_as_single_string_is_one_folder(small_project, tmp_path):
    _s, pdir = small_project
    src = project.set_sources(pdir, str(tmp_path / "data"))
    assert src["roots"] == [os.path.realpath(str(tmp_path / "data"))]
    with pytest.raises(ValueError):
        project.set_sources(pdir, [1, 2])


def test_all_runs_disabled_has_clear_message(small_project):
    _s, pdir = small_project
    project.save_edits(pdir, {"runs": {"HaO3": {"enabled": False}}})
    with pytest.raises(ValueError, match="ausgeschaltet"):
        project.assert_stack_ready(pdir)


# ------------------------------------------------------------------ CLI

def _cli(tmp_path, *args):
    env = dict(os.environ, ASTROSTACK_SETTINGS=str(tmp_path / "s.json"), B2_NO_UPDATE_CHECK="1")
    return subprocess.run([sys.executable, "-m", "astrostack", *args], capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(os.path.dirname(__file__)), timeout=120)


def test_cli_plan_text_and_errors(small_project, tmp_path):
    s, pdir = small_project
    json.dump({"workdir": s.workdir}, open(tmp_path / "s.json", "w"))
    name = os.path.basename(pdir)
    r = _cli(tmp_path, "plan", name)
    assert r.returncode == 0 and "Lights" in r.stdout, r.stderr
    r = _cli(tmp_path, "--json", "plan", name)
    d = json.loads(r.stdout)
    assert d["runs"][0]["n_lights"] == 3 and "lights" not in d["runs"][0]
    for bad, msg in ((["plan", name, "--disable", "Foo"], "nbekannter Lauf"),
                     (["plan", name, "--set", "ohne"], "mit ="),
                     (["stack", name, "--only", "Foo"], "nbekannter Lauf"),
                     (["settings", "--set", "analysis_workers=zwei"], "kein gültiger Wert"),
                     (["settings", "--set", "gibts=1"], "unbekannte Einstellung")):
        r = _cli(tmp_path, *bad)
        assert r.returncode != 0 and msg in (r.stderr + r.stdout), (bad, r.stderr)
        assert "Traceback" not in r.stderr


def test_cli_settings_parses_types(tmp_path):
    json.dump({"workdir": str(tmp_path / "w")}, open(tmp_path / "s.json", "w"))
    assert _cli(tmp_path, "settings", "--set", "site_lat=48,5", "keep_awake=nein").returncode == 0
    d = json.load(open(tmp_path / "s.json"))
    assert d["site_lat"] == 48.5 and d["keep_awake"] is False
    assert _cli(tmp_path, "settings", "--set", "site_lat=").returncode == 0
    assert json.load(open(tmp_path / "s.json"))["site_lat"] is None


def test_eta_appears_once_enough_progress():
    from astrostack import jobs
    import time as _t
    j = jobs.Job.__new__(jobs.Job)
    j.progress, j.created, j._log_step, j._log_pct = {}, "x", None, -1
    j.write = j.save = lambda *a, **k: None
    j.set_progress(step="measure", done=0, total=100)
    j._step_t0 = _t.time() - 3
    j.set_progress(step="measure", done=2, total=100)
    assert "eta_s" not in j.progress, "zu wenig Fortschritt für eine Schätzung"
    j.set_progress(step="measure", done=5, total=100)
    assert 40 <= j.progress["eta_s"] <= 70
    j._step_t0 = _t.time() - 1
    j.set_progress(step="measure", done=50, total=100)
    assert "eta_s" not in j.progress, "erst nach 2 s schätzen"


def test_progress_is_written_at_most_once_per_second_plus_final():
    from astrostack import jobs
    j = jobs.Job.__new__(jobs.Job)
    j.progress, j.created, j._log_step, j._log_pct = {}, "x", None, -1
    saves = []
    j.write = lambda *a, **k: None
    j.save = lambda: saves.append(1)
    for i in range(1, 200):
        j.set_progress(step="measure", done=i, total=200)
    assert len(saves) == 1, "erster Schritt speichert, danach gedrosselt"
    j.set_progress(step="measure", done=200, total=200)
    assert len(saves) == 2, "Abschluss wird immer gespeichert"
    j.set_progress(step="report", done=0, total=0)
    assert len(saves) == 3, "neuer Schritt wird sofort sichtbar"


def test_changelog_has_entry_for_current_version(tmp_path):
    import astrostack
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "packaging"))
    import release_notes
    out = tmp_path / "notes.md"
    release_notes.main(astrostack.__version__, str(out), "abc123")
    text = out.read_text(encoding="utf-8")
    assert "Programmfenster" in text and "abc123" in text and len(text) > 300
    assert release_notes.section("0.0.0", "## 0.1\nx") is None


def test_cached_measurement_of_multiplane_file_is_not_reused(tmp_path):
    good = _write_lights(str(tmp_path / "d"), 2)
    hdr = dict(NINA_LIGHT, FILTER="RGB")
    cube = make_fits(str(tmp_path / "d" / "cube.fits"), hdr, data=np.stack([star_field(5)] * 3))
    frames = _metas(good + [cube])
    assert frames[-1].planes == 3
    out = str(tmp_path / "an")
    analyze.analyze_group(frames, out, workers=1, catalog_dir=str(tmp_path / "cat"))
    errs = json.load(open(os.path.join(out, analyze.ERRORS_FILE)))
    assert [e["path"] for e in errs] == [cube] and "Bildebenen" in errs[0]["reason"]
    import csv
    rows = {r["path"] for r in csv.DictReader(open(os.path.join(out, "metrics.csv"), encoding="utf-8"))}
    assert cube not in rows
    logs = []
    analyze.analyze_group(frames, out, workers=1, catalog_dir=str(tmp_path / "cat"), log=logs.append)
    assert any("1 unveränderte defekte" in x for x in logs), "unveränderter Würfel wird nicht erneut gelesen"
