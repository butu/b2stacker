"""Stacking-Szenarien aus echten Windows-Berichten, mit nachgebautem PixInsight als eigenem Prozess.

Bericht 0.8: Kalibrierordner „Bias, Darks, Flats“ (Komma), Lights auf dem NAS, Kamera zwischen den Nächten gedreht
(Flat-Gruppen), nur ein Teil der Lights gewählt. Auf Windows laufen dieselben Szenarien zusätzlich über eine echte
Netzwerkfreigabe (\\\\localhost\\C$\\…), auf der Junctions scheitern wie auf einem NAS.
"""
import json
import os
import sys

import pytest

from astrostack import project, staging
from astrostack.engines import pixinsight as pi
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .fake_pixinsight import ProcRunner
from .test_calibcheck import _flat_header, synth_flat
from .test_e2e import star_field

NIGHTS = (("2026-02-14", 273.5), ("2026-02-20", 93.4), ("2026-03-01", 183.2))
CAL = "Bias, Darks, Flats"


def build(root, flat_sets="one", bias=5):
    for night, rot in NIGHTS:
        for i in range(3):
            make_fits(f"{root}/C11/NGC3486/Lights/RGB/{night}/Light_NGC3486_180s_{night}_{i:04d}.fits",
                      dict(NINA_LIGHT, OBJECT="NGC3486", FILTER="RGB", ROTATOR=rot,
                           **{"DATE-OBS": "%sT21:%02d:00" % (night, i), "DATE-LOC": "%sT22:%02d:00" % (night, i)}),
                      data=star_field(i))
    sets = [(NIGHTS[0][0], NIGHTS[0][1])] if flat_sets == "one" else list(NIGHTS)
    for night, rot in sets:
        for i in range(6):
            make_fits(f"{root}/C11/{CAL}/Flats/RGB/{night}/Flat_{i:04d}.fits",
                      _flat_header(FILTER="RGB", ROTATOR=rot, **{"DATE-OBS": "%sT06:00:%02d" % (night, i),
                                                                 "DATE-LOC": "%sT07:00:%02d" % (night, i)}),
                      data=synth_flat(h=256, w=384))
    hb = dict(NINA_LIGHT, IMAGETYP="Bias Frame", EXPOSURE=0.0001, EXPTIME=0.0001)
    hb.pop("OBJECT")
    for i in range(bias):
        make_fits(f"{root}/C11/{CAL}/Bias/Gain 120/Bias_{i:04d}.fits", hb, shape=(256, 384), value=500)


def unc(path: str) -> str | None:
    """C:\\x\\y → \\\\localhost\\C$\\x\\y (echte SMB-Freigabe auf Windows-Testrechnern)."""
    if sys.platform != "win32":
        return None
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    u = "\\\\localhost\\%s$%s" % (drive.rstrip(":"), rest)
    return u if os.path.isdir(u) else None


def analyse(tmp_path, monkeypatch, root):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1, keep_awake=False, open_results=False, min_free_ram_gb=0.1)
    res = project.run_report([root], None, s, log=lambda *a: None)
    return s, project.pdir_of(s, res[0]["project"])


def select_only(pdir, keep):
    plan = project.load_plan(pdir)
    for p in plan["runs"][0]["lights"]:
        if not keep(p):
            project.set_override(pdir, p, "out")
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir
    for r in plan["runs"]:
        r["gb"] = 0
    return plan


def stack(s, plan, monkeypatch, lines=None):
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    st = pi.run_plan(plan, s, log=(lines.append if lines is not None else lambda *a: None), runner_factory=ProcRunner)
    jobdir = os.path.join(plan["workdir"], "runs", sorted(os.listdir(os.path.join(plan["workdir"], "runs")))[-1])
    run = plan["runs"][0]
    out = os.path.join(jobdir, pi.safe_file(run["name"]))
    if not os.path.exists(os.path.join(out, "fakepi-input.json")):
        raise AssertionError("PixInsight wurde nicht gestartet: %s – %s" % (st.state, st.error))
    got = json.load(open(os.path.join(out, "fakepi-input.json"), encoding="utf-8"))
    cmd = open(os.path.join(out, "command.txt"), encoding="utf-8").read()
    return st, got, cmd, jobdir


