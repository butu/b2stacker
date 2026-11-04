"""Neubau: Arbeitsverzeichnis, Projekte und automatischer Ablauf (Schritte 1–3) auf der Datenbank."""
import json
import os
import shutil

import numpy as np
import pytest

from astrostack import flow, library
from astrostack.flow import discover as disc
from astrostack.workspace import Workspace, folder_name

from .conftest import NINA_LIGHT, make_fits


def _stars(seed, shape=(128, 192)):
    rng, srng = np.random.default_rng(seed), np.random.default_rng(1)
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    img = np.full(shape, 1200.0)
    for _ in range(40):
        cy, cx = srng.uniform(8, shape[0] - 8), srng.uniform(8, shape[1] - 8)
        img += srng.uniform(2000, 20000) * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * 1.3 ** 2))
    return np.clip(img + rng.normal(0, 15, shape), 0, 65535).astype(np.uint16)


@pytest.fixture
def env(tmp_path):
    ws = Workspace(str(tmp_path / "work")).ensure()
    db = ws.open_db()
    data = tmp_path / "Rohdaten, NAS"
    for i in range(3):
        make_fits(str(data / "vdB 131" / "2026-10-12" / "HaO3" / ("L_%04d.fits" % i)),
                  dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)}), data=_stars(i))
    make_fits(str(data / "vdB 131" / "_rejected" / "L_9999.fits"), NINA_LIGHT, data=_stars(9))
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(2):
        make_fits(str(data / "FLAT" / "2026-10-31" / ("F_%04d.fits" % i)), hf, shape=(128, 192), value=30000)
    hb = dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0001, EXPTIME=0.0001)
    make_fits(str(data / "Bias" / "B_0000.fits"), hb, shape=(128, 192), value=500)
    (data / "notizen.txt").write_text("kein Bild")
    yield ws, db, str(data)
    db.close()


def test_workspace_layout_and_names(tmp_path):
    ws = Workspace(str(tmp_path / "w")).ensure()
    for d in ("projects", "temp", "system", os.path.join("system", "backup")):
        assert os.path.isdir(os.path.join(ws.root, d))
    assert folder_name('M 31: Andromeda / "neu"?') == "M 31_ Andromeda _ _neu__"
    assert folder_name("con") == "_con" and folder_name("   ") == "Projekt" and folder_name("x.") == "x"
    os.makedirs(ws.project_dir("M 31"))
    assert ws.unique_folder("m 31") == "m 31 (2)"
    open(os.path.join(ws.temp, "rest.xisf"), "w").close()
    os.makedirs(os.path.join(ws.temp, "run", "sub"))
    assert ws.clear_temp() == 2 and os.listdir(ws.temp) == []


def test_projects_create_rename_folders(env):
    ws, db, data = env
    pid = library.create_project(db, ws, "vdB 131", "Reflexionsnebel", [data, data + os.sep])
    assert os.path.isdir(ws.project_dir("vdB 131"))
    assert db.value("SELECT COUNT(*) FROM source_folders WHERE project_id = ?", (pid,)) == 1   # doppelt = einmal
    assert db.value("SELECT COUNT(*) FROM fields WHERE project_id = ?", (pid,)) == 1
    with pytest.raises(library.LibraryError, match="gibt es schon"):
        library.create_project(db, ws, "VDB 131")
    open(os.path.join(ws.project_dir("vdB 131"), "final_final.tif"), "w").close()
    folder = library.rename_project(db, ws, pid, "vdB 131 – Iris?")
    assert folder == "vdB 131 – Iris_" and os.path.exists(os.path.join(ws.project_dir(folder), "final_final.tif"))
    assert library.find_project(db, "VDB 131 – IRIS?") == pid and library.find_project(db, str(pid)) == pid
    kinds = [r[0] for r in db.query("SELECT kind FROM events WHERE project_id = ? ORDER BY id", (pid,))]
    assert kinds == ["folder_added", "project_created", "project_renamed"]
    with pytest.raises(library.LibraryError):
        library.find_project(db, "gibt es nicht")


def test_flow_discovers_reads_and_measures(env):
    ws, db, data = env
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    steps = []
    res = flow.run_project(db, ws, pid, progress=lambda **kw: steps.append(kw.get("step")), workers=1)
    assert res["discover"]["new"] == 7 and res["discover"]["offline"] == 0          # .txt zählt nicht
    assert res["headers"] == {"read": 7, "errors": 0}
    assert res["measure"]["measured"] == 7 and res["measure"]["errors"] == 0
    assert {"Neue Dateien suchen", "Header lesen", "Frames vermessen"} <= set(steps)
    types = dict(db.query("SELECT type, COUNT(*) FROM files GROUP BY type"))
    assert types == {"light": 4, "flat": 2, "bias": 1}
    assert db.value("SELECT excluded_dir FROM files WHERE path LIKE ?", ("%_rejected%",)) == 1
    lights = db.query("SELECT f.checksum, m.nstars, m.fwhm, m.thumb FROM files f JOIN frame_metrics m "
                      "ON m.file_id = f.id WHERE f.type = 'light'")
    assert all(len(r["checksum"]) == 64 and r["nstars"] > 10 and r["fwhm"] and r["thumb"] for r in lights)
    assert os.path.exists(os.path.join(ws.previews, "thumbs", lights[0]["thumb"]))
    flat = json.loads(db.value("SELECT m.data_json FROM files f JOIN frame_metrics m ON m.file_id = f.id "
                               "WHERE f.type = 'flat' LIMIT 1"))
    assert flat["brightest"] > 20000
    assert db.value("SELECT COUNT(*) FROM setups") == 1
    meta = json.loads(db.value("SELECT meta_json FROM files WHERE type = 'light' LIMIT 1"))
    assert meta["filter"] == "HaO3" and meta["object"] == "vdB 131"
    # zweiter Durchlauf: nichts zu tun
    again = flow.run_project(db, ws, pid, workers=1)
    assert again["discover"]["new"] == 0 and again["headers"]["read"] == 0 and again["measure"]["measured"] == 0


