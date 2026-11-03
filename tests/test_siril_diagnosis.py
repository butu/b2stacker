"""Fehlerdiagnose aus der Siril-Ausgabe (siril._causes): jede Regel mit der echten Meldungsform."""
import pytest

from astrostack.engines import siril


@pytest.mark.parametrize("line, fragment", [
    ("log: not enough star pairs", "Sternpaare"),
    ("log: star matching failed", "Sternzuordnung"),
    ("log: No such file or directory: /x/light_00001.fit", "Datei fehlt"),
    ("log: file not found", "Datei fehlt"),
    ("log: images have different dimensions", "Bildgrößen"),
    ("log: out of memory", "Arbeitsspeicher"),
    ("log: std::bad_alloc: memory limit", "Arbeitsspeicher"),
])
def test_text_rules(line, fragment):
    causes = siril._causes([line])
    assert causes and fragment in causes[0]["text"]


def test_script_error_line_is_quoted_without_log_prefix():
    lines = ["log: Running command: stack", "log: Error in line 7 ('stack r_x'): sequence not found",
             "log: Script execution failed."]
    causes = siril._causes(lines)
    assert causes[-1]["text"].startswith("Error in line 7")
    assert not causes[-1]["text"].startswith("log:")


def test_unknown_error_gives_no_invented_cause():
    assert siril._causes(["log: Running command: convert"]) == []


@pytest.mark.parametrize("code, fragment", [
    (-9, "SIGKILL"), (-11, "Speicherzugriff"), (-6, "SIGABRT"),
])
def test_posix_signal_exit_codes(code, fragment):
    causes = siril._causes([], code)
    assert causes and fragment in causes[0]["text"]


def test_windows_crash_code_is_named():
    causes = siril._causes([], 0xC000041D)
    assert causes and "Arbeitsspeicher" in causes[0]["text"]


def test_plain_exit_code_1_gives_no_crash_cause():
    assert siril._causes([], 1) == []


def test_crash_cause_comes_first_when_text_also_matches():
    causes = siril._causes(["log: out of memory"], -9)
    assert "SIGKILL" in causes[0]["text"]
