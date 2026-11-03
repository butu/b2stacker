"""Selbsttest auf dem echten Rechner: kleiner synthetischer Datensatz → Analyse → Stacking mit der gewählten Engine.

Prüft genau die Dinge, die Linux-Tests nicht sehen: Netzlaufwerk, Komma im Ordnernamen, Flat-Gruppen, Symlink-Recht
(Entwicklermodus), lange Pfade, WBPP-Version und die Hotpixel-Korrektur in PixInsight bzw. die Siril-Version. Der Testordner wird nur neu
angelegt (``b2stacker-selbsttest, mit Komma``) – vorhandene Daten werden nicht angefasst. Das Testprojekt liegt in einem
eigenen Arbeitsordner (``<arbeitsverzeichnis>/selbsttest``) und erscheint nicht in der Projektliste.
"""
from __future__ import annotations

import datetime
import json
import os
import time

import numpy as np

TEST_DIR = "b2stacker-selbsttest, mit Komma"
NIGHTS = (("2026-01-10", 273.5), ("2026-01-17", 93.4))


def _fits(path, header, data):
    from astropy.io import fits
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hdu = fits.PrimaryHDU(data)
    for k, v in header.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)


def _stars(seed, shape=(800, 1200), n=260, fwhm=3.2, bg=1100):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:shape[0], 0:shape[1]]
    img = np.full(shape, float(bg))
    s = fwhm / 2.355
    srng = np.random.default_rng(7)
    dx, dy = rng.uniform(-6, 6, 2)                       # leichtes Dithering
    for _ in range(n):
        cy, cx = srng.uniform(12, shape[0] - 12) + dy, srng.uniform(12, shape[1] - 12) + dx
        amp = srng.uniform(1500, 22000)
        r0, r1 = int(max(cy - 10, 0)), int(min(cy + 11, shape[0]))
        c0, c1 = int(max(cx - 10, 0)), int(min(cx + 11, shape[1]))
        img[r0:r1, c0:c1] += amp * np.exp(-((y[r0:r1, c0:c1] - cy) ** 2 + (x[r0:r1, c0:c1] - cx) ** 2) / (2 * s * s))
    img += rng.normal(0, 18, shape)
    return np.clip(img, 0, 65535).astype(np.uint16)


BASE = {"EXPOSURE": 120.0, "EXPTIME": 120.0, "GAIN": 120, "OFFSET": 30, "BAYERPAT": "RGGB", "XBINNING": 1,
        "INSTRUME": "b2stacker Selbsttest", "CCD-TEMP": -10.0, "SET-TEMP": -10.0, "FOCALLEN": 800.0, "XPIXSZ": 3.76,
        "SITELAT": 48.86, "SITELONG": 15.01, "SWCREATE": "N.I.N.A. 3.2.0.9001 (x64)"}


def make_dataset(folder: str, log=print) -> str:
    """Lights (2 Nächte, Kamera gedreht), Flats je Stellung, Bias – alles unter einem Ordner mit Komma."""
    root = os.path.join(folder, TEST_DIR)
    if os.path.isdir(root) and os.listdir(root):
        log("Testdaten vorhanden: %s" % root)
        return root
    log("Lege Testdaten an: %s" % root)
    k = 0
    for night, rot in NIGHTS:
        for i in range(4):
            h = dict(BASE, IMAGETYP="LIGHT", OBJECT="Selbsttest", FILTER="RGB", ROTATOR=rot, RA=150.0, DEC=30.0,
                     **{"DATE-OBS": "%sT20:%02d:00" % (night, 5 * i), "DATE-LOC": "%sT21:%02d:00" % (night, 5 * i)})
            _fits(os.path.join(root, "Lights", night, "Light_Selbsttest_120s_%04d.fits" % k), h, _stars(k))
            k += 1
    yy, xx = np.mgrid[0:800, 0:1200]
    vign = 1 - 0.25 * (((yy - 400) / 400.0) ** 2 + ((xx - 600) / 600.0) ** 2)
    for night, rot in NIGHTS:
        for i in range(5):
            h = dict(BASE, IMAGETYP="FLAT", EXPOSURE=1.5, EXPTIME=1.5, FILTER="RGB", ROTATOR=rot,
                     **{"DATE-OBS": "%sT06:00:%02d" % (night, i), "DATE-LOC": "%sT07:00:%02d" % (night, i)})
            data = np.clip(28000 * vign + np.random.default_rng(100 + i).normal(0, 60, vign.shape), 0, 65535).astype(np.uint16)
            _fits(os.path.join(root, "Bias, Darks, Flats", "Flats", night, "Flat_%04d.fits" % i), h, data)
    for i in range(5):
        h = dict(BASE, IMAGETYP="BIAS", EXPOSURE=0.0001, EXPTIME=0.0001)
        data = np.clip(500 + np.random.default_rng(200 + i).normal(0, 8, (800, 1200)), 0, 65535).astype(np.uint16)
        _fits(os.path.join(root, "Bias, Darks, Flats", "Bias", "Gain 120", "Bias_%04d.fits" % i), h, data)
    return root


