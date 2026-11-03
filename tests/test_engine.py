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


CC_OK = """[x] IC.cosmeticCorrectionHigh = false;
[x] CC.useAutoDetect = true;
[x] CC.hotAutoCheck = true;
[x] Cosmetic Correction: applying b2stacker_CC process icon.
[x] * End cosmetic correction of light frames
"""


def cc_text(items):
    return CC_OK if "usePipelineBuilderScript=true" in items else ""


class FakePI:
    calls = []
    cc_works = True

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
            from astrostack import xisf
            from .test_stackqa import synth_master
            xisf.write_image(os.path.join(out, "master", "masterLight_BIN-1_FILTER-HaO3_RGB.xisf"),
                             synth_master(shape=(300, 450)), {"OBJECT": "vdB 131"})
        self.code = 0xC000041D if self.fail else 0
        if type(self) is FakePI and not script.endswith("split.js") and not self.fail:
            out = dict(i.split("=", 1) for i in items if i.startswith("outputDirectory="))["outputDirectory"]
            os.makedirs(os.path.join(out, "logs"), exist_ok=True)
            k = len(os.listdir(os.path.join(out, "logs")))
            open(os.path.join(out, "logs", "w%d.log" % k), "w").write(cc_text(items) if FakePI.cc_works else "")
        return cmd

    def poll(self):
        return self.code

    def kill(self):
        pass


def _setup(tmp_path, monkeypatch, n=3, pier=None):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    r = str(tmp_path / "data")
    for i in range(n):
        h = dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)})
        if pier:
            h["PIERSIDE"] = pier[i]
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
    import datetime
    day = datetime.date.today().isoformat()
    names = sorted(os.path.basename(f) for f in st.finals)
    assert names == ["vdB131_Ha_%s_0.5h.xisf" % day, "vdB131_OIII_%s_0.5h.xisf" % day], names
    wbpp_items = FakePI.calls[0][1]
    assert "automationMode=true" in wbpp_items and "debayerOutputMethod=0" in wbpp_items
    # keine Darks → PixInsight-CosmeticCorrection per Pipeline-Builder-Script, Original-Lights, strengere Sternerkennung
    assert "usePipelineBuilderScript=true" in wbpp_items and "hotPixelFilterRadius=2" in wbpp_items
    script = [i[len("pipelineBuilderScriptFile="):] for i in wbpp_items if i.startswith("pipelineBuilderScriptFile=")][0]
    code = open(script, encoding="utf-8").read()
    assert "b2stacker_CC" in code and "__HOT__" not in code and "useAutoDetect = true" in code
    assert any(i.startswith("dir=") and i.endswith("/HaO3") and "vdB 131" in i for i in wbpp_items)
    assert len([c for c in FakePI.calls if c[0] != "split.js"]) == 1
    # Snapshot-Bias (IMAGETYP=LIGHT) wurde als korrigierte Kopie übergeben
    bias = [i for i in wbpp_items if "calib_fixed" in i]
    assert len(bias) == 1 and bias[0].startswith("dir=")
    fixed = [os.path.join(bias[0][4:], f) for f in os.listdir(bias[0][4:])]
    assert len(fixed) == 3 and fits.getheader(fixed[0])["IMAGETYP"] == "Bias Frame"
    # Farbsäume gemessen und korrigiert, Kennzahlen da
    outs = st.runs["HaO3"]["outputs"]
    assert outs[0]["kpi"]["fringe_before_px"] > 0.4 and outs[0]["kpi"]["stars"] > 20
    # Zwischendateien weg, Master bleibt
    run_dir = os.path.dirname(os.path.dirname(st.runs["HaO3"]["master"]))
    assert not os.path.exists(os.path.join(run_dir, "registered"))
    assert os.path.exists(st.runs["HaO3"]["master"])
    # Historie
    hist = project.stacks(pdir)
    assert len(hist) == 1 and hist[0]["lights"] == 3 and hist[0]["outputs"][0]["exists"]
    assert not os.path.isabs(json.load(open(os.path.join(pdir, "stacks.json")))[0]["outputs"][0]["file"])
    # zweiter Lauf: eigener Dateiname (_2), nichts überschrieben, Historie wächst
    st2 = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=FakePI)
    assert st2.state == "done"
    assert len(os.listdir(os.path.join(pdir, "final"))) == 4
    assert len(project.stacks(pdir)) == 2
    assert project.latest_output(pdir, "HaO3").endswith("_2.xisf")


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