def originals(root):
    return sorted(os.path.join(d, f) for d, _x, fs in os.walk(root) for f in fs)


@pytest.fixture(params=["local", "unc"])
def where(request, tmp_path):
    local = str(tmp_path / "nas")
    os.makedirs(local)
    if request.param == "unc":
        u = unc(local)
        if not u:
            pytest.skip("Netzwerkfreigabe \\\\localhost nur auf Windows-Testrechnern")
        return u
    return local


def test_comma_folders_and_subset_of_one_group(tmp_path, monkeypatch, where):
    """Bericht 0.8 §1/§2/§7: Komma im Kalibrierpfad + 3 Gruppen mit gleichem Flat-Satz, nur Nacht 1 gewählt."""
    build(where)
    before = originals(where)
    s, pdir = analyse(tmp_path, monkeypatch, where)
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p and not p.endswith("_0002.fits"))
    assert plan["runs"][0]["lightCount"] == 2
    lines = []
    st, got, cmd, jobdir = stack(s, plan, monkeypatch, lines)
    assert st.state == "done", st.error + "\n" + "\n".join(lines)
    assert got["ok"] and not got["errors"], got
    assert len(got["files"]["light"]) == 2                     # genau die Auswahl
    assert CAL not in cmd                                       # kein Originalpfad mit Komma in der Kommandozeile
    for k, fs in got["files"].items():                       # kein Pfad mit Komma kommt bei PixInsight an
        assert not any("," in f for f in fs), (k, fs)
    assert not got["grouping"]                                # nur eine Gruppe übrig → kein Grouping
    assert len(got["files"]["flat"]) == 6 and len(got["files"]["bias"]) == 5
    assert originals(where) == before                         # Originale unverändert
    assert not os.path.exists(os.path.join(jobdir, "_grp"))  # aufgeräumt


def test_distinct_flats_per_rotation_group_by_keyword(tmp_path, monkeypatch, where):
    build(where, flat_sets="per-night")
    s, pdir = analyse(tmp_path, monkeypatch, where)
    plan = select_only(pdir, lambda p: True)
    assert len(plan["runs"][0].get("flat_groups") or []) == 3
    st, got, cmd, jobdir = stack(s, plan, monkeypatch)
    assert st.state == "done", st.error
    assert got["grouping"] and sorted(got["groups"]) == ["1", "2", "3"]
    assert all(g.get("light") == 3 and g.get("flat") == 6 for g in got["groups"].values()), got["groups"]
    assert not got["warnings"]


def test_two_of_three_groups_selected_drops_third_and_its_flats(tmp_path, monkeypatch):
    root = str(tmp_path / "nas")
    build(root, flat_sets="per-night")
    s, pdir = analyse(tmp_path, monkeypatch, root)
    plan = select_only(pdir, lambda p: NIGHTS[2][0] not in p)
    st, got, cmd, jobdir = stack(s, plan, monkeypatch)
    assert st.state == "done", st.error
    assert len(got["files"]["light"]) == 6 and len(got["files"]["flat"]) == 12
    assert sorted(got["groups"]) == ["1", "2"]


def test_without_symlinks_copies_once_and_reuses_cache(tmp_path, monkeypatch, where):
    """Kein Entwicklermodus, anderes Laufwerk: nur gewählte Dateien werden kopiert – beim zweiten Lauf aus dem Cache."""
    root = where
    build(root)
    s, pdir = analyse(tmp_path, monkeypatch, root)
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    monkeypatch.setattr(staging, "symlinks_allowed", lambda base: False)
    real_link = os.link

    def no_cross_link(a, b):                                   # Hardlinks nur innerhalb des Arbeitsordners
        if str(tmp_path / "work") not in os.path.abspath(a):
            raise OSError(18, "cross-device link")
        return real_link(a, b)
    monkeypatch.setattr(staging.os, "link", no_cross_link)
    lines = []
    st, got, _cmd, _j = stack(s, plan, monkeypatch, lines)
    assert st.state == "done", st.error
    text = "\n".join(lines)
    assert "kopiert" in text and "Entwicklermodus" in text
    copied_first = [ln for ln in lines if "Bereitgestellt" in ln]
    lines2 = []
    st2, got2, _c, _j = stack(s, plan, monkeypatch, lines2)
    assert st2.state == "done", st2.error
    assert any("aus dem Kopie-Cache" in ln for ln in lines2), (copied_first, lines2)
    assert len(got2["files"]["light"]) == 3


