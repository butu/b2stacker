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
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0,
              **{"DATE-OBS": "2026-10-31T17:00:00", "DATE-LOC": "2026-10-31T18:00:00"})
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
    assert flow.decide(db, pid, [fid], "skip", "Wolken") == 1
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


def test_assess_marks_rejected_folder_and_keeps_manual(env):
    ws, db, data = env
    blurred = np.clip(_stars(3).astype(float) * 0 + 1200 + np.random.default_rng(3).normal(0, 15, (128, 192)),
                      0, 65535).astype(np.uint16)                       # keine Sterne → Wolken
    make_fits(os.path.join(data, "vdB 131", "2026-10-12", "HaO3", "L_0003.fits"),
              dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:40:00"}), data=blurred)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    res = flow.run_project(db, ws, pid, workers=1)["assess"]
    st = {os.path.basename(r["path"]): (r["status"], r["origin"], r["reason"]) for r in db.query(
        "SELECT f.path, s.status, s.origin, s.reason FROM file_status s JOIN files f ON f.id = s.file_id")}
    assert st["L_9999.fits"][0] == "skip" and "Ordner" in st["L_9999.fits"][2]
    assert st["L_0003.fits"][0] == "skip" and "Wolken" in st["L_0003.fits"][2]
    assert st["L_0000.fits"][:2] == ("use", "auto")
    assert res["skip"] == 2 and res["use"] == 3 and res["nights"] and res["filters"] == ["HaO3"]
    fid = db.value("SELECT id FROM files WHERE path LIKE '%L_0003.fits'")
    assert flow.decide(db, pid, [fid], "use") == 1
    flow.run_project(db, ws, pid, workers=1)
    assert db.one("SELECT status, origin FROM file_status WHERE file_id = ?", (fid,))[:] == ("use", "manual")
    assert flow.decide(db, pid, [fid], "auto") == 1
    flow.assess_project(db, pid)
    assert db.one("SELECT status, origin FROM file_status WHERE file_id = ?", (fid,))[:] == ("skip", "auto")
    with pytest.raises(ValueError):
        flow.decide(db, pid, [fid], "löschen")
    assert flow.decide(db, pid, [999999], "skip") == 0


def test_duplicate_content_is_skipped(env):
    ws, db, data = env
    src = os.path.join(data, "vdB 131", "2026-10-12", "HaO3", "L_0001.fits")
    shutil.copy(src, os.path.join(data, "vdB 131", "2026-10-12", "HaO3", "Kopie von L_0001.fits"))
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    reasons = [r[0] for r in db.query("SELECT reason FROM file_status WHERE status = 'skip'")]
    assert any("Duplikat" in r for r in reasons)


def _calib_tree(data):
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0,
              **{"DATE-OBS": "2026-10-31T17:00:00", "DATE-LOC": "2026-10-31T18:00:00"})
    hf.pop("OBJECT")
    for i in range(2, 5):                                          # jetzt 5 Flats am 31.10.
        make_fits(os.path.join(data, "FLAT", "2026-10-31", "F_%04d.fits" % i), hf, shape=(128, 192), value=30000)
    for i in range(3):                                             # Flats derselben Nacht, aber anderer Gain
        make_fits(os.path.join(data, "FLAT", "2026-10-12", "F_%04d.fits" % i),
                  dict(hf, GAIN=0, **{"DATE-OBS": "2026-10-12T17:00:00", "DATE-LOC": "2026-10-12T18:00:00"}),
                  shape=(128, 192), value=30000)
    hb = dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0001, EXPTIME=0.0001)
    for i in range(1, 4):
        make_fits(os.path.join(data, "Bias", "B_%04d.fits" % i), hb, shape=(128, 192), value=500)
    rgb = dict(NINA_LIGHT, FILTER="RGB", **{"DATE-OBS": "2026-07-20T21:00:00", "DATE-LOC": "2026-07-20T23:00:00"})
    for i in range(3):
        make_fits(os.path.join(data, "vdB 131", "2026-07-20", "RGB", "R_%04d.fits" % i), rgb, data=_stars(20 + i))


def test_calibration_matching_by_header(env):
    from astrostack.flow import calib
    ws, db, data = env
    _calib_tree(data)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    runs = {r["filter"]: r for r in calib.match_project(db, pid)}
    assert set(runs) == {"HaO3", "RGB"}
    ha = runs["HaO3"]
    assert ha["lights"] == 3 and ha["exposure_s"] == 1800
    fl = ha["nights"][0]["flats"]
    assert len(fl["file_ids"]) == 5 and "31.10." in fl["reason"] and "19 Tage" in fl["reason"]
    assert fl["quality"]["verdict"] in ("good", "warning", "critical")
    assert len(ha["bias"]["file_ids"]) == 4 and "Bias" in ha["bias"]["reason"]
    assert ha["darks"]["file_ids"] == [] and "ohne Darks" in ha["darks"]["reason"]
    assert ha["problems"] == []
    rgb = runs["RGB"]                                               # RGB-Lights nie mit HaO3-Flats
    assert rgb["nights"][0]["flats"]["file_ids"] == []
    assert "keine Flats mit Filter RGB" in rgb["nights"][0]["flats"]["reason"]
    assert rgb["problems"] and "20.07." in rgb["problems"][0]


