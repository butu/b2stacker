"""Langlebigkeit: alte Projekte öffnen, umbenennen, beschreiben, verschieben, Historie mit relativen Pfaden."""
import json
import os

from astrostack import project
from astrostack.settings import Settings


def _old_project(base):
    """Projekt im Format von Version 0.1 (ohne project.json / stacks.json)."""
    pdir = os.path.join(base, "projects", "vdB 15")
    os.makedirs(os.path.join(pdir, "runs", "20261101-135541"))
    os.makedirs(os.path.join(pdir, "final"))
    final = os.path.join(pdir, "final", "vdB15_RGB.xisf")
    open(final, "wb").write(b"XISF0100")
    json.dump({"target": "vdB 15", "created": "2026-11-01T13:50:00", "total_frames": 55, "options": {},
               "runs": [{"name": "RGB", "lightCount": 55, "flats": ["f"], "bias": []}]},
              open(os.path.join(pdir, "plan.json"), "w"))
    json.dump({"roots": ["F:/NINA/vdB 15"], "target": "vdB 15"}, open(os.path.join(pdir, "sources.json"), "w"))
    json.dump({"state": "done", "finished": "2026-11-01T14:31:25",
               "runs": {"RGB": {"master": "x", "finals": [final], "minutes": 35.7}}},
              open(os.path.join(pdir, "runs", "20261101-135541", "status.json"), "w"))
    json.dump({"runs": [{"name": "RGB", "lightCount": 55, "flats": ["f"], "bias": [], "nights": ["2026-04-07"]}]},
              open(os.path.join(pdir, "runs", "20261101-135541", "plan.json"), "w"))
    return pdir


def test_old_project_opens_and_migrates(tmp_path):
    pdir = _old_project(str(tmp_path / "work"))
    s = Settings(workdir=str(tmp_path / "work"))
    lst = project.list_projects(s)
    assert lst[0]["title"] == "vdB 15" and lst[0]["stacks"] == 1
    m = project.meta(pdir)
    assert m["schema"] == project.SCHEMA and m["name"] == "vdB 15"
    st = project.stacks(pdir)
    assert st[0]["legacy"] and st[0]["lights"] == 55 and st[0]["outputs"][0]["exists"]
    # in stacks.json relativ gespeichert
    raw = json.load(open(os.path.join(pdir, "stacks.json")))
    assert raw[0]["outputs"][0]["file"] == "final/vdB15_RGB.xisf"


def test_rename_description_checklist(tmp_path):
    pdir = _old_project(str(tmp_path / "w"))
    project.save_meta(pdir, name="Kleiner Reflexionsnebel", description="RGB aus April", checklist={"crop": "2026-11-02"})
    m = project.meta(pdir)
    assert m["name"] == "Kleiner Reflexionsnebel" and m["target"] == "vdB 15" and m["checklist"]["crop"]
    import pytest
    with pytest.raises(ValueError):
        project.save_meta(pdir, name="  ")


def test_move_workdir_keeps_history(tmp_path):
    old, new = str(tmp_path / "C_AstroStack"), str(tmp_path / "G_b2stacking")
    _old_project(old)
    project.stacks(os.path.join(old, "projects", "vdB 15"))          # Historie anlegen
    assert project.foreign_projects(old, new) == ["vdB 15"]
    assert project.move_projects(old, new, ["vdB 15"]) == ["vdB 15"]
    pdir = os.path.join(new, "projects", "vdB 15")
    st = project.stacks(pdir)
    assert st[0]["outputs"][0]["exists"], "relative Pfade müssen nach dem Verschieben stimmen"
    assert project.latest_output(pdir, "RGB").startswith(new)


def test_sources_editable(tmp_path):
    pdir = _old_project(str(tmp_path / "w"))
    project.set_sources(pdir, ["F:/NINA/vdB 15", "F:/NINA/FLAT", "F:/NINA/Bias"])
    assert len(project.sources(pdir)["roots"]) == 3


def test_unknown_future_fields_are_kept(tmp_path):
    """Neuere Programmversion schreibt zusätzliche Felder – ältere/neuere Versionen dürfen sie nicht verlieren."""
    pdir = _old_project(str(tmp_path / "w"))
    m = project.meta(pdir)
    m["kuenftig"] = {"x": 1}
    json.dump(m, open(os.path.join(pdir, "project.json"), "w"))
    project.save_meta(pdir, description="neu")
    assert project.meta(pdir)["kuenftig"] == {"x": 1}


def test_check_pixinsight(tmp_path):
    from astrostack.app.server import check_pixinsight
    base = tmp_path / "PixInsight"
    (base / "bin").mkdir(parents=True)
    exe = base / "bin" / "PixInsight.exe"
    exe.write_bytes(b"")
    r = check_pixinsight(str(exe))
    assert r["exists"] and not r["wbpp"] and not r["ok"]
    wb = base / "src" / "scripts" / "BatchPreprocessing"
    wb.mkdir(parents=True)
    (wb / "WBPP.js").write_text("")
    assert check_pixinsight(str(exe))["ok"]
    r = check_pixinsight(str(base))                 # Ordner gewählt → exe wird gefunden
    assert r["ok"] and r["path"].endswith("PixInsight.exe")
    other = tmp_path / "notepad.exe"
    other.write_bytes(b"")
    assert not check_pixinsight(str(other))["ok"]
