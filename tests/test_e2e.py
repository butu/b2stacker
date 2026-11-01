"""Ende-zu-Ende: synthetische Sternfelder → Scan → Vermessung → Bericht + Plan."""
import json
import os

import numpy as np

from astrostack import project
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits


def star_field(seed, shape=(256, 384), fwhm=3.0, n=60, bg=1200):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    img = np.full(shape, float(bg))
    s = fwhm / 2.355
    srng = np.random.default_rng(1)  # gleiche Sterne in allen Frames
    for _ in range(n):
        cy, cx = srng.uniform(10, shape[0] - 10), srng.uniform(10, shape[1] - 10)
        img += srng.uniform(2000, 20000) * np.exp(-((y - cy) ** 2 + (x - cx) ** 2) / (2 * s * s))
    img += rng.normal(0, 15, shape)
    return np.clip(img, 0, 65535).astype(np.uint16)


def test_report_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    r = str(tmp_path / "data")
    for i in range(4):
        h = dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i), "DATE-LOC": "2026-10-12T22:%02d:00" % (10 * i)})
        make_fits(f"{r}/vdB 131/2026-10-12/HaO3/2.6{i}_vdB 131_600.00sec_gain100_-9.90c_000{i}.fits", h,
                  data=star_field(i))
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(3):
        make_fits(f"{r}/FLAT/FLAT_2026-10-31/HaO3/flat_000{i}.fits", hf, shape=(256, 384), value=30000)
    s = Settings(workdir=str(tmp_path / "work"), analysis_workers=1)
    res = project.run_report([r], None, s, log=lambda *a: None)
    assert len(res) == 1 and res[0]["frames"] == 4
    html = open(res[0]["html"], encoding="utf-8").read()
    assert "vdB 131" in html and "/*__DATA__*/null" not in html
    plan = json.load(open(res[0]["plan"], encoding="utf-8"))
    assert plan["runs"] and plan["runs"][0]["lightCount"] >= 3
    assert plan["runs"][0]["flats"], "Flats sollten zugeordnet sein"
    assert os.path.exists(os.path.join(s.workdir, "projects", "vdB 131", "calibration.json"))


def test_cli_scan_json(mixed_tree, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "settings.json"))
    from astrostack.cli import main
    main(["--json", "scan", mixed_tree])
    d = json.loads(capsys.readouterr().out)
    assert any(g["target"] == "vdB 131" for g in d["lights"])
