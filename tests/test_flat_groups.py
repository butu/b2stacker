"""Kamera zwischen Nächten gedreht → je Nachtgruppe eigene Flats (WBPP Grouping Keyword über Verknüpfungen)."""
import os

from astrostack import project
from astrostack.engines import pixinsight as pi
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_calibcheck import _flat_header, synth_flat
from .test_e2e import star_field
from .test_engine import FakePI


def _data(r):
    for night, rot in (("2026-04-07", 273.5), ("2026-04-08", 273.6), ("2026-04-20", 93.4)):
        for i in range(3):
            make_fits(f"{r}/vdB 15/{night}/RGB/L_{night}_{i}.fits",
                      dict(NINA_LIGHT, OBJECT="vdB 15", FILTER="RGB", ROTATOR=rot, **{"DATE-OBS": "%sT21:%02d:00" % (night, i),
                                                                     "DATE-LOC": "%sT23:%02d:00" % (night, i)}),
                      data=star_field(i))
    for day, rot in (("2026-04-09", 273.5), ("2026-04-21", 93.4)):
        for i in range(12):
            make_fits(f"{r}/FLAT/FLAT_{day}/RGB/f_{i}.fits",
                      _flat_header(FILTER="RGB", ROTATOR=rot, **{"DATE-OBS": "%sT10:00:%02d" % (day, i),
                                                                 "DATE-LOC": "%sT12:00:%02d" % (day, i)}),
                      data=synth_flat(h=256, w=384))


def test_flat_groups_by_rotator(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r = str(tmp_path / "data")
    _data(r)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1, keep_awake=False, open_results=False, min_free_ram_gb=0.1)
    res = project.run_report([r], None, s, log=lambda *a: None)
    pdir = project.pdir_of(s, res[0]["project"])
    run = project.load_plan(pdir)["runs"][0]
    fg = run["flat_groups"]
    assert [g["nights"] for g in fg] == [["2026-04-07", "2026-04-08"], ["2026-04-20"]]
    assert "FLAT_2026-04-09" in fg[0]["flat_set"] and "FLAT_2026-04-21" in fg[1]["flat_set"]
    assert len(run["flats"]) == 24 and run["rotation"]["source"] == "rotator"
    # Stacken mit simuliertem PixInsight
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir
    for x in plan["runs"]:
        x["gb"] = 0
    FakePI.calls = []
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=FakePI)
    assert st.state == "done", st.error
    items = FakePI.calls[0][1]
    assert "groupingKeywordsEnabled=true" in items and "keywords=ASFLAT pre" in items
    l1 = [i for i in items if "/ASFLAT_1/L" in i.replace("\\\\", "/")]
    l2 = [i for i in items if "/ASFLAT_2/L" in i.replace("\\\\", "/")]
    f1 = [i for i in items if "/ASFLAT_1/F" in i.replace("\\\\", "/")]
    f2 = [i for i in items if "/ASFLAT_2/F" in i.replace("\\\\", "/")]
    assert l1 and l2 and f1 and f2
    # Verknüpfungen weg, Originale unversehrt
    assert not os.path.exists(os.path.join(pdir, "runs", st.started and os.listdir(os.path.join(pdir, "runs"))[0], "_grp"))
    assert len(os.listdir(f"{r}/vdB 15/2026-04-20/RGB")) == 3
    assert len(os.listdir(f"{r}/FLAT/FLAT_2026-04-21/RGB")) == 12


def test_manual_flat_choice_overrides_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    r = str(tmp_path / "data")
    _data(r)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    res = project.run_report([r], None, s, log=lambda *a: None)
    pdir = project.pdir_of(s, res[0]["project"])
    plan = project.save_edits(pdir, {"runs": {"RGB": {"calib": {"flats": {"folder": f"{r}/FLAT/FLAT_2026-04-09/RGB"}}}}})
    assert not plan["runs"][0].get("flat_groups") and len(plan["runs"][0]["flats"]) == 12
    plan = project.save_edits(pdir, {"runs": {"RGB": {"calib": {"flats": "auto"}}}})
    assert len(plan["runs"][0]["flat_groups"]) == 2


def test_remove_link_tree_never_deletes_targets(tmp_path):
    tgt = tmp_path / "orig"
    tgt.mkdir()
    (tgt / "a.fits").write_bytes(b"x")
    base = tmp_path / "_grp"
    pi.link_dir(str(tgt), str(base / "ASFLAT_1" / "L0"))
    assert (base / "ASFLAT_1" / "L0" / "a.fits").exists()
    pi.remove_link_tree(str(base))
    assert not base.exists() and (tgt / "a.fits").exists()


def test_unc_and_network_targets_are_remote():
    assert pi.is_remote_path(r"\\JUPITER\disk2\vdB 152\LIGHT")
    assert pi.is_remote_path("//JUPITER/disk2/LIGHT")


def test_group_links_fall_back_to_copies_when_links_fail(tmp_path, monkeypatch):
    # Netzlaufwerk ohne Symlink-Recht: keine kaputte Verknüpfung, sondern nur die gewählten Dateien als Kopie
    src = tmp_path / "share" / "LIGHT"
    src.mkdir(parents=True)
    for n in ("a.fits", "b.fits", "unused.fits"):
        (src / n).write_bytes(n.encode())
    monkeypatch.setattr(pi.os, "symlink", lambda *a, **k: (_ for _ in ()).throw(OSError("no symlink right")))
    monkeypatch.setattr(pi.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("cross device")))
    logs = []
    base = tmp_path / "_grp"
    lights, flats = pi.build_group_links([dict(key="1", lights=[str(src / "a.fits"), str(src / "b.fits")], flats=[])],
                                         str(base), log=logs.append)
    l0 = base / "ASFLAT_1" / "L0"
    assert sorted(os.listdir(l0)) == ["a.fits", "b.fits"] and not pi._is_link(str(l0))
    assert lights == [str(l0 / "a.fits"), str(l0 / "b.fits")] and logs
    pi.remove_link_tree(str(base))
    assert not base.exists() and sorted(os.listdir(src)) == ["a.fits", "b.fits", "unused.fits"]


def test_unreadable_network_file_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pi.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(pi.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("[WinError 123]")))
    try:
        pi.stage_lights([str(tmp_path / "x.fits")], str(tmp_path / "stage"), print)
        raise AssertionError("muss mit klarer Meldung abbrechen")
    except pi.StackError as e:
        assert "lokale Platte" in str(e)
