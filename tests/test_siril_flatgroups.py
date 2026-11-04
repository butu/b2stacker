"""Siril: flat groups (camera position per night) – master flat per group, then merge into one sequence."""
import os

from astropy.io import fits

from astrostack import engines
from astrostack.engines import siril

from .test_siril_run import frames, make_cli, settings


def make_group_plan(tmp_path):
    data = tmp_path / "data"
    a = frames(data / "light_a", 2, "light", seed=11)
    b = frames(data / "light_b", 2, "light", seed=12)
    fa = frames(data / "flat_a", 3, "flat", seed=13)
    fb = frames(data / "flat_b", 3, "flat", seed=14)
    bias = frames(data / "bias", 3, "bias", seed=15)
    dark = frames(data / "dark", 3, "dark", seed=16)
    groups = [dict(key="1", nights=["n1"], lights=a, flats=fa, flat_set="A"),
              dict(key="2", nights=["n2"], lights=b, flats=fb, flat_set="B")]
    run = dict(name="L", mode="mono", outputs=["L"], channel_map={}, enabled=True, is_osc=False, lights=a + b,
               bias=bias, darks=dark, flats=fa + fb, darkflats=[], reference="auto", hours=0.1, gb=0.01, ram_gb=0.1,
               lightCount=4, flat_groups=groups, star_detection=dict(why="test", params=dict(sensitivity=0.9)))
    plan = dict(target="Test", workdir=str(tmp_path / "work"), runs=[run], options=dict(fix_fringes=False), pixscale=1.0)
    return plan, run, a, b


def test_script_has_one_flat_master_per_group_and_merge():
    base = dict(outdir="/o", bias=dict(master="/m/b.fit"), groups=[
        dict(sub="g1", lights="/o/raw/g1/light", flat=dict(raw="/o/raw/g1/flat", prefix="flat")),
        dict(sub="g2", lights="/o/raw/g2/light", flat=dict(raw="/o/raw/g2/flat", prefix="flat"))])
    txt = siril.build_script(base)
    assert txt.count("convert flat") == 2 and "seq_flat" in txt
    assert "/o/g1/seq_flat" in txt and "/o/g2/seq_flat" in txt
    assert "merge " in txt and "/o/g1/seq_light/pp_light_" in txt and "/o/g2/seq_light/pp_light_" in txt
    assert "register lights_ -2pass" in txt and "stack r_lights_ " in txt


def test_two_flat_groups_stack_together_and_map_back_to_originals(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    plan, run, a, b = make_group_plan(tmp_path)
    logs = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=logs.append, open_results=False)
    assert st.state == "done", st.error
    assert fits.getheader(st.runs["L"]["master"])["STACKCNT"] == 4
    text = "\n".join(logs)
    assert "Gruppe 1" in text and "Gruppe 2" in text
    ssf = open(os.path.join(os.path.dirname(st.runs["L"]["master"]), "siril.ssf"), encoding="utf-8").read()
    assert ssf.count("-flat=") == 2 and "merge " in ssf


def test_bad_frame_in_second_group_is_mapped_to_original_light(tmp_path, monkeypatch):
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    monkeypatch.setenv("FAKESIRIL_BAD_FRAMES", "3")          # 1-based in the merged sequence → first light of group 2
    plan, run, a, b = make_group_plan(tmp_path)
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    res = st.runs["L"]
    assert res["excluded"] == [b[0]]
    assert res["lights"] == 3                               # retry without the wrong frame