SA_BLOCK = """[2026-11-01 17:49:55] * Begin registration of light frames
%s[2026-11-01 17:51:10] ===== StarAlignment: 4 succeeded, 0 failed, 0 skipped =====
"""
SA_FRAME = """[2026-11-01 17:51:01] * Loading target file: G:/w/debayered/Light_X/%s_c_d.xisf
[2026-11-01 17:51:01] 527 putative star pair matches.
[2026-11-01 17:51:01] %d star pair matches in 472 RANSAC iterations.
[2026-11-01 17:51:01] Inliers       : %.3f
[2026-11-01 17:51:01] Quality       : %.3f
[2026-11-01 17:51:01] rotation      :     -0.36 deg
[2026-11-01 17:51:01] scale         : %.3f
[2026-11-01 17:51:01] scale_x       : 0.973
[2026-11-01 17:51:01] Registration successful.
"""


class FakePIBadReg(FakePI):
    """Erster Lauf: L_0002 falsch registriert (wie NGC 1999). Wiederholung: Cache, keine Registrierung im Protokoll."""

    def start(self, script, items):
        cmd = super().start(script, items)
        out = dict(i.split("=", 1) for i in items if i.startswith("outputDirectory="))["outputDirectory"]
        os.makedirs(os.path.join(out, "logs"), exist_ok=True)
        k = len(os.listdir(os.path.join(out, "logs")))
        frames = "".join(SA_FRAME % ("L_%04d" % i, 18 if i == 2 else 900, 0.034 if i == 2 else 0.66,
                                    0.36 if i == 2 else 0.9, 0.953 if i == 2 else 1.0) for i in range(4))
        open(os.path.join(out, "logs", "run%d.log" % k), "w").write(
            cc_text(items) + (SA_BLOCK % frames if k == 0 else "keine Registrierung\n"))
        return cmd


def test_bad_registration_is_excluded_and_restacked(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch, n=4)
    FakePI.calls = []
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    lines = []
    st = pi.run_plan(plan, s, log=lines.append, runner_factory=FakePIBadReg)
    assert st.state == "done", st.error
    wbpp = [c for c in FakePI.calls if c[0] != "split.js"]
    assert len(wbpp) == 2                                    # einmal wiederholt
    second = " ".join(wbpp[1][1])
    assert "L_0002" not in second and "L_0001" in second
    r = st.runs["HaO3"]
    assert r["lights"] == 3 and r["excluded"][0].endswith("L_0002.fits")
    assert any("falsch registriert" in n for n in r["notes"])
    assert any("WARNUNG" in l and "L_0002" in l for l in lines)
    hist = project.stacks(pdir)
    assert hist[-1]["excluded"] == ["L_0002.fits"] and hist[-1]["notes"]


def test_registration_parser_on_wbpp_log():
    from astrostack import wbpplog
    frames = "".join(SA_FRAME % ("F_%d" % i, 900, 0.6, 0.9, 1.0) for i in range(3)) + SA_FRAME % ("F_9", 35, 0.096, 0.416, 0.999)
    regs = wbpplog.registrations(SA_BLOCK % frames)
    assert len(regs) == 4 and regs[0]["matches"] == 900 and regs[0]["scale"] == 1.0
    assert wbpplog.judge(regs) == []                         # niedrige, aber plausible Qualität ist kein Fehler
    assert wbpplog.stem("G:/x/3.23_NGC 1999_600.00sec_-9.90c_0034_c_d_r.xisf") == "3.23_NGC 1999_600.00sec_-9.90c_0034"


class FakePILocked(FakePI):
    """Erster Lauf: Bilder der Ostseite liegen „perfekt“ auf der Referenz (an Hotpixeln ausgerichtet)."""
    attempts = 0

    def start(self, script, items):
        cmd = super().start(script, items)
        out = dict(i.split("=", 1) for i in items if i.startswith("outputDirectory="))["outputDirectory"]
        os.makedirs(os.path.join(out, "logs"), exist_ok=True)
        k = FakePILocked.attempts               # jeder Versuch hat einen eigenen, leeren Ausgabeordner
        FakePILocked.attempts += 1
        ref = "[2026-11-01 17:49:55] Reference image: G:/w/debayered/L_0000_c_d.xisf\n"
        frames = "".join(SA_FRAME % ("L_%04d" % i, 900, 0.6, 0.9, 1.0) for i in range(4))
        if k:   # zweiter Versuch: echte Verschiebung/Drehung
            frames = frames.replace("rotation      :     -0.36 deg", "rotation      :   +179.80 deg")
        open(os.path.join(out, "logs", "run%d.log" % k), "w").write(cc_text(items) + SA_BLOCK % (ref + frames))
        return cmd


