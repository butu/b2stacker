"""Flats/Bias/Darks im Plan selbst wählen: Kandidat, keine, eigener Ordner, zurück zum Vorschlag – bleibt nach Neu-Analyse."""
from astrostack import project
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_calibcheck import _flat_header, synth_flat
from .test_e2e import star_field


def _data(r):
    for i in range(3):
        make_fits(f"{r}/vdB 15/2026-04-08/RGB/L_{i}.fits",
                  dict(NINA_LIGHT, FILTER="RGB", **{"DATE-OBS": "2026-04-08T21:%02d:00" % i}), data=star_field(i))
    for i in range(20):   # Panel-Serie
        make_fits(f"{r}/FLAT/FLAT_2026-04-14/RGB/p_{i}.fits",
                  _flat_header(FILTER="RGB", EXPOSURE=0.062, EXPTIME=0.062, **{"DATE-OBS": "2026-04-14T15:20:%02d" % i}),
                  data=synth_flat(h=256, w=384))
    for i in range(8):    # zweite, kleinere Serie
        make_fits(f"{r}/FLAT/FLAT_2026-04-13/RGB/q_{i}.fits",
                  _flat_header(FILTER="RGB", **{"DATE-OBS": "2026-04-13T15:20:%02d" % i}), data=synth_flat(h=256, w=384))
    # Bias außerhalb der Quellordner, NINA-Snapshot (IMAGETYP=LIGHT)
    hb = dict(NINA_LIGHT, EXPOSURE=0.0001, EXPTIME=0.0001)
    hb.pop("OBJECT")
    for i in range(6):
        make_fits(f"{r}/../anderswo/MeineBias/b_{i}.fits", hb, shape=(256, 384), value=500)


def test_choose_calibration(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r = str(tmp_path / "data")
    _data(r)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    res = project.run_report([r], None, s, log=lambda *a: None)
    pdir = project.pdir_of(s, res[0]["project"])
    run = project.load_plan(pdir)["runs"][0]
    assert len(run["flats"]) == 20 and not run["bias"]
    other = next(c for c in run["calib_choices"]["flats"] if c["frames"] == 8)
    # 1) anderen Flat-Satz wählen
    plan = project.save_edits(pdir, {"runs": {"RGB": {"calib": {"flats": other["id"]}}}})
    assert len(plan["runs"][0]["flats"]) == 8 and plan["runs"][0]["calib_manual"]["flats"] == "gewählt"
    # 2) eigener Bias-Ordner (Header sagt LIGHT) – Prüfung erkennt Bias, Einstellungen passen
    bias_dir = str(tmp_path / "anderswo" / "MeineBias")
    info = project.inspect_calib_folder(bias_dir, "bias", plan["runs"][0])
    assert info["frames"] == 6 and info["ok"] and info["types"].get("bias") == 6
    plan = project.save_edits(pdir, {"runs": {"RGB": {"calib": {"bias": {"folder": bias_dir}}}}})
    assert len(plan["runs"][0]["bias"]) == 6 and plan["runs"][0]["bias_set"] == bias_dir
    # 3) Neu-Analyse: Auswahl bleibt
    project.run_report([r], None, s, log=lambda *a: None)
    plan = project.apply_edits(pdir)
    assert len(plan["runs"][0]["flats"]) == 8 and len(plan["runs"][0]["bias"]) == 6
    # 4) Flats wieder auf Vorschlag, Bias auf keine
    plan = project.save_edits(pdir, {"runs": {"RGB": {"calib": {"flats": "auto", "bias": "none"}}}})
    assert len(plan["runs"][0]["flats"]) == 20 and plan["runs"][0]["bias"] == []
    assert "flats" not in plan["runs"][0].get("calib_manual", {})


def test_inspect_detects_wrong_gain(tmp_path):
    d = tmp_path / "falsch"
    for i in range(3):
        make_fits(str(d / f"b{i}.fits"), dict(NINA_LIGHT, IMAGETYP="BIAS", GAIN=0, EXPOSURE=0.0001), shape=(64, 96))
    run = {"settings_key": ["atr2600c", 100, 50, "high conversion gain", 1, 96, 64], "filter": "RGB"}
    info = project.inspect_calib_folder(str(d), "bias", run)
    assert not info["ok"] and any("Gain" in p for p in info["problems"])


def test_second_analysis_keeps_old_sources(tmp_path, monkeypatch):
    """Dasselbe Objekt aus einem weiteren Ordner analysieren: bisherige Ordner/Nächte bleiben im Projekt."""
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    a, b = str(tmp_path / "A"), str(tmp_path / "B")
    for root, night in ((a, "2026-04-08"), (b, "2026-05-02")):
        for i in range(2):
            make_fits(f"{root}/vdB 15/{night}/RGB/L_{i}.fits",
                      dict(NINA_LIGHT, OBJECT="vdB 15", FILTER="RGB", **{"DATE-OBS": "%sT21:%02d:00" % (night, i)}),
                      data=star_field(i))
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    project.run_report([a], None, s, log=lambda *x: None)
    project.run_report([b], None, s, log=lambda *x: None)
    pdir = project.pdir_of(s, "vdB 15")
    assert len(project.sources(pdir)["roots"]) == 2
    assert project.load_plan(pdir)["runs"][0]["lightCount"] == 4
