"""Ziele: Schreibweisen vereinheitlichen, Vorschau vor dem Anlegen, Zusammenlegen, Überspringen, bestehende Projekte."""
import os

from astrostack import project
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_e2e import star_field


def L(r, obj, night, i, ra=None, dec=None, filt="RGB"):
    h = dict(NINA_LIGHT, OBJECT=obj, FILTER=filt, **{"DATE-OBS": "%sT21:%02d:00" % (night, i)})
    if ra is not None:
        h.update(RA=ra, DEC=dec)
    make_fits(f"{r}/{obj}/{night}/{filt}/L_{i}.fits", h, data=star_field(i))


def test_spellings_are_one_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r = str(tmp_path / "d")
    L(r, "Sh2-82", "2025-10-02", 0)
    L(r, "Sh 2-82", "2025-10-03", 1)
    L(r, "SH2_82", "2025-10-04", 2)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    res = project.run_report([r], None, s, log=lambda *a: None)
    assert len(res) == 1
    assert len(os.listdir(os.path.join(s.workdir, "projects"))) == 1
    assert project.load_plan(project.pdir_of(s, res[0]["project"]))["runs"][0]["lightCount"] == 3


def test_preview_near_and_assign(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r = str(tmp_path / "d")
    L(r, "IC 2118", "2025-12-01", 0, ra=76.9, dec=-7.2)
    L(r, "Hexenkopf", "2025-12-02", 1, ra=76.95, dec=-7.25)       # anderes Wort, gleiches Feld
    L(r, "M 31", "2025-09-01", 2, ra=10.68, dec=41.27)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    pv = project.preview_targets([r], s)
    keys = {t["key"] for t in pv["targets"]}
    assert keys == {"ic2118", "hexenkopf", "m31"}
    assert pv["near"] and {pv["near"][0]["a"], pv["near"][0]["b"]} == {"ic2118", "hexenkopf"}
    # Benutzer: IC 2118 + Hexenkopf zusammen als „Hexenkopfnebel“, M 31 nicht anlegen
    res = project.run_report([r], None, s, log=lambda *a: None,
                             assign={"ic2118": "Hexenkopfnebel", "hexenkopf": "Hexenkopfnebel", "m31": None})
    assert [x["target"] for x in res] == ["Hexenkopfnebel"]
    pdir = project.pdir_of(s, "Hexenkopfnebel")
    assert project.load_plan(pdir)["runs"][0]["lightCount"] == 2
    assert not os.path.exists(os.path.join(s.workdir, "projects", "M 31"))
    # später neue Nacht mit wieder anderer Schreibweise → landet automatisch im bestehenden Projekt?
    pv2 = project.preview_targets([r], s)
    ic = next(t for t in pv2["targets"] if t["key"] == "ic2118")
    assert ic["existing"] is None          # Projekt heißt anders → Benutzer wählt es in der Rückfrage


def test_existing_project_is_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r1, r2 = str(tmp_path / "a"), str(tmp_path / "b")
    L(r1, "vdB 131", "2026-10-12", 0)
    L(r2, "vdB131", "2026-10-30", 1)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    project.run_report([r1], None, s, log=lambda *a: None)
    pv = project.preview_targets([r2], s)
    assert pv["targets"][0]["existing"]["name"] == "vdB 131"
    project.run_report([r2], None, s, log=lambda *a: None)
    assert os.listdir(os.path.join(s.workdir, "projects")) == ["vdB 131"]
    assert project.load_plan(project.pdir_of(s, "vdB 131"))["runs"][0]["lightCount"] == 2


def test_title_for_single_target(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r = str(tmp_path / "d")
    L(r, "NGC 7000", "2025-08-01", 0)
    s = Settings(workdir=str(tmp_path / "w"), analysis_workers=1)
    res = project.run_report([r], None, s, log=lambda *a: None, title="Nordamerikanebel")
    assert res[0]["target"] == "Nordamerikanebel"
