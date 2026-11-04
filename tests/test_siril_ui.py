"""Engine-dependent step list: Siril texts name no PixInsight/WBPP; PixInsight texts unchanged."""
from astrostack import steps


def _text(entry):
    return " ".join(str(v) for v in entry.values())


def test_siril_step_texts_name_no_pixinsight():
    siril = steps.stack_for("siril")
    assert [s["key"] for s in siril] == [s["key"] for s in steps.STACK]
    for entry in siril:
        assert "PixInsight" not in _text(entry) and "WBPP" not in _text(entry), entry


def test_pixinsight_step_texts_unchanged():
    assert steps.stack_for("pixinsight") is steps.STACK
    cosmetic = next(s for s in steps.STACK if s["key"] == "cosmetic")
    assert "PixInsight (CosmeticCorrection, Auto Detect)" in cosmetic["what"]


def test_siril_cosmetic_is_own_correction():
    cosmetic = next(s for s in steps.stack_for("siril") if s["key"] == "cosmetic")
    assert "b2stacker entfernt" in cosmetic["what"]
    assert "Siril" in cosmetic["why"]


def test_stack_for_does_not_mutate_shared_steps():
    before = [dict(s) for s in steps.STACK]
    steps.stack_for("siril")
    assert steps.STACK == before
