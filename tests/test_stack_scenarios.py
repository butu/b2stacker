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