def test_sensor_lock_detected_and_retried_with_cosmetic(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch, n=4, pier=["West", "West", "East", "East"])
    FakePI.calls = []
    FakePILocked.attempts = 0
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    lines = []
    st = pi.run_plan(plan, s, log=lines.append, runner_factory=FakePILocked)
    assert st.state == "done", st.error
    wbpp = [c for c in FakePI.calls if c[0] != "split.js"]
    assert len(wbpp) == 2
    assert "hotPixelFilterRadius=2" in wbpp[1][1] and "noiseReductionFilterRadius=2" in wbpp[1][1]   # 0–2 gültig
    assert any("am Sensor" in l or "Hotpixeln" in l for l in lines)
    assert st.runs["HaO3"]["lights"] == 4                    # niemand ausgelassen, nur neu ausgerichtet


def test_sensor_lock_twice_fails_clearly(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch, n=4, pier=["West", "West", "East", "East"])

    class AlwaysLocked(FakePILocked):
        def start(self, script, items):
            cmd = FakePI.start(self, script, items)
            out = dict(i.split("=", 1) for i in items if i.startswith("outputDirectory="))["outputDirectory"]
            os.makedirs(os.path.join(out, "logs"), exist_ok=True)
            ref = "[2026-11-01 17:49:55] Reference image: G:/w/debayered/L_0000_c_d.xisf\n"
            frames = "".join(SA_FRAME % ("L_%04d" % i, 900, 0.6, 0.9, 1.0) for i in range(4))
            open(os.path.join(out, "logs", "r%d.log" % len(os.listdir(os.path.join(out, "logs")))), "w").write(
                cc_text(items) + SA_BLOCK % (ref + frames))
            return cmd
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=AlwaysLocked)
    assert st.state == "failed" and "Darks" in st.error


def test_hotpixels_removed_stars_kept():
    import numpy as np
    from astrostack import hotpix
    rng = np.random.default_rng(1)
    img = rng.normal(1000, 20, (400, 600)).astype(np.float32)
    yy, xx = np.mgrid[0:400, 0:600]
    stars = [(50 + 37 * i % 350, 40 + 53 * i % 560, 3000 + 800 * (i % 7)) for i in range(40)]
    for y, x, a in stars:
        img += a * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.8 ** 2))
    hot = [(rng.integers(5, 395), rng.integers(5, 595)) for _ in range(300)]
    clean = img.copy()
    for y, x in hot:
        img[y, x] += 5000
    mask, rep = hotpix.detect(img, is_cfa=True)
    found = sum(mask[y, x] for y, x in hot)
    assert found >= 0.9 * len(hot)
    star_px = np.zeros_like(mask)
    for y, x, a in stars:
        star_px |= (yy - y) ** 2 + (xx - x) ** 2 <= 9
    hot_px = np.zeros_like(mask)
    for y, x in hot:
        hot_px[y, x] = True
    assert (mask & star_px & ~hot_px).sum() <= 3          # Sterne bleiben unangetastet


def test_cc_not_applied_falls_back_to_own_cleaning(tmp_path, monkeypatch):
    s, plan, pdir = _setup(tmp_path, monkeypatch)
    FakePI.calls = []
    monkeypatch.setattr(FakePI, "cc_works", False)
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    st = pi.run_plan(plan, s, log=lambda *a: None, runner_factory=FakePI)
    assert st.state == "done", st.error
    wbpp = [c for c in FakePI.calls if c[0] != "split.js"]
    assert len(wbpp) == 2
    second = wbpp[1][1]
    assert not any(i.startswith("usePipelineBuilderScript") for i in second)
    assert any(i.startswith("dir=") and "/_cosmetic/" in i for i in second)
    assert any("PixInsight-Hotpixel" in n for n in st.runs["HaO3"]["notes"])