def test_single_raw_bias_stops_before_pixinsight(tmp_path, monkeypatch):
    """Bericht 0.8 §8: nur 1 Roh-Bias → klare Meldung vor dem Start, PixInsight rechnet nicht umsonst."""
    root = str(tmp_path / "nas")
    build(root, bias=1)
    s, pdir = analyse(tmp_path, monkeypatch, root)
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    ProcRunner.last = None
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=ProcRunner)
    assert st.state == "failed" and "Nur 1 Bias-Bild" in st.error and "Master" in st.error, st.error
    assert ProcRunner.last is None                             # PixInsight nie gestartet


def test_wbpp_failure_is_explained_and_marks_step(tmp_path, monkeypatch):
    """Bericht 0.8 §9: scheitert WBPP trotzdem, nennt b2stacker die Ursache aus dem WBPP-Protokoll."""
    root = str(tmp_path / "nas")
    build(root, bias=1)
    s, pdir = analyse(tmp_path, monkeypatch, root)
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    monkeypatch.setattr(pi, "_preflight_calibration", lambda *a: None)
    st, got, _cmd, _j = stack(s, plan, monkeypatch)
    assert st.state == "failed" and not got["ok"]
    assert any("only 1 active" in e for e in got["errors"])
    assert "Master-Bias" in st.error and "nur 1 Bias-Bild" in st.error, st.error
    assert st.failed_step and st.causes and st.logdir


def test_app_master_bias_is_passed_as_master(tmp_path, monkeypatch):
    """Bericht 0.8 §8: MB-…fits aus Astro Pixel Processor ist ein Master, kein einzelnes Roh-Bias."""
    root = str(tmp_path / "nas")
    build(root, bias=0)
    from astropy.io import fits
    import numpy as np
    mb = f"{root}/C11/Rohsummenstacks/28.12.2024/MB-IG_120.0-E_0.001s-ZWO_ASI294MC_Pro-4144x2822-all_channels-all_sessions.fits"
    os.makedirs(os.path.dirname(mb), exist_ok=True)
    hdu = fits.PrimaryHDU(np.full((256, 384), 500, dtype=np.float32))
    hdu.header["SOFTWARE"] = "Astro Pixel Processor by Aries Productions"
    hdu.header["INSTRUME"] = NINA_LIGHT["INSTRUME"]
    hdu.writeto(mb)
    s, pdir = analyse(tmp_path, monkeypatch, root)
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    plan["runs"][0]["bias"] = [mb]                           # wie im Bericht: Master als Bias-Satz zugeordnet
    st, got, _cmd, _j = stack(s, plan, monkeypatch)
    assert st.state == "done", st.error
    assert got["files"].get("master_bias") and not got["files"].get("bias"), got["files"]


def test_staging_invariants_unit(tmp_path):
    """Gruppen auf Auswahl beschränken, gleiche Flats zusammenlegen, Sonderzeichen ersetzen – ohne PixInsight."""
    src = tmp_path / "a, b"
    files = {}
    for g in ("1", "2", "3"):
        for i in range(2):
            p = src / ("n%s" % g) / ("L%s_%d.fits" % (g, i))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            files.setdefault(g, []).append(str(p))
    flats = [str(src / "flat" / ("F%d.fits" % i)) for i in range(2)]
    for f in flats:
        os.makedirs(os.path.dirname(f), exist_ok=True)
        open(f, "wb").close()
    run = dict(lights=files["1"][:1] + files["2"], flat_groups=[dict(key=g, lights=files[g], flats=flats, nights=[g])
                                                                 for g in ("1", "2", "3")])
    lay = staging.prepare(run, dict(flats=flats, bias=[]), None, str(tmp_path / "job"), str(tmp_path / "cache"), 30000)
    assert len(lay.lights) == 3 and not lay.grouping           # gleiche Flats → zusammengelegt, kein Grouping
    assert not any("," in p for p in lay.lights + lay.calib["flats"])
    assert lay.calib["flats"] and all(os.path.exists(p) for p in lay.lights + lay.calib["flats"])
    pi.remove_link_tree(str(tmp_path / "job" / "_grp"))
    assert all(os.path.exists(p) for g in files.values() for p in g)   # Originale bleiben


