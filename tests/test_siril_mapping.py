"""WBPP options → Siril commands (siril.map_wbpp) and their effect in script / run."""
import os

from astrostack import engines
from astrostack.engines import siril

from .test_siril_run import cli, make_plan, settings  # noqa: F401 – Fixture

PLAN = dict(options=dict(local_normalization=True, distortion_correction=True, large_scale_rejection=True,
                         weighting="psf_signal"))


def _run(**kw):
    return dict(dict(star_detection=None, wbpp={}), **kw)


def test_sensitivity_becomes_sigma_and_is_consumed():
    run = _run(star_detection=dict(params=dict(sensitivity=0.9)))
    m = siril.map_wbpp(PLAN, run)
    assert m["findstar"] == {"sigma": 0.65}
    assert not any("sensitivity" in n for n in m["notes"])


def test_lock_retry_raises_sigma_by_one():
    m = siril.map_wbpp(PLAN, _run(star_detection=dict(params=dict(sensitivity=0.9))), lock_retry=True)
    assert m["findstar"]["sigma"] == 1.65


def test_shape_params_map_to_findstar_with_notes():
    run = _run(star_detection=dict(params=dict(maxStarDistortion="0.45", minStructureSize="9")))
    m = siril.map_wbpp(PLAN, run)
    assert m["findstar"] == {"roundness": "0.45", "minA": "9"}
    assert sum("→ setfindstar" in n for n in m["notes"]) == 2


def test_unmapped_params_are_named_not_silently_dropped():
    run = _run(star_detection=dict(params=dict(structureLayers="6", peakResponse="0.5",
                                               hotPixelFilterRadius="2")))
    m = siril.map_wbpp(PLAN, run)
    text = " | ".join(m["notes"])
    assert "structureLayers" in text and "peakResponse" in text
    assert "hotPixelFilterRadius" not in text                    # handled by b2stacker, no hint


def test_weighting_options_map_to_siril_weight():
    assert siril.map_wbpp(dict(options=dict(weighting="psf_signal")), _run())["weight"] == "wfwhm"
    assert siril.map_wbpp(dict(options=dict(weighting="psf_snr")), _run())["weight"] == "noise"
    assert siril.map_wbpp({"options": {}}, _run())["weight"] == "wfwhm"


def test_plan_options_without_siril_equivalent_are_listed():
    text = " | ".join(siril.map_wbpp(PLAN, _run())["notes"])
    for word in ("Local Normalization", "Verzeichnungskorrektur", "Satellitenspuren"):
        assert word in text


def test_rejection_maps_follow_wbpp_flag():
    assert siril.map_wbpp(PLAN, _run())["rejmaps"] is True
    assert siril.map_wbpp(PLAN, _run(wbpp={"generateRejectionMaps": "false"}))["rejmaps"] is False


def test_unknown_expert_key_is_reported():
    m = siril.map_wbpp(PLAN, _run(wbpp={"rejectionLow": "4"}))
    assert any("rejectionLow=4" in n for n in m["notes"])


def test_script_contains_mapped_options():
    s = siril.build_script(dict(outdir="/o", lights="/o/l", flat=None, bias=None, dark=None, osc=False,
                                findstar=dict(sigma=0.65, roundness="0.45"), weight="noise", rejmaps=True,
                                cosmetic=False))
    assert "setfindstar -roundness=0.45 -sigma=0.65" in s
    assert "-weight=noise" in s and "-rejmaps" in s
    plain = siril.build_script(dict(outdir="/o", lights="/o/l", flat=None, bias=None, dark=None, osc=False,
                                    cosmetic=False))
    assert "setfindstar" not in plain and "-rejmaps" not in plain and "-weight=wfwhm" in plain


def test_run_writes_rejection_maps_next_to_master_and_logs_gaps(tmp_path, cli):
    plan, run, _ = make_plan(tmp_path)
    plan["options"] = dict(plan["options"], local_normalization=True)
    lines = []
    st = engines.run_plan(plan, settings(tmp_path, cli), log=lambda *a: lines.append(" ".join(map(str, a))),
                          open_results=False)
    assert st.state == "done", st.error
    rej = os.path.join(os.path.dirname(st.runs["L"]["master"]), "master", "rejection_low.fit")
    assert os.path.exists(rej)
    assert any("nicht übernommen" in ln and "Local Normalization" in ln for ln in lines)


def test_command_lines_use_shared_constants_exactly():
    s = siril.build_script(dict(outdir="/o", lights="/o/l", flat=dict(master="/m/f.fit"), bias=None, dark=None,
                                osc=False, cosmetic=False, weight="wfwhm", rejmaps=False))
    assert "register pp_light_ -2pass -minpairs=10 -transf=homography -maxstars=2000" in s
    assert "seqapplyreg pp_light_ -interp=lanczos4 -prefix=r_" in s
    assert "stack r_pp_light_ rej w 3 3 -norm=addscale -weight=wfwhm -output_norm -32b -out=result" in s
    assert siril.REJECTION == "rej w 3 3" and siril.REG_TRANSFORM == "-transf=homography" and siril.INTERP == "lanczos4"
