"""Siril-Lauf auf einem echten Report-Plan: Ergebnis wird von Requality, latest_output und Report gefunden."""
import json
import os
import sys

import pytest

from astrostack import engines, project, stackqa
from astrostack.engines import siril
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits
from .test_e2e import star_field
from .test_siril_run import make_cli

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Wrapper ist ein Shell-Skript")


@posix_only
def test_siril_stack_is_found_by_requality_and_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(siril, "_VERSION_CACHE", {})
    cli = make_cli(tmp_path / "siril-cli")
    r = str(tmp_path / "data")
    for i in range(4):
        h = dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i), "DATE-LOC": "2026-10-12T22:%02d:00" % (10 * i)})
        make_fits(f"{r}/vdB 131/2026-10-12/HaO3/2.6{i}_vdB 131_600.00sec_gain100_-9.90c_000{i}.fits", h,
                  data=star_field(i))
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(3):
        make_fits(f"{r}/FLAT/FLAT_2026-10-31/HaO3/flat_000{i}.fits", hf, shape=(256, 384), value=30000)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1, engine="siril", siril=cli,
                 keep_awake=False, open_results=False)
    res = project.run_report([r], None, s, log=lambda *a: None)
    plan = json.load(open(res[0]["plan"], encoding="utf-8"))
    pdir = plan["workdir"]

    st = engines.run_plan(plan, s, log=lambda *a: None, open_results=False)
    assert st.state == "done", st.error
    final = st.finals[0]
    assert project.latest_output(pdir, "HaO3") == final

    # Requality (server.start_requality) rechnet dieselben Kennzahlen aus den gespeicherten Outputs
    img = stackqa.load_master(final)
    k = stackqa.kpis(img, plan.get("pixscale"), {})
    assert k["stars"] >= 0 and k["noise"] >= 0
    rec = project.stacks(pdir)[-1]
    assert rec["outputs"][0]["file"] == final

    rep = project.run_report([r], None, s, log=lambda *a: None)
    assert len(rep) == 1 and os.path.exists(rep[0]["html"])
