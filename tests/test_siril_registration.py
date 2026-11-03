"""Siril: Registrierungs-Check (R0/I-Zeilen), Wiederholungen, Hotpixel-Vorstufe und FWHM-Retry."""
import os

import pytest

from astrostack import engines, wbpplog
from astrostack.engines import siril
from astrostack.engines import pixinsight as pi

from .test_siril_run import make_cli, make_plan, posix_only, settings

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "siril")
LIGHTS4 = ["/d/a.fit", "/d/b.fit", "/d/c.fit", "/d/d.fit"]


def _fixture(name: str) -> str:
    return open(os.path.join(FIX, name), encoding="utf-8").read()


def test_parse_real_mono_sequence_reads_homography_from_register_output():
    # Siril 1.4.4: Homographie steht in <seq>.seq nach „register“; Referenz = Index 3 (Frame 4, Identität)
    regs = siril.parse_registration(_fixture("mono_fault_pp_light.seq"), LIGHTS4)
    assert {"reference": "/d/d.fit"} in regs
    by = {r["file"]: r for r in regs if "file" in r}
    assert by["/d/a.fit"]["tx"] == pytest.approx(-3) and by["/d/a.fit"]["ty"] == pytest.approx(-1)
    assert by["/d/b.fit"]["tx"] == pytest.approx(-8) and by["/d/b.fit"]["ty"] == pytest.approx(2)
    assert by["/d/a.fit"]["scale"] == pytest.approx(1, abs=1e-6)
    assert by["/d/a.fit"]["nstars"] == 76
    assert by["/d/c.fit"]["failed"] is True            # von Siril verworfen (I-Zeile 0, Zeile 0 0 0)


def test_parse_real_osc_sequence_uses_green_channel():
    regs = siril.parse_registration(_fixture("osc_fault_pp_light.seq"), LIGHTS4)
    by = {r["file"]: r for r in regs if "file" in r}
    assert {"reference": "/d/a.fit"} in regs              # OSC: Referenz = Index 0
    assert by["/d/b.fit"]["tx"] == pytest.approx(-5, abs=1e-3) and by["/d/b.fit"]["ty"] == pytest.approx(3, abs=1e-3)
    assert by["/d/c.fit"]["failed"] is True


def test_seqapplyreg_identity_sequence_is_not_used_for_checks():
    # r_-Sequenz hat nach seqapplyreg nur Identität → taugt nicht zur Prüfung
    regs = siril.parse_registration(_fixture("mono_fault_r_pp_light.seq"), ["/d/a.fit", "/d/b.fit", "/d/d.fit"])
    assert all(r.get("tx", 0) == 0 for r in regs)


def test_lock_frame_is_judged_as_sensor_alignment():
    lights = ["/d/a_c.fit", "/d/b_c.fit", "/d/c_c.fit"]
    info = {"a": {"night": "n1", "pier": None}, "b": {"night": "n2", "pier": None}, "c": {"night": "n1", "pier": None}}
    seq = "S 'r_pp_light_' 1 3 3 5 0 6 0 0 0\nL 1\nI 1 1\nI 2 1\nI 3 1\n" \
          "R0 4.6 4.6 0.77 0 0.02 38 H 1 0 0 0 1 0 0 0 1\n" \
          "R0 4.6 4.6 0.77 0 0.02 38 H 1 0 0 0 1 0 0 0 1\n" \
          "R0 4.6 4.6 0.77 0 0.02 38 H 1 0 0 0 1 0 0 0 1\n"
    bad = wbpplog.judge(siril.parse_registration(seq, lights), info)
    assert [b["file"] for b in bad] == ["/d/b_c.fit"] and bad[0]["lock"] is True
    assert pi.registration_verdict(bad, [b["file"] for b in bad], dict(lights=lights)) == "lock"


def test_verdict_drop_suspect_and_ok():
    lights = ["/d/%d.fit" % i for i in range(5)]
    bad = [dict(file=lights[1], why=["nicht registriert"])]
    assert pi.registration_verdict(bad, [lights[1]], dict(lights=lights)) == "drop"
    assert pi.registration_verdict(bad, [lights[1]], dict(lights=lights, _retry=True)) == "suspect"
    assert pi.registration_verdict([], [], dict(lights=lights)) == "ok"


