"""Bericht 2 §14: StarAlignment-Parameter aus den gemessenen Sternen; Plausibilität des Stacks."""
from astrostack import plan
from astrostack.engines import pixinsight as pi

NGC3486 = dict(fwhm_p10=6.8, fwhm_med=7.7, fwhm_p90=8.4, ecc_med=0.66, ecc_p90=0.70)


def test_large_oval_stars_get_adapted_detection():
    sd = plan.star_detection(NGC3486)
    p = sd["params"]
    assert 0.4 <= float(p["maxStarDistortion"]) <= 0.5
    assert p["hotPixelFilterRadius"] == 2 and p["noiseReductionFilterRadius"] == 2       # Zahlen, nie Text
    assert 4 <= int(p["minStructureSize"]) <= 12
    assert "ovale" in sd["why"] and "große" in sd["why"]
    checked, notes = pi.check_wbpp_params(p)
    assert not notes, "alle Werte im gültigen Bereich"
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p.values())


def test_small_round_stars_keep_wbpp_defaults():
    assert plan.star_detection(dict(fwhm_p10=2.2, fwhm_med=2.6, fwhm_p90=3.0, ecc_p90=0.45)) is None
    assert plan.star_detection(None) is None


def test_huge_stars_get_more_layers():
    p = plan.star_detection(dict(fwhm_p10=14, fwhm_med=17, fwhm_p90=20, ecc_p90=0.4))["params"]
    assert p["structureLayers"] == 6


def test_light_stats_percentiles():
    fr = [dict(fwhm=f, ecc=0.5 + f / 100, nstars=100) for f in range(1, 11)]
    st = plan.light_stats(fr)
    assert st["fwhm_med"] == 5.5 and st["n"] == 10 and st["nstars_med"] == 100
    assert plan.light_stats(fr[:2]) is None


def test_items_use_detection_but_user_overrides_win():
    run = dict(mode="debayer", lights=["/a/L1.fits"], star_detection=dict(params={"maxStarDistortion": "0.46"}),
               wbpp={"hotPixelFilterRadius": "1"})
    items = pi.wbpp_items(dict(options={}), run, "/out", None, {})
    assert "maxStarDistortion=0.46" in items and "hotPixelFilterRadius=1" in items


def test_master_with_tiny_stars_is_implausible():
    outs = [dict(name="RGB", kpi=dict(fwhm_px=2.44))]
    assert "2,4 px statt 7,7 px" in pi.fwhm_implausible(dict(fwhm_med=7.7), outs)
    assert pi.fwhm_implausible(dict(fwhm_med=7.7), [dict(name="RGB", kpi=dict(fwhm_px=7.1))]) is None
    assert pi.fwhm_implausible(None, outs) is None