def test_selftest_end_to_end_with_fake_pixinsight(tmp_path, monkeypatch):
    """Selbsttest: legt Daten mit Komma + zwei Kamerastellungen an, analysiert, stackt, schreibt Bericht."""
    from astrostack import selftest
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1, keep_awake=False, open_results=False, min_free_ram_gb=0.1)
    folder = tmp_path / "nas"
    folder.mkdir()
    res = selftest.run(s, str(folder), log=lambda *a: None, runner_factory=ProcRunner)
    assert res["stack"]["state"] == "done", res
    assert any(st["name"] == "Analyse" and "2 Flat-Gruppen" in st["detail"] for st in res["steps"]), res["steps"]
    assert os.path.exists(res["report"]) and "," in os.listdir(str(folder))[0]
    assert project.list_projects(s) == []                      # Testprojekt nicht in der Projektliste


ASIAIR = {"IMAGETYP": "Light", "EXPTIME": 180.0, "GAIN": 120, "OFFSET": 30, "BAYERPAT": "RGGB", "XBINNING": 1,
          "INSTRUME": "ZWO ASI294MC Pro", "CCD-TEMP": -10.0, "FOCALLEN": 2800.0, "XPIXSZ": 4.63, "RA": 85.25, "DEC": -2.46}
IC434_DIRS = ["Dual", "RGB/25.-27.12.2024", "RGB/29.12.2024", "RGB/04.03.2024", "RGB/30.12.2024", "RGB/31.12.2024",
              "RGB/30.01.2024", "RGB/10.01.2024", "RGB/25.02.2024"]


def build_ic434(root, n=2):
    base = os.path.join(root, "Pferdekopfnebel IC 434_17 Std")
    k = 0
    for d in IC434_DIRS:
        flt = "Dual" if d == "Dual" else "IR"
        for i in range(n):
            day = 10 + k % 18
            make_fits(os.path.join(base, "Lights", d, "Light_IC434_180.0s_Bin1_294MC_%s_gain120_202401%02d-21%02d46_%04d.fit"
                                   % (flt, day, i, k)),
                      dict(ASIAIR, FILTER=flt, **{"DATE-OBS": "2024-01-%02dT20:%02d:00" % (day, i)}), data=star_field(k))
            k += 1
    return base


def test_asiair_without_object_is_one_target(tmp_path, monkeypatch):
    """Bericht 0.8 §5: Datumsordner (25.-27.12.2024) und Filterordner (Dual) sind keine Objekte."""
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    base = build_ic434(str(tmp_path / "nas"))
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    pv = project.preview_targets([base], s)
    assert [t["name"] for t in pv["targets"]] == ["IC434"], [t["name"] for t in pv["targets"]]
    assert pv["targets"][0]["frames"] == 2 * len(IC434_DIRS)


def test_merged_project_accepts_calibration_only_folder(tmp_path, monkeypatch):
    """Bericht 0.8 §10: zusammengelegtes Projekt + reiner Bias-Ordner → Neu analysieren ohne „mehrere Ziele“."""
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    root = str(tmp_path / "nas")
    a, b = os.path.join(root, "Teil A"), os.path.join(root, "Teil B")
    for folder, name in ((a, "Pferdekopf"), (b, "Horsehead")):     # zwei Schreibweisen desselben Objekts
        for i in range(3):
            make_fits(os.path.join(folder, "Lights", "Light_%s_180.0s_%04d.fit" % (name, i)),
                      dict(ASIAIR, FILTER="IR", **{"DATE-OBS": "2024-12-%02dT21:00:00" % (20 + i)}), data=star_field(i))
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    pv = project.preview_targets([a, b], s)
    keys = [t["key"] for t in pv["targets"]]
    assert len(keys) == 2
    res = project.run_report([a, b], None, s, log=lambda *x: None, assign={k: "IC 434" for k in keys})
    pdir = project.pdir_of(s, res[0]["project"])
    assert {o.lower() for o in project.meta(pdir)["objects"]} >= {"pferdekopf", "horsehead"}
    hb = dict(ASIAIR, IMAGETYP="Bias Frame", EXPTIME=0.0001)
    bias = os.path.join(root, "Bias, Darks, Flats", "Bias")
    for i in range(5):
        make_fits(os.path.join(bias, "Bias_%04d.fit" % i), hb, shape=(256, 384), value=500)
    project.set_sources(pdir, project.sources(pdir)["roots"] + [bias])
    m = project.meta(pdir)
    res2 = project.run_report(project.sources(pdir)["roots"], m["target"], s, log=lambda *x: None, into=m["target"])
    assert res2 and res2[0]["frames"] == 6
    assert project.load_plan(pdir)["runs"][0]["bias"], "Bias aus dem neuen Ordner übernommen"