def environment(settings, folder: str) -> dict:
    from . import staging, system
    from .engines.pixinsight import is_remote_path
    wd = os.path.join(settings.workdir, "selbsttest")
    os.makedirs(wd, exist_ok=True)
    env = dict(engine=settings.engine, test_folder=folder, test_folder_remote=is_remote_path(folder),
               symlinks_allowed=staging.symlinks_allowed(wd), long_paths=staging.long_paths_enabled(),
               workdir=settings.workdir, workdir_free_gb=system.free_disk_gb(settings.workdir))
    if settings.engine == "siril":
        from .engines import siril
        c = siril.check(settings.siril)
        env.update(siril=settings.siril, siril_ok=c["ok"], siril_version=c["version"], siril_cmd=c["cmd"],
                   siril_message=c["message"])
        return env
    pi_dir = os.path.dirname(os.path.dirname(settings.pixinsight))
    version = None
    for cand in (os.path.join(pi_dir, "doc", "PixInsight.txt"), os.path.join(pi_dir, "bin", "version.txt")):
        if os.path.exists(cand):
            try:
                version = open(cand, encoding="utf-8", errors="replace").readline().strip()[:80]
            except OSError:
                pass
    env.update(pixinsight=settings.pixinsight, pixinsight_ok=os.path.exists(settings.pixinsight),
               wbpp_ok=os.path.exists(settings.wbpp_script), pixinsight_version=version)
    return env


def run(settings, folder: str, log=print, progress=None, cancel=None, with_pixinsight: bool = True,
        runner_factory=None) -> dict:
    """Selbsttest mit der in den Einstellungen gewählten Engine (PixInsight/WBPP oder Siril)."""
    from dataclasses import replace

    from . import engines, project
    t0 = time.time()
    siril_mode = settings.engine == "siril"
    label = engines.LABELS.get(settings.engine, settings.engine)
    res = dict(started=datetime.datetime.now().isoformat(timespec="seconds"), engine=settings.engine, steps=[])

    def step(name, ok, detail=""):
        res["steps"].append(dict(name=name, ok=ok, detail=detail))
        log("%s %s%s" % ("✓" if ok else "✗", name, (" – " + detail) if detail else ""))

    env = environment(settings, folder)
    res["environment"] = env
    if siril_mode:
        step("Siril gefunden", env["siril_ok"], env["siril_message"])
        engine_ok = env["siril_ok"]
    else:
        step("PixInsight gefunden", env["pixinsight_ok"] and env["wbpp_ok"], env["pixinsight"])
        engine_ok = env["pixinsight_ok"]
    step("Datei-Symlinks erlaubt (Entwicklermodus)", env["symlinks_allowed"],
         "" if env["symlinks_allowed"] else "ohne: Dateien vom Netzlaufwerk werden kopiert")
    if env["long_paths"] is not None:
        step("Lange Pfade (LongPathsEnabled)", env["long_paths"], "" if env["long_paths"] else "Pfade > 260 Zeichen scheitern")
    step("Testordner auf Netzlaufwerk", True, "ja" if env["test_folder_remote"] else "nein (lokal)")
    if progress:
        progress(stage="Testdaten anlegen", step="scan", done=0, total=3)
    root = make_dataset(folder, log)
    s2 = replace(settings, workdir=os.path.join(settings.workdir, "selbsttest"), open_results=False)
    if progress:
        progress(stage="Testdaten analysieren", step="measure", done=1, total=3)
    rep = project.run_report([root], None, s2, log=log, cancel=cancel)
    pdir = project.pdir_of(s2, rep[0]["project"])
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir
    groups = len(plan["runs"][0].get("flat_groups") or []) if plan.get("runs") else 0
    step("Analyse", bool(plan.get("runs")), "%d Lights, %d Flat-Gruppen" % (plan.get("total_frames") or 0, groups))
    if with_pixinsight and (engine_ok or runner_factory):
        if progress:
            progress(stage="Stacken mit %s" % label, step="stack", done=2, total=3)
        for r in plan["runs"]:
            r["gb"] = min(r.get("gb") or 0, 1)
        st = engines.run_plan(plan, s2, log=log, cancel=cancel, open_results=False, runner_factory=runner_factory)
        res["stack"] = dict(state=st.state, error=st.error, causes=st.causes, finals=st.finals)
        step("Stacking mit %s" % label, st.state == "done", st.error or ", ".join(os.path.basename(f) for f in st.finals))
        jobs = sorted(os.listdir(os.path.join(pdir, "runs")))
        jobdir = os.path.join(pdir, "runs", jobs[-1]) if jobs else None
        if jobdir:
            res["jobdir"] = jobdir
        if jobdir and not siril_mode:
            from . import wbpplog
            from .engines import pixinsight as pi
            for r in plan["runs"]:
                out = os.path.join(jobdir, pi.safe_file(r["name"]))
                if os.path.isdir(os.path.join(out, "logs")):
                    cc = pi.cc_check(out)
                    res.setdefault("cosmetic", {})[r["name"]] = {k: v for k, v in cc.items() if k != "log"}
                    res.setdefault("wbpp_causes", {})[r["name"]] = wbpplog.diagnose(out)
    else:
        step("Stacking mit %s" % label, False, "übersprungen (%s nicht gefunden)" % label)
    res["ok"] = all(s["ok"] for s in res["steps"] if not s["name"].startswith(("Datei-Symlinks", "Testordner")))
    res["duration_s"] = round(time.time() - t0, 1)
    out = os.path.join(settings.workdir, "selbsttest", "bericht-%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, ensure_ascii=False, default=str)
    res["report"] = out
    log("Bericht: " + out)
    return res
