from astrostack import filters as f


def test_classify_common_names():
    assert f.classify("HaO3") == f.DUAL_HA_OIII
    assert f.classify("Optolong L-eXtreme 2\"") == f.DUAL_HA_OIII
    assert f.classify("L-eNhance") == f.DUAL_HA_OIII
    assert f.classify("SII/OIII") == f.DUAL_SII_OIII
    assert f.classify("RGB") == f.BROADBAND
    assert f.classify("UV/IR Cut") == f.BROADBAND
    assert f.classify("") == f.BROADBAND
    assert f.classify(None) == f.BROADBAND
    assert f.classify("Ha") == f.NARROW_HA
    assert f.classify("Antlia 3nm SII Pro") == f.NARROW_SII
    assert f.classify("OIII 6nm") == f.NARROW_OIII
    assert f.classify("Schwefel") == f.NARROW_SII
    assert f.classify("Lum") == f.MONO_L
    assert f.classify("Mystery 42") == f.UNKNOWN


def test_custom_overrides():
    assert f.classify("Mystery 42", {"mystery 42": f.DUAL_SII_OIII}) == f.DUAL_SII_OIII


def test_processing():
    p = f.processing_for(f.DUAL_HA_OIII, is_osc=True)
    assert p.mode == "extract" and p.outputs == ("Ha", "OIII") and p.channel_map["OIII"] == "GB"
    assert f.processing_for(f.DUAL_SII_OIII, True).outputs == ("SII", "OIII")
    assert f.processing_for(f.BROADBAND, True).mode == "debayer"
    assert f.processing_for(f.NARROW_HA, False).outputs == ("Ha",)
    assert f.processing_for(f.MONO_R, False).mode == "mono"