def _lock_once(monkeypatch, then_fail=False):
    """Erster Versuch: „an Hotpixeln ausgerichtet“ (wie NGC 3486, Bericht 2 §13), danach normal."""
    from astrostack import wbpplog
    calls = []

    def fake_check(outdir, info):
        calls.append(outdir)
        if len(calls) == 1:
            if then_fail:
                monkeypatch.setenv("FAKEPI_FAIL_REGISTRATION", "1")
            return dict(frames=3, reference=None, log=None,
                        bad=[dict(file="L_%d.xisf" % i, why=["0,0 px"], lock=True) for i in range(2)])
        return dict(frames=3, reference=None, log=None, bad=[])
    monkeypatch.setattr(wbpplog, "check_registration", fake_check)
    monkeypatch.setattr(pi, "_cosmetic", lambda plan, run, *a, **k: run)      # Hotpixel-Weg hier nicht Thema
    return calls


def test_lock_retry_uses_valid_parameters_and_new_master(tmp_path, monkeypatch):
    """Bericht 2 §13: Wiederholung mit hotPixelFilterRadius ≤ 2, Ergebnis stammt aus dem zweiten Versuch."""
    build(str(tmp_path / "nas"))
    s, pdir = analyse(tmp_path, monkeypatch, str(tmp_path / "nas"))
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    calls = _lock_once(monkeypatch)
    lines = []
    st, got, cmd, jobdir = stack(s, plan, monkeypatch, lines)
    assert st.state == "done", st.error + "\n" + "\n".join(lines)
    assert len(calls) == 2, "nach dem zweiten Versuch erneut geprüft"
    assert "hotPixelFilterRadius=2" in cmd and "hotPixelFilterRadius=3" not in cmd
    out = os.path.join(jobdir, "RGB")
    assert os.path.isdir(out + ".versuch-1"), "erster Versuch beiseitegelegt"
    assert st.runs["RGB"]["master"].startswith(out + os.sep)
    assert any("Hotpixeln" in n for n in st.runs["RGB"]["notes"])


def test_failed_retry_never_ships_old_master(tmp_path, monkeypatch):
    """Bericht 2 §13 (kritisch): scheitert die Wiederholung, endet der Lauf als fehlgeschlagen – ohne Final."""
    build(str(tmp_path / "nas"))
    s, pdir = analyse(tmp_path, monkeypatch, str(tmp_path / "nas"))
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    _lock_once(monkeypatch, then_fail=True)
    st, got, cmd, jobdir = stack(s, plan, monkeypatch)
    assert st.state == "failed" and not st.finals, (st.state, st.finals)
    assert st.failed_step == "register" or "Ausrichtung" in st.error, st.error
    assert any("kein" in c["text"].lower() for c in st.causes), st.causes
    final = os.path.join(pdir, "final")
    assert not os.path.isdir(final) or not os.listdir(final)
    old = os.path.join(jobdir, "RGB.versuch-1", "master")
    assert all(f.endswith(".verworfen") for f in os.listdir(old)), os.listdir(old)


def test_wbpp_parameters_clamped_before_start():
    p, notes = pi.check_wbpp_params({"hotPixelFilterRadius": "3", "maxStarDistortion": "0.45", "structureLayers": "x"})
    assert p["hotPixelFilterRadius"] == "2" and p["maxStarDistortion"] == "0.45" and "structureLayers" not in p
    assert len(notes) == 2


