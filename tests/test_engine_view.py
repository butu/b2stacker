"""Engine-dependent plan view, engine in the stack record and in the report."""
import copy
import json
import os

from astrostack import project
from astrostack.engines import planview
from astrostack.engines import pixinsight as pi
from astrostack.engines import siril
from astrostack.report import build as report_build


def _plan(workdir="", **opt):
    options = dict(fix_fringes=True, local_normalization=True, hot_pixels=True, distortion_correction=True,
                   large_scale_rejection=True, autocrop=False, weighting="psf_signal", rejection="auto")
    options.update(opt)
    settings = [["Gewichtung", "PSF Signal Weight", "x"], ["Local Normalization", "an", "x"],
                ["Rejection", "automatisch + Large-Scale (hoch)", "x"],
                ["Registrierung", "Verzerrungskorrektur an", "x"], ["Drizzle", "aus", "x"],
                ["Autocrop", "aus", "x"]]
    runs = [dict(name="L", lightCount=3, star_detection=dict(params={"maxStarDistortion": "0.45"}, why="x"))]
    return dict(options=options, settings=settings, runs=runs, workdir=workdir)


def _rows(plan):
    return {r[0]: r[1] for r in plan["settings"]}


def test_pixinsight_view_is_unchanged():
    p = _plan()
    assert planview.apply(p, "pixinsight") is p


def test_siril_settings_show_engine_values():
    rows = _rows(planview.apply(_plan(), "siril"))
    assert "wfwhm" in rows["Gewichtung"]
    assert "rej w 3 3" in rows["Rejection"]
    assert "nicht verfügbar" in rows["Local Normalization"]
    assert "Homografie" in rows["Registrierung"]


def test_siril_weighting_snr_maps_to_noise():
    rows = _rows(planview.apply(_plan(weighting="snr"), "siril"))
    assert "noise" in rows["Gewichtung"]


def test_siril_flags_unsupported_options():
    view = planview.apply(_plan(), "siril")["engine_view"]
    assert set(view["unsupported"]) == {"local_normalization", "distortion_correction", "large_scale_rejection"}
    assert "fix_fringes" not in view["unsupported"] and "hot_pixels" not in view["unsupported"]


def test_siril_findstar_comes_from_engine_mapping():
    p = _plan()
    out = planview.apply(p, "siril")
    expected = siril.map_wbpp(p, p["runs"][0])["findstar"]
    assert expected == {"roundness": "0.45"}
    assert out["runs"][0]["siril"]["findstar"] == "setfindstar -roundness=0.45"
    assert out["runs"][0]["siril"]["weight"] == "wfwhm"


def test_apply_does_not_mutate_input():
    p = _plan()
    before = copy.deepcopy(p)
    planview.apply(p, "siril")
    assert p == before


def test_stack_record_stores_engine(tmp_path):
    pdir = str(tmp_path / "proj")
    os.makedirs(pdir)
    st = pi.Status(state="done", started="2026-01-01T00:00:00")
    st.runs = {"L": dict(outputs=[], lights=2)}
    pi._record_stacks(dict(workdir=pdir, options={}), st, "20260101-000000", lambda m: None,
                      dict(engine="siril", engine_version="1.4.2"))
    rec = json.load(open(os.path.join(pdir, "stacks.json"), encoding="utf-8"))[0]
    assert rec["engine"] == "siril" and rec["engine_version"] == "1.4.2"


def test_report_engine_text_from_last_stack(tmp_path):
    pdir = str(tmp_path)
    assert report_build.engine_meta(pdir, "pixinsight")["engine_text"] == "PixInsight WBPP"
    assert report_build.engine_meta(pdir, "siril")["engine_text"] == "Siril"
    recs = [dict(id="a", date="2026-01-01T10:00:00"),                       # old record without the field
            dict(id="b", date="2026-02-01T10:00:00", engine="siril", engine_version="1.4.2")]
    json.dump(recs, open(os.path.join(pdir, "stacks.json"), "w", encoding="utf-8"))
    assert report_build.engine_meta(pdir, "pixinsight")["engine_text"] == "Siril 1.4.2"
    recs[1] = dict(id="b", date="2026-02-01T10:00:00")
    json.dump(recs, open(os.path.join(pdir, "stacks.json"), "w", encoding="utf-8"))
    assert report_build.engine_meta(pdir, "siril")["engine_text"] == "PixInsight WBPP"


def test_cached_report_shows_engine_version_from_later_stack(tmp_path):
    pdir = str(tmp_path)
    snap = dict(version=1, generated="01.01.2026 10:00", target="T",
                data=dict(meta=dict(target="T", engine="siril", engine_text="Siril", planned_engine="siril"),
                          frames=[], filters=[], kpi=[], nights=[], checks=[], rec=[], plan=dict(runs=[])))
    os.makedirs(os.path.dirname(os.path.join(pdir, report_build.REPORT_DATA)))
    json.dump(snap, open(os.path.join(pdir, report_build.REPORT_DATA), "w", encoding="utf-8"))
    json.dump([dict(id="b", date="2026-02-01T10:00:00", engine="siril", engine_version="1.4.4")],
              open(os.path.join(pdir, "stacks.json"), "w", encoding="utf-8"))
    assert report_build.cached_data(pdir)["meta"]["engine_text"] == "Siril 1.4.4"


def test_siril_autocrop_maps_to_common_area_framing():
    from astrostack.engines import siril as sr
    assert _rows(planview.apply(_plan(autocrop=True), "siril"))["Autocrop"] == "Siril -framing=min"
    assert _rows(planview.apply(_plan(autocrop=False), "siril"))["Autocrop"] == "aus"
    assert sr.map_wbpp(_plan(autocrop=True), {})["framing"] == "min"
    assert sr.map_wbpp(_plan(autocrop=False), {})["framing"] == "max"
    script = sr.build_script(dict(outdir="/o", lights="/l", osc=False, groups=[dict(sub="", lights="/l", n=2)],
                                  framing="min"))
    assert "seqapplyreg pp_light_ -interp=lanczos4 -framing=min -prefix=r_" in script