@posix_only
def test_bad_frame_is_dropped_and_stack_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    monkeypatch.setenv("FAKESIRIL_BAD_FRAMES", "2")
    plan, _, lights = make_plan(tmp_path)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    res = st.runs["L"]
    assert res["excluded"] == [lights[1]]
    assert any("ausgelassen" in n and "Siril" in n for n in res["notes"])
    assert res["lights"] == 3


@posix_only
def test_suspect_frames_are_reported_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    monkeypatch.setenv("FAKESIRIL_BAD_FRAMES", "2,3")          # nur 2 Frames blieben → nichts ausschließen
    plan, _, _ = make_plan(tmp_path)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    assert st.runs["L"]["excluded"] == [] and any("möglicherweise" in n for n in st.runs["L"]["notes"])


@posix_only
def test_lock_twice_stops_with_plain_message(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKESIRIL_ZERO_SHIFT", "1")          # alle Frames „0,0 px“ = am Sensor ausgerichtet
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    plan, _, lights = make_plan(tmp_path)
    info = {wbpplog.stem(p).lower(): {"night": "n2" if i == 1 else "n1", "pier": None} for i, p in enumerate(lights)}
    monkeypatch.setattr(siril.wbpplog, "light_info", lambda ps: info)
    logs = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=logs.append, open_results=False)
    assert st.state == "failed" and "zweiten Versuch" in st.error
    assert any("Neuer Versuch" in m for m in logs) and any("Sternerkennung strenger" in m for m in logs)


@posix_only
def test_fwhm_implausible_retries_once_with_stricter_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    calls = []

    def fake_check(stats, outputs):
        calls.append(1)
        return "Sterne im Stack 1,0 px statt 4,0 px" if len(calls) == 1 else None
    monkeypatch.setattr(pi, "fwhm_implausible", fake_check)
    plan, _, _ = make_plan(tmp_path)
    logs = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=logs.append, open_results=False)
    assert st.state == "done", st.error
    assert any("Hotpixel werden vor dem Stacken" in m for m in logs)
    base = max(0.3, min(3.0, round(2.0 - 1.5 * 0.9, 2)))              # sensitivity 0.9 aus make_plan
    assert any("setfindstar -sigma=%.2f" % (base + 1.0) in m for m in logs)
    assert any("Erster Versuch unplausibel" in n for n in st.runs["L"]["notes"])


@posix_only
def test_hot_pixels_without_darks_clean_before_stack(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    plan, _, _ = make_plan(tmp_path, darks=0)
    logs = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=logs.append, open_results=False)
    assert st.state == "done", st.error
    assert any("Hotpixel werden vor dem Stacken" in m for m in logs)
    script = open(os.path.join(os.path.dirname(st.runs["L"]["master"]), "siril.ssf")).read()
    assert "-cc=dark" not in script


@posix_only
def test_darks_use_cosmetic_correction_with_configured_sigmas(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    plan, _, _ = make_plan(tmp_path, darks=3)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    script = open(os.path.join(os.path.dirname(st.runs["L"]["master"]), "siril.ssf")).read()
    assert "-cc=dark %.1f %.1f" % (pi.CC_COLD_SIGMA, pi.CC_HOT_SIGMA) in script


def test_registration_without_rows_is_not_reported_as_plausible(tmp_path):
    seq_dir = tmp_path / "seq_light"
    seq_dir.mkdir()
    (seq_dir / "pp_light_.seq").write_text("#Siril sequence file\nS 'pp_light_' 1 0 0 5 0 6 0 0 0\n")
    logs = []
    lights = [str(tmp_path / ("light_%d.fit" % i)) for i in range(3)]
    assert siril._check_registration(dict(name="L", lights=lights), str(tmp_path), "pp_light_", 0, [],
                                     logs.append) is None
    assert any("nicht geprüft" in m for m in logs)
    assert not any("plausibel" in m for m in logs)