def test_calibration_too_few_and_old(env):
    from astrostack.flow import calib
    ws, db, data = env
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    run = calib.match_project(db, pid)[0]
    assert run["nights"][0]["flats"]["file_ids"] == [] and "zu wenige" in run["nights"][0]["flats"]["reason"]
    assert run["bias"]["file_ids"] == [] and "nur 1 Bias" in run["bias"]["reason"]
    assert len(run["problems"]) == 2
    old = calib.FLAT_WARN_DAYS
    try:                                                             # weit entfernte Flats: kein Ausschluss, kein Befund
        calib.FLAT_WARN_DAYS = 5
        fl = calib.match_project(db, pid)[0]["nights"][0]["flats"]
        assert fl.get("days", 0) > 5 or "zu wenige" in fl["reason"]
        if fl.get("days", 0) > 5:
            assert fl["far"] and not any("entfernt" in (h.get("title") or "") for h in fl["hints"])
    finally:
        calib.FLAT_WARN_DAYS = old


def test_plan_settings_reasons_and_edits(env):
    from astrostack.flow import plan as fp
    ws, db, data = env
    _calib_tree(data)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    p = fp.update_plan(db, pid, ws.root)
    runs = {r["filter"]: r for r in p["runs"]}
    ha, rgb = runs["HaO3"], runs["RGB"]
    assert p["runs"][0]["filter"] == "RGB"                          # Breitband zuerst (Referenz für alle)
    assert ha["mode"] == "extract" and ha["stack_mode"] == "separated" and ha["outputs"] == ["Ha", "OIII"]
    wv = fp.wbpp_values(ha)
    assert wv["debayerOutputMethod"] == 1 and wv["recombineRGB"] is False
    assert wv["debayerActiveChannelR"] and wv["debayerActiveChannelG"] and wv["debayerActiveChannelB"]
    assert fp.channels(dict(channel_map={"Ha": "R"})) == {"R"}
    assert fp.wbpp_values(rgb)["debayerOutputMethod"] == 0
    assert ha["enabled"] and ha["why_off"] is None and ha["reference_id"] in ha["light_ids"]
    assert rgb["enabled"] is False and "keine Flats" in rgb["why_off"]
    assert ha["align_to"] is None                                   # RGB ist aus → HaO3 ist erster aktiver Lauf
    s = {x["key"]: x for x in ha["settings"]}
    assert s["fast_integration"]["value"] is False and "200" in s["fast_integration"]["why"]
    assert s["rejection"]["value"] == "percentile" and "3 Frames" in s["rejection"]["why"]
    assert s["local_normalization"]["value"] is False and "eine Nacht" in s["local_normalization"]["why"]
    assert all(x["why"] for x in ha["settings"])
    assert fp.wbpp_values(ha)["autoIntegrationMode"] is False and fp.wbpp_values(ha)["rejection_4"] == 0
    assert fp.load_plan(db, pid)["runs"][0]["id"] == p["runs"][0]["id"]
    # Änderungen überleben eine Neuberechnung
    p2 = fp.edit_plan(db, pid, ws.root, options={"local_normalization": True},
                      runs={rgb["id"]: {"enabled": True}})
    r2 = {r["filter"]: r for r in p2["runs"]}
    ln = next(x for x in r2["HaO3"]["settings"] if x["key"] == "local_normalization")
    assert ln["value"] is True and ln["edited"] and ln["auto"] is False
    assert r2["RGB"]["enabled"] is True
    flow.run_project(db, ws, pid, workers=1)
    p3 = fp.update_plan(db, pid, ws.root)
    assert {r["filter"]: r for r in p3["runs"]}["RGB"]["enabled"] is True
    p4 = fp.edit_plan(db, pid, ws.root, options={"local_normalization": None}, runs={rgb["id"]: {"enabled": None}})
    assert {r["filter"]: r for r in p4["runs"]}["RGB"]["enabled"] is False
    with pytest.raises(ValueError):
        fp.edit_plan(db, pid, ws.root, options={"turbo": True})
    assert fp.explain(p4)[0].startswith("RGB: 3 Lights")


def test_rejection_rules():
    from astrostack.flow import plan as fp
    assert fp.rejection_for(5)[0] == "percentile"
    assert fp.rejection_for(7)[0] == "winsorized" and fp.rejection_for(14)[0] == "winsorized"
    assert fp.rejection_for(15)[0] == "esd" and fp.rejection_for(400)[0] == "esd"


