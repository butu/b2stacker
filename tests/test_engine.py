"""Stacking-Engine mit simuliertem PixInsight (prüft Befehl, Kalibrier-Korrektur, Aufteilen, Aufräumen)."""
import json
import os
import threading

from astropy.io import fits

from astrostack import project
from astrostack.engines import pixinsight as pi
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_e2e import star_field


class FakePI:
    calls = []

    def __init__(self, fail=False):
        self.fail = fail
        self.code = None

    def start(self, script, items):
        cmd = pi.build_command("PixInsight.exe", script, items)
        FakePI.calls.append((os.path.basename(script), items, cmd))
        kv = [i.split("=", 1) for i in items]
        d = {}
        for k, v in kv:
            d.setdefault(k, []).append(v)
        if script.endswith("split.js"):
            for k in d:
                if k.startswith("out"):
                    open(d[k][0], "wb").write(b"XISF0100")
        elif not self.fail:
            out = d["outputDirectory"][0]
            os.makedirs(os.path.join(out, "registered"), exist_ok=True)
            open(os.path.join(out, "registered", "a_r.xisf"), "wb").close()
            os.makedirs(os.path.join(out, "master"), exist_ok=True)
            open(os.path.join(out, "master", "masterLight_BIN-1_FILTER-HaO3_RGB.xisf"), "wb").write(b"XISF0100")
        self.code = 0xC000041D if self.fail else 0
        return cmd

    def poll(self):
        return self.code

    def kill(self):
        pass


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    r = str(tmp_path / "data")
    for i in range(3):
        h = dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)})
        make_fits(f"{r}/vdB 131/2026-10-12/HaO3/L_{i:04d}.fits", h, data=star_field(i))
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(3):
        make_fits(f"{r}/FLAT/FLAT_2026-10-31/HaO3/flat_{i:04d}.fits", hf, shape=(256, 384), value=30000)
    hb = dict(NINA_LIGHT, IMAGETYP="LIGHT", EXPOSURE=0.0001, EXPTIME=0.0001)
    hb.pop("OBJECT")
    for i in range(3):
        make_fits(f"{r}/Bias/2024-11-05/b_{i:04d}.fits", hb, shape=(256, 384), value=500)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1, keep_awake=False, open_results=False,
                 min_free_ram_gb=0.1)
    res = project.run_report([r], None, s, log=lambda *a: None)
    pdir = project.pdir_of(s, res[0]["project"])
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir
    for run in plan["runs"]:
        run["gb"] = 0
    return s, plan, pdir


def test_stack_extract_haoiii(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch)
    FakePI.calls = []
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=FakePI)
    assert st.state == "done", st.error
    names = sorted(os.path.basename(f) for f in st.finals)
    assert names == ["vdB131_Ha.xisf", "vdB131_OIII.xisf"]
    wbpp_items = FakePI.calls[0][1]
    assert "automationMode=true" in wbpp_items and "debayerOutputMethod=0" in wbpp_items
    # ganzer Light-Ordner → dir=, Flats-Ordner → dir=
    assert any(i.startswith("dir=") and i.endswith("/HaO3") and "vdB 131" in i for i in wbpp_items)
    # Snapshot-Bias (IMAGETYP=LIGHT) wurde als korrigierte Kopie übergeben
    bias = [i for i in wbpp_items if "calib_fixed" in i]
    assert len(bias) == 1 and bias[0].startswith("dir=")
    fixed = [os.path.join(bias[0][4:], f) for f in os.listdir(bias[0][4:])]
    assert len(fixed) == 3 and fits.getheader(fixed[0])["IMAGETYP"] == "Bias Frame"
    # Aufteilen mit R und GB
    split_items = FakePI.calls[1][1]
    assert "ch1=R" in split_items and "ch2=GB" in split_items
    # Zwischendateien weg, Master bleibt
    run_dir = os.path.dirname(st.runs["HaO3"]["master"].rsplit(os.sep + "master", 1)[0] + os.sep)
    assert not os.path.exists(os.path.join(run_dir, "registered"))
    assert os.path.exists(st.runs["HaO3"]["master"])
    # zweiter Lauf: altes Ergebnis wird archiviert statt überschrieben
    st2 = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=FakePI)
    assert st2.state == "done"
    assert len(os.listdir(os.path.join(pdir, "final", "_alt"))) == 2


def test_stack_crash_reports_reason(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=lambda: FakePI(fail=True))
    assert st.state == "failed" and "Arbeitsspeicher" in st.error
    saved = json.load(open(os.path.join(pdir, "runs", os.listdir(os.path.join(pdir, "runs"))[0], "status.json")))
    assert saved["state"] == "failed"


def test_refuses_when_pixinsight_open(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [{"pid": 1, "mem_mb": 9000}])
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=FakePI)
    assert st.state == "failed" and "läuft bereits" in st.error


def test_selected_files_use_file_args(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    for n in ("a.fits", "b.fits", "c.fits"):
        (d / n).write_bytes(b"")
    items = pi.compress_paths([str(d / "a.fits"), str(d / "b.fits")])
    assert all(i.startswith("file=") for i in items) and len(items) == 2
    assert pi.compress_paths([str(d / n) for n in ("a.fits", "b.fits", "c.fits")])[0].startswith("dir=")


def test_comma_in_path_rejected():
    import pytest
    with pytest.raises(pi.StackError):
        pi.build_command("pi", "WBPP.js", ["dir=C:/a,b"])


def test_plan_edits_survive(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch)
    project.save_edits(pdir, {"options": {"local_normalization": False}, "runs": {"HaO3": {"enabled": False}}})
    p = project.apply_edits(pdir)
    assert p["options"]["local_normalization"] is False and p["runs"][0]["enabled"] is False
    st = pi.run_plan(dict(p, workdir=pdir), s, log=lambda *a: None, runner_factory=FakePI, cancel=threading.Event())
    assert st.state == "failed" and "Kein Lauf" in st.error
