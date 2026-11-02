import csv
import os
import re
import shutil

import numpy as np

from astrostack import analyze, calibcheck, discover
from tests.conftest import NINA_LIGHT, make_fits


def _light(path, seed, header=None):
    rng = np.random.default_rng(seed)
    arr = rng.normal(1000, 10, (64, 96))
    arr[20, 30] += 3000
    arr[40, 60] += 2500
    make_fits(path, header or NINA_LIGHT, data=np.clip(arr, 0, 65535).astype(np.uint16))
    return path


def _touch_newer(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def _run(roots, outdir, cat, opt=None):
    """Scan + Analyse wie das Backend. Liefert (Log-Zeilen, Inventar)."""
    opt = opt or discover.ScanOptions(catalog_dir=cat)
    inv = discover.scan(roots, opt)
    frames = [f for g in inv.lights for f in g.frames]
    logs = []
    analyze.analyze_group(frames, outdir, workers=1, catalog_dir=cat, log=logs.append)
    return logs, inv


def _counts(logs):
    line = next(x for x in logs if x.startswith("Katalog:"))
    m = re.search(r"(\d+) aus Projekt-CSV, (\d+) aus Katalog, (\d+) neu, (\d+) geändert", line)
    assert m, line
    return dict(zip(("csv", "cat", "new", "changed"), map(int, m.groups())))


def _rows(outdir):
    with open(os.path.join(outdir, "metrics.csv"), encoding="utf-8") as fh:
        return {os.path.basename(r["path"]): r for r in csv.DictReader(fh)}


def _errors(outdir):
    import json
    with open(os.path.join(outdir, "measure_errors.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_measure_errors_json_full_paths_and_cleared_next_run(tmp_path):
    from astrostack import fitsmeta as fm
    x = tmp_path / "x" / "a.fits"          # gleicher Basename, Ordner x: nicht lesbar (fehlt)
    y = tmp_path / "y" / "a.fits"          # gleicher Basename, Ordner y: kaputter Inhalt
    y.parent.mkdir(parents=True)
    y.write_bytes(b"not a fits file")
    frames = [fm.FrameMeta(path=str(x)), fm.FrameMeta(path=str(y))]
    out = str(tmp_path / "out")
    analyze.analyze_group(frames, out, workers=1)
    by = {e["path"]: e for e in _errors(out)}
    assert set(by) == {str(x), str(y)}
    assert by[str(x)]["kind"] == "stat" and by[str(x)]["reason"]
    assert by[str(y)]["kind"] == "measure" and by[str(y)]["reason"]
    # Nächster Lauf: beide Dateien behoben → keine alten Fehler mehr in der Datei
    _light(str(x), 1)
    _light(str(y), 2)
    analyze.analyze_group(frames, out, workers=1)
    assert _errors(out) == []


def test_flat_stats_not_stored_under_old_hash_when_file_changes(tmp_path):
    import hashlib
    from astrostack import catalog
    p = str(tmp_path / "flat.fits")
    with open(p, "wb") as fh:
        fh.write(b"OLD CONTENT")
    cat = catalog.Catalog(str(tmp_path / "cache" / "catalog"))
    cache = calibcheck._FlatStatsCache(cat, {p: None})
    assert p not in cache                          # hashes OLD content, miss
    with open(p, "wb") as fh:                      # acquisition overwrites the flat before the store
        fh.write(b"NEW CONTENT LONGER")
    cache[p] = {"path": p, "peak": 1.0}
    old_sha = hashlib.sha256(b"OLD CONTENT").hexdigest()
    assert cat.flat_get(old_sha, None) is None


def test_measure_version_bump_invalidates_catalog_and_csv(tmp_path, monkeypatch):
    from astrostack import catalog
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 12)
    out = str(tmp_path / "out")
    _run([r], out, cat)
    monkeypatch.setattr(catalog, "LIGHT_VERSION", catalog.LIGHT_VERSION + 1)
    logs, _ = _run([r], out, cat)
    c = _counts(logs)
    assert c["csv"] == 0 and c["cat"] == 0 and c["new"] + c["changed"] == 1


def test_flat_version_bump_invalidates_only_flat_stats(tmp_path, monkeypatch):
    from astrostack import catalog
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(2):
        _light(f"{r}/FLAT/f{i}.fits", 30 + i, hf)
    inv = discover.scan([r], discover.ScanOptions(catalog_dir=cat))
    calls = []
    real = calibcheck.flat_frame_stats
    monkeypatch.setattr(calibcheck, "flat_frame_stats", lambda p, b: (calls.append(p), real(p, b))[1])
    calibcheck.check_all(inv, catalog_dir=cat)
    n = len(calls)
    calibcheck.check_all(inv, catalog_dir=cat)
    assert len(calls) == n                       # warm: no re-measure
    monkeypatch.setattr(catalog, "FLAT_VERSION", catalog.FLAT_VERSION + 1)
    calibcheck.check_all(inv, catalog_dir=cat)
    assert len(calls) == 2 * n                   # bumped flat version re-measures all flats


def test_cache_write_failure_does_not_abort_analysis(tmp_path, monkeypatch):
    from astrostack import catalog
    def no_space(path, write):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(catalog, "_atomic_write", no_space)
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 40)
    _light(f"{r}/b.fits", 41)
    out = str(tmp_path / "out")
    logs, _ = _run([r], out, cat)
    assert _counts(logs)["new"] == 2
    assert set(_rows(out)) == {"a.fits", "b.fits"}
    assert _errors(out) == []
    assert any("Schreibfehler" in x for x in logs)


def test_unusable_catalog_dir_is_ignored(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 42)
    out = str(tmp_path / "out")
    logs, inv = _run([r], out, str(blocker / "catalog"))
    assert _counts(logs)["new"] == 1
    assert len(inv.lights) == 1


def test_atomic_write_removes_tmp_on_failure(tmp_path):
    import pytest
    from astrostack import catalog
    target = str(tmp_path / "x.json")
    def partial_then_fail(fh):
        fh.write(b"partial")
        raise OSError(28, "No space left on device")
    with pytest.raises(OSError):
        catalog._atomic_write(target, partial_then_fail)
    assert os.listdir(tmp_path) == []


def test_normalize_roots_canonical(tmp_path):
    r = tmp_path / "data"
    r.mkdir()
    link = tmp_path / "link"
    os.symlink(r, link)
    assert discover.normalize_roots([str(r), str(r) + os.sep, str(link)]) == [os.path.realpath(r)]


def test_overlapping_roots_yield_each_file_once(tmp_path):
    r = str(tmp_path / "data")
    _light(f"{r}/sub/a.fits", 1)
    files = discover.find_files([r, f"{r}/sub"], discover.ScanOptions())
    assert len(files) == 1


def test_summary_hours_total_includes_excluded(tmp_path):
    r = str(tmp_path / "data")
    h = dict(NINA_LIGHT, EXPTIME=600.0)
    make_fits(f"{r}/x/2026-10-12/HaO3/a.fits", h)
    make_fits(f"{r}/x/_rejected/2026-10-12/HaO3/b.fits", h)
    g = discover.scan([r]).lights[0].summary()
    assert g["hours"] == 0.17 and g["hours_total"] == 0.33


def test_two_projects_same_content_reuse_catalog(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    r1, r2 = str(tmp_path / "p1"), str(tmp_path / "p2")
    _light(f"{r1}/a.fits", 7)
    logs1, _ = _run([r1], str(tmp_path / "out1"), cat)
    assert _counts(logs1)["new"] == 1
    os.makedirs(f"{r2}/copy", exist_ok=True)
    shutil.copy(f"{r1}/a.fits", f"{r2}/copy/a.fits")
    out2 = str(tmp_path / "out2")
    logs2, _ = _run([r2], out2, cat)
    c = _counts(logs2)
    assert c["cat"] == 1 and c["new"] == 0
    assert os.path.exists(os.path.join(out2, "thumbs", analyze.thumb_name(f"{r2}/copy/a.fits")))
    assert _rows(out2)["a.fits"]["nstars"] == _rows(str(tmp_path / "out1"))["a.fits"]["nstars"]


def test_changed_file_same_path_is_remeasured(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    p = _light(f"{r}/a.fits", 3)
    out = str(tmp_path / "out")
    _run([r], out, cat)
    old_bg = _rows(out)["a.fits"]["bg"]
    _light(p, 99)
    _touch_newer(p)
    logs, _ = _run([r], out, cat)
    c = _counts(logs)
    assert c["changed"] == 1 and c["new"] == 0 and c["csv"] == 0
    assert _rows(out)["a.fits"]["bg"] != old_bg


def test_removed_file_not_kept_in_csv(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 1)
    _light(f"{r}/b.fits", 2)
    out = str(tmp_path / "out")
    _run([r], out, cat)
    assert set(_rows(out)) == {"a.fits", "b.fits"}
    os.remove(f"{r}/b.fits")
    _run([r], out, cat)
    assert set(_rows(out)) == {"a.fits"}


def test_pathonly_csv_without_signature_not_trusted(tmp_path):
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 4)
    out = str(tmp_path / "out")
    _run([r], out, None)
    p = os.path.join(out, "metrics.csv")
    with open(p, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["bg"] = "999.0"
        row.pop("file_sig", None)
        row.pop("sha256", None)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    logs, _ = _run([r], out, None)
    c = _counts(logs)
    assert c["csv"] == 0 and c["new"] + c["changed"] == 1
    assert _rows(out)["a.fits"]["bg"] != "999.0"


def test_changed_file_without_catalog_is_remeasured(tmp_path):
    r = str(tmp_path / "p")
    p = _light(f"{r}/a.fits", 5)
    out = str(tmp_path / "out")
    _run([r], out, None)
    old_bg = _rows(out)["a.fits"]["bg"]
    _light(p, 55)
    _touch_newer(p)
    logs, _ = _run([r], out, None)
    assert _counts(logs)["changed"] == 1
    assert _rows(out)["a.fits"]["bg"] != old_bg


def test_context_is_not_cached_across_scans(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    hdr = dict(NINA_LIGHT)
    hdr.pop("IMAGETYP")
    hdr.pop("OBJECT")
    r1, r2 = str(tmp_path / "a"), str(tmp_path / "b")
    _light(f"{r1}/Darks/d.fits", 8, hdr)
    os.makedirs(f"{r2}/Lights", exist_ok=True)
    shutil.copy(f"{r1}/Darks/d.fits", f"{r2}/Lights/d.fits")
    s1 = discover.scan([r1], discover.ScanOptions(catalog_dir=cat)).summary()
    s2 = discover.scan([r2], discover.ScanOptions(catalog_dir=cat)).summary()
    assert [c["type"] for c in s1["calib"]] == ["dark"]
    assert s1["lights"] == []
    assert len(s2["lights"]) == 1
    assert s2["calib"] == []


def test_custom_filters_not_contaminated(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 6)
    a = discover.scan([r], discover.ScanOptions(catalog_dir=cat, custom_filters={"HaO3": "custom-kind"}))
    b = discover.scan([r], discover.ScanOptions(catalog_dir=cat))
    assert a.lights[0].frames[0].filter_kind == "custom-kind"
    assert b.lights[0].frames[0].filter_kind != "custom-kind"


def test_corrupt_cache_entries_are_ignored_and_repaired(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    _light(f"{r}/a.fits", 9)
    out = str(tmp_path / "out")
    _run([r], out, cat)
    ref = _rows(out)["a.fits"]["bg"]
    for root, _, files in os.walk(cat):
        for fn in files:
            with open(os.path.join(root, fn), "wb") as fh:
                fh.write(b"{broken")
    out2 = str(tmp_path / "out2")
    logs, inv = _run([r], out2, cat)
    assert _counts(logs)["cat"] == 0 and _counts(logs)["new"] == 1
    assert _rows(out2)["a.fits"]["bg"] == ref
    assert len(inv.lights) == 1


def test_flat_stats_cached_and_reused(tmp_path):
    cat = str(tmp_path / "cache" / "catalog")
    r = str(tmp_path / "p")
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0)
    hf.pop("OBJECT")
    for i in range(3):
        _light(f"{r}/FLAT/f{i}.fits", 20 + i, hf)
    inv = discover.scan([r], discover.ScanOptions(catalog_dir=cat))
    res1 = calibcheck.check_all(inv, catalog_dir=cat)
    flat_items = [f for _, _, fs in os.walk(cat) for f in fs if "flatstats" in f]
    assert len(flat_items) == 3
    res2 = calibcheck.check_all(inv, catalog_dir=cat)
    assert res1["flat_checks"] == res2["flat_checks"]