def test_changed_missing_back_and_moved_files(env):
    ws, db, data = env
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    light = os.path.join(data, "vdB 131", "2026-10-12", "HaO3", "L_0000.fits")
    fid = db.value("SELECT id FROM files WHERE path = ?", (os.path.realpath(light),))
    with db.transaction():
        db.execute("INSERT INTO file_status VALUES (?, ?, 'skip', 'manual', 'Wolken', '2026-11-04T20:00:00Z')", (pid, fid))
    # geändert → Messung neu
    make_fits(light, dict(NINA_LIGHT, GAIN=101), data=_stars(5))
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["discover"]["changed"] == 1 and r["measure"]["measured"] == 1
    # verschoben → fehlt, wird über Prüfsumme wiedergefunden, Entscheidung bleibt
    moved = os.path.join(data, "sortiert", "L_0000.fits")
    os.makedirs(os.path.dirname(moved))
    shutil.move(light, moved)
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["discover"]["new"] == 1 and r["discover"]["missing"] == 1 and r["measure"]["moved"] == 1
    new_id = db.value("SELECT id FROM files WHERE path = ?", (os.path.realpath(moved),))
    assert db.one("SELECT reason FROM file_status WHERE file_id = ?", (new_id,))["reason"] == "Wolken"
    assert db.value("SELECT COUNT(*) FROM files WHERE id = ?", (fid,)) == 0
    # gelöscht → „Datei fehlt“, zurückgelegt → wieder da
    bias = os.path.join(data, "Bias", "B_0000.fits")
    keep = bias + ".bak"
    os.rename(bias, keep)
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["discover"]["missing"] == 1
    assert db.value("SELECT missing_since IS NOT NULL FROM files WHERE path = ?", (os.path.realpath(bias),)) == 1
    os.rename(keep, bias)
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["discover"]["back"] == 1


def test_offline_source_keeps_files(env, tmp_path):
    ws, db, data = env
    gone = str(tmp_path / "NAS-aus")
    pid = library.create_project(db, ws, "vdB 131", folders=[data, gone])
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["discover"]["offline"] == 1
    assert db.value("SELECT online FROM source_folders WHERE path = ?", (os.path.abspath(gone),)) == 0
    moved = str(tmp_path / "weg")
    os.rename(data, moved)                                   # ganze Quelle offline
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["discover"]["offline"] == 2 and r["discover"]["missing"] == 0
    assert db.value("SELECT COUNT(*) FROM files WHERE missing_since IS NOT NULL") == 0
    assert library.list_projects(db)[0]["offline"] == 2


def test_shared_flat_folder_is_read_once(env):
    ws, db, data = env
    a = library.create_project(db, ws, "A", folders=[data])
    b = library.create_project(db, ws, "B", folders=[os.path.join(data, "FLAT")])
    flow.run_project(db, ws, a, workers=1)
    r = flow.run_project(db, ws, b, workers=1)
    assert r["discover"]["new"] == 0 and r["headers"]["read"] == 0 and r["measure"]["measured"] == 0
    assert db.value("SELECT COUNT(*) FROM project_files WHERE project_id = ?", (b,)) == 2


def test_broken_file_is_reported_not_fatal(env):
    ws, db, data = env
    with open(os.path.join(data, "kaputt.fits"), "wb") as fh:
        fh.write(b"SIMPLE  = T" + b"\x00" * 50)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    r = flow.run_project(db, ws, pid, workers=1)
    assert r["headers"]["errors"] == 1 and r["measure"]["measured"] == 7
    assert db.value("SELECT header_error FROM files WHERE path LIKE '%kaputt.fits'")


def test_cancel_discovery_marks_nothing_missing(env):
    ws, db, data = env
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    fid = int(db.value("SELECT id FROM source_folders WHERE project_id = ?", (pid,)))
    os.remove(os.path.join(data, "Bias", "B_0000.fits"))
    r = disc.discover_folder(db, pid, fid, data, cancel=lambda: True)
    assert r["missing"] == 0


def test_parallel_measure(env):
    ws, db, data = env
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    r = flow.run_project(db, ws, pid, workers=2)
    assert r["measure"]["measured"] == 7