def test_plan_top_percent_reference_label_and_manual_calibration(env):
    from astrostack.flow import plan as fp
    ws, db, data = env
    _calib_tree(data)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    p = fp.update_plan(db, pid, ws.root)
    ha = next(r for r in p["runs"] if r["filter"] == "HaO3")
    assert ha["lights_all"] == ha["lights"] == 3 and len(ha["rank_exp"]) == 3 and ha["top_percent"] == 100
    assert ha["reference_choices"] and ha["reference_choices"][0]["id"] == ha["reference_auto"]
    other = next(c["id"] for c in ha["reference_choices"] if c["id"] != ha["reference_auto"])
    assert fp.top_keep(100, 10) == 10 and fp.top_keep(10, 10) == 3 and fp.top_keep(2, 10) == 2
    p2 = fp.edit_plan(db, pid, ws.root, runs={ha["id"]: {"label": "Ha+OIII", "reference": other,
                                                         "flats": {"2026-10-12": "none"}, "bias": "none",
                                                         "star_detection": {"minStructureSize": 7}}})
    h2 = next(r for r in p2["runs"] if r["id"] == ha["id"])
    assert h2["name"] == "Ha+OIII" and h2["reference_id"] == other and h2["reference_edited"]
    assert h2["flats"][0]["file_ids"] == [] and h2["flats"][0]["manual"] and "ohne Flats" in h2["flats"][0]["reason"]
    assert h2["bias"]["file_ids"] == [] and "von Hand" in h2["bias"]["reason"]
    sd = next(s for s in h2["settings"] if s["key"] == "star_detection")
    assert sd["value"]["minStructureSize"] == 7 and sd["edited"] and sd["edited_keys"] == ["minStructureSize"]
    assert fp.wbpp_values(h2)["minStructureSize"] == 7
    opt = h2["flat_options"][0]["key"]
    p3 = fp.edit_plan(db, pid, ws.root, runs={ha["id"]: {"flats": {"2026-10-12": opt}, "bias": None,
                                                         "top_percent": 34}})
    h3 = next(r for r in p3["runs"] if r["id"] == ha["id"])
    assert h3["flats"][0]["file_ids"] and h3["flats"][0]["manual"] and h3["lights"] == 3   # mind. 3 Lights bleiben
    # Sternerkennung von Hand (wie aus der Oberfläche: Text) → als Zahl gespeichert; Unsinn wird abgelehnt
    p4 = fp.edit_plan(db, pid, ws.root, runs={ha["id"]: {"star_detection": {"maxStarDistortion": "0,52",
                                                                             "hotPixelFilterRadius": "2"}}})
    sd4 = next(s for s in next(r for r in p4["runs"] if r["id"] == ha["id"])["settings"] if s["key"] == "star_detection")
    assert sd4["value"]["maxStarDistortion"] == 0.52 and sd4["value"]["hotPixelFilterRadius"] == 2
    assert sd4["value"]["minStructureSize"] == 7                       # frühere Eingabe bleibt
    with pytest.raises(ValueError, match="hotPixelFilterRadius"):
        fp.edit_plan(db, pid, ws.root, runs={ha["id"]: {"star_detection": {"hotPixelFilterRadius": "zwei"}}})
    with pytest.raises(ValueError, match="unbekannt"):
        fp.edit_plan(db, pid, ws.root, runs={ha["id"]: {"star_detection": {"hotPixelRadius": 2}}})


def test_flat_distance_is_quiet_and_manual_choice_is_confirmed(env):
    """Feedback 0.13.1 („Ruhe, wenn alles passt“): Der Abstand zu den Flats ist nie ein Befund und muss nicht abgehakt
    werden – über 14 Tage nur ``far`` für eine leise Zeile. Von Hand gewählte Flats gelten als bestätigt: ihre
    Befunde erscheinen nur noch als Hinweis (info), nie als Warnung."""
    from astrostack.flow import calib
    from astrostack.flow import plan as fp
    ws, db, data = env
    _calib_tree(data)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    assert calib.FLAT_WARN_DAYS == 14
    p = fp.update_plan(db, pid, ws.root)
    far = [(r, f) for r in p["runs"] for f in r["flats"] if (f.get("days") or 0) > 14]
    assert far, "Testdaten brauchen eine Nacht mit weit entfernten Flats"
    for r in p["runs"]:
        for f in r["flats"]:
            assert f["far"] == ((f.get("days") or 0) > 14)
            assert not any(h.get("kind") == "distance" for h in f["hints"])
    r, f = far[0]
    opt = next(o for o in r["flat_options"] if not o.get("library"))
    p2 = fp.edit_plan(db, pid, ws.root, runs={r["id"]: {"flats": {f["night"]: opt["key"]}}})
    f2 = next(x for x in next(y for y in p2["runs"] if y["id"] == r["id"])["flats"] if x["night"] == f["night"])
    assert f2["manual"] and f2["verdict"] == "manual" and f2["file_ids"] == opt["ids"]
    assert all(x["level"] == "info" for x in f2["findings"]) and all(h["level"] == "info" for h in f2["hints"])
