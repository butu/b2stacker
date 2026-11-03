"""Siril: Ergebnisse öffnen (Flatpak braucht Ordner-Freigaben) und klare Meldung ohne Oberfläche."""
import pytest

from astrostack.engines import siril


def test_flatpak_gui_gets_grant_per_result_folder():
    inst = siril.Install(cli=("flatpak", "run"), gui=("flatpak", "run", siril.FLATPAK_ID), label="x", flatpak=True)
    cmd = inst.gui_command(["/data/a", "/data/a", "/data/b"], ["/data/a/m1.fit", "/data/b/m2.fit"])
    assert cmd[:2] == ["flatpak", "run"]
    assert "--filesystem=/data/a" in cmd and "--filesystem=/data/b" in cmd
    assert cmd[-3:] == [siril.FLATPAK_ID, "/data/a/m1.fit", "/data/b/m2.fit"]


def test_binary_gui_without_gui_binary_is_none(tmp_path, monkeypatch):
    cli = tmp_path / "siril-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    inst = siril._from_binary(str(cli))
    assert inst is not None and inst.gui is None
    assert inst.gui_command([str(tmp_path)], [str(tmp_path / "m.fit")]) is None
    monkeypatch.setattr(siril, "locate", lambda setting="": inst)
    with pytest.raises(FileNotFoundError, match="Siril-Oberfläche nicht gefunden"):
        siril.open_command(str(cli), [str(tmp_path / "m.fit")])


def test_pixinsight_runner_factory_is_rejected_for_siril(tmp_path):
    from astrostack import engines
    from astrostack.engines.pixinsight import StackError
    from astrostack.settings import Settings
    s = Settings(engine="siril", siril="/nicht/da", workdir=str(tmp_path), keep_awake=False, open_results=False)
    with pytest.raises(StackError, match="passt nicht zur Engine siril"):
        engines.run_plan(dict(runs=[], workdir=str(tmp_path)), s, runner_factory=lambda: None)