def test_wbpp_diagnostics_are_hints_and_calibration_counts_lights(tmp_path, monkeypatch):
    """Bericht 2 §11/§12: LongPaths-Vorabdiagnose ist kein Alarm; „Kalibrieren“ zählt Lights, nicht Flats."""
    build(str(tmp_path / "nas"))
    s, pdir = analyse(tmp_path, monkeypatch, str(tmp_path / "nas"))
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    monkeypatch.setattr(pi, "ProcRunner", None, raising=False)
    seen = []
    orig = pi._set_step

    class NoLong(ProcRunner):
        def __init__(self):
            super().__init__(long_paths=False)
    monkeypatch.setattr(pi.staging, "long_paths_enabled", lambda: False)
    monkeypatch.setattr(pi, "estimate_max_path", lambda *a: 175)      # Temp-Pfade auf Windows-Runnern sind lang
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    lines = []
    st = pi.run_plan(plan, s, log=lines.append, runner_factory=NoLong,
                     on_status=lambda x: seen.append((x.step, x.done, x.total, list(x.substeps))))
    assert st.state == "done", st.error
    assert not st.alert, st.alert
    assert any("lange Pfade" in h and "unkritisch" in h for h in st.hints), st.hints
    assert not any(l.lstrip().startswith("⚠") for l in lines)
    cal = [x for x in seen if x[0] == "calibrate"]
    assert all(d <= t == 3 for _k, d, t, _s in cal), cal
    subs = [s for x in cal for s in x[3]]
    assert {"master_bias", "cal_flat", "cal_light"} <= {s["key"] for s in subs} or not subs


def test_calibration_substeps_from_wbpp_log():
    from astrostack import wbpplog
    t = wbpplog.CalibTracker(n_lights=28, n_flats=60)
    t.feed("[x] * Begin integration of Bias frames")
    t.feed("[x] * Writing master Bias frame")
    for i in range(60):
        t.feed("Calibration frame %d: G:/w/_grp/flats_652791f451/Flat_%04d.fit ---> G:/w/IR/calibrated/Flat_BIN-1_CFA/Flat_%04d_c.xisf" % (i, i, i))
    t.feed("[x] * Begin integration of Flat frames")
    for i in range(5):
        t.feed("Calibration frame %d: //JUPITER/disk2/L_%04d.fit ---> G:/w/IR/calibrated/Light_BIN-1_CFA/L_%04d_c.xisf" % (i, i, i))
    subs = {s["key"]: s for s in t.as_list()}
    assert subs["master_bias"]["state"] == "done"
    assert subs["cal_flat"]["done"] == 60 and subs["cal_flat"]["state"] == "done"
    assert subs["master_flat"]["state"] == "done"            # vom nächsten Unterschritt abgelöst
    assert subs["cal_light"]["done"] == 5 and t.lights_done == 5 and subs["cal_light"]["state"] == "now"
    assert "unkritisch" in wbpplog.hint_text("** Warning: Diagnostics: Cannot create files with a path longer than 256 characters", 175)


def test_too_long_paths_stop_before_pixinsight(tmp_path, monkeypatch):
    """Bericht 2 §11: wirklich zu lange Pfade ohne LongPaths → klare Meldung vor dem Start, PixInsight startet nicht."""
    build(str(tmp_path / "nas"))
    s, pdir = analyse(tmp_path, monkeypatch, str(tmp_path / "nas"))
    plan = select_only(pdir, lambda p: NIGHTS[0][0] in p)
    monkeypatch.setattr(pi.staging, "long_paths_enabled", lambda: False)
    monkeypatch.setattr(pi, "estimate_max_path", lambda *a: 300)
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    ProcRunner.last = None
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=ProcRunner)
    assert st.state == "failed" and "300" in st.error and "Arbeitsverzeichnis" in st.error
    assert ProcRunner.last is None


def test_path_estimate_matches_report():
    out = "C:\\Users\\Martin\\AstroStack\\projects\\NGC3486\\runs\\20261103-184152\\IR"
    est = pi.estimate_max_path(out, ["\\\\JUPITER\\disk2\\Flat_35.0ms_Bin1_294MC_IR_gain120_20260215-220434_0009.fit"])
    assert 170 <= est <= 215, est          # Lights bekommen längere Gruppenordner als die Flats im Bericht
