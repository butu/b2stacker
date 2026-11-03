"""Cached API: browsing and selection changes must not touch prepared originals."""
import json
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from astrostack import assets, previews, project
from astrostack.app import server
from astrostack.report import build as report_build
from astrostack.settings import Settings

from .conftest import NINA_LIGHT, make_fits


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    wd = tmp_path / "work"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workdir": str(wd)}), encoding="utf-8")
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(settings_file))
    pdir = wd / "projects" / "Test"
    (pdir / "analysis").mkdir(parents=True)
    json.dump({"roots": [], "target": "Test"}, open(pdir / "sources.json", "w"))
    return str(wd)


@contextmanager
def client():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def request(url, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _project(workdir):
    return os.path.join(workdir, "projects", "Test")


def _frame(workdir, path="/no/such/L1.fits"):
    json.dump([{"path": path, "type": "light", "filter": "Ha", "night": "2026-10-12", "excluded": False}],
              open(os.path.join(_project(workdir), "analysis", "frames.json"), "w"))


def _fake_media(monkeypatch, pdir, path="/no/such/L1.fits"):
    thumb = os.path.join(pdir, "analysis", "frame-thumbs", "thumb.jpg")
    screen = os.path.join(pdir, "analysis", "frame-thumbs", "screen.jpg")
    os.makedirs(os.path.dirname(thumb), exist_ok=True)
    open(thumb, "wb").write(b"\xff\xd8thumb")
    open(screen, "wb").write(b"\xff\xd8screen")
    media = {
        (path, "thumb"): dict(key="k-thumb", path=thumb, orig_width=6000, orig_height=4000, width=320, height=213),
        (path, "screen"): dict(key="k-screen", path=screen, orig_width=6000, orig_height=4000, width=1800, height=1200),
    }

    def fake(path_, analysis_dir, variant="screen", preview_cache_dir=None):
        assert analysis_dir == os.path.join(pdir, "analysis")
        return media.get((path_, variant))

    def fake_bulk(paths, analysis_dir, variants=("thumb", "screen", "full"), preview_cache_dir=None):
        return {p: {v: media.get((p, v)) for v in variants} for p in paths}

    monkeypatch.setattr(previews, "cached_media", fake)
    monkeypatch.setattr(previews, "cached_media_bulk", fake_bulk)
    monkeypatch.setattr(previews, "frame_preview", lambda *a, **k: (_ for _ in ()).throw(AssertionError("render")))
    monkeypatch.setattr(previews, "frame_thumbnail", lambda *a, **k: (_ for _ in ()).throw(AssertionError("render")))
    return media


def test_frames_and_media_ignore_original_io(workdir, monkeypatch, tmp_path):
    pdir = _project(workdir)
    _frame(workdir)
    _fake_media(monkeypatch, pdir)
    original = "/no/such/L1.fits"
    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(original):
            raise AssertionError("Original darf beim Browsen nicht gestattet werden")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    with client() as url:
        code, _, body = request(url, "/api/project/Test/frames")
        assert code == 200
        f = json.loads(body)["frames"][0]
        assert f["thumb_url"].startswith("/api/frame-thumb/Test?path=") and "v=k-thumb" in f["thumb_url"]
        assert f["preview_url"] and f["full_url"].startswith("/api/frame-full/")
        assert (f["width"], f["height"], f["preview_width"], f["preview_height"]) == (6000, 4000, 1800, 1200)
        assert f["media_ready"] is True and f["media_error"] is None

        code, headers, body = request(url, f["thumb_url"])
        assert code == 200 and body == b"\xff\xd8thumb"
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
        etag = headers["ETag"]
        code, _, body = request(url, f["thumb_url"], headers={"If-None-Match": etag})
        assert code == 304 and body == b""

        code, _, _ = request(url, "/api/frame-thumb/Test?path=/not/member")
        assert code == 403
        code, _, body = request(url, "/api/frame-preview/Test?path=/no/such/L1.fits&v=missing")
        assert code == 200, "prepared screen media is served"


def test_cached_media_path_allowlist_blocks_disclosure(workdir, monkeypatch):
    pdir = _project(workdir)
    _frame(workdir)
    leak = os.path.join(pdir, "report.html")
    open(leak, "w").write("secret")
    monkeypatch.setattr(previews, "cached_media",
                        lambda *a, **k: dict(key="k", path=leak))
    with client() as url:
        code, _, body = request(url, "/api/frame-thumb/Test?path=/no/such/L1.fits")
        assert code == 403 and b"secret" not in body


def test_workdir_raw_cache_is_not_a_media_root(workdir, monkeypatch):
    pdir = _project(workdir)
    _frame(workdir)
    raw = os.path.join(workdir, "cache", "catalog", "metrics.csv")
    os.makedirs(os.path.dirname(raw), exist_ok=True)
    open(raw, "w").write("path,nstars\n")
    monkeypatch.setattr(previews, "cached_media",
                        lambda *a, **k: dict(key="k", path=raw))
    with client() as url:
        code, _, body = request(url, "/api/frame-thumb/Test?path=/no/such/L1.fits")
        assert code == 403 and b"nstars" not in body


def test_unversioned_media_revalidates_instead_of_immutable(workdir, monkeypatch):
    pdir = _project(workdir)
    _frame(workdir)
    cached = os.path.join(pdir, "analysis", "frame-thumbs", "unversioned.jpg")
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    open(cached, "wb").write(b"\xff\xd8unversioned")
    monkeypatch.setattr(previews, "cached_media",
                        lambda *a, **k: dict(key=None, path=cached))
    with client() as url:
        code, headers, _ = request(url, "/api/frame-thumb/Test?path=/no/such/L1.fits")
        assert code == 200 and headers["Cache-Control"] == "no-cache, must-revalidate"
        etag = headers["ETag"]
        code, _, body = request(url, "/api/frame-thumb/Test?path=/no/such/L1.fits",
                                headers={"If-None-Match": etag})
        assert code == 304 and body == b""


def test_override_returns_small_delta_without_reloading_project(workdir, monkeypatch):
    delta = dict(revision=7, frame=dict(path="/no/such/L1.fits", selected=False),
                 facts=dict(lights_used=2), analysis_required=False, selection_ready=True)
    seen = {}

    def fake_set(pdir, path, value):
        seen["set"] = (pdir, path, value)
        return {"/no/such/L1.fits": value}

    monkeypatch.setattr(project, "set_override", fake_set)
    monkeypatch.setattr(project, "selection_delta", lambda pdir, path: delta)
    monkeypatch.setattr(server, "project_detail", lambda name: (_ for _ in ()).throw(AssertionError("full reload")))
    with client() as url:
        code, _, body = request(url, "/api/project/Test/override", method="POST",
                                body={"path": "/no/such/L1.fits", "value": "out"},
                                headers={"X-Astrostack": "1"})
        assert code == 200
        assert json.loads(body) == delta
        assert set(seen) == {"set"} and seen["set"][1:] == ("/no/such/L1.fits", "out")


def test_project_and_asset_preview_are_offline_cached(workdir, monkeypatch):
    pdir = _project(workdir)
    src = os.path.join(pdir, "final", "old.jpg")
    os.makedirs(os.path.dirname(src))
    Image.new("RGB", (40, 30), (10, 20, 30)).save(src)
    entry = assets.link(pdir, src)
    os.unlink(src)
    os.rmdir(os.path.dirname(src))
    assets.refresh(pdir)
    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(src):
            raise AssertionError("Offline-Original darf nicht gestattet werden")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    with client() as url:
        code, _, body = request(url, "/api/project/Test")
        assert code == 200
        detail = json.loads(body)
        a = next(x for x in detail["assets"] if x["id"] == entry["id"])
        assert a["preview_url"] and "&v=" in a["preview_url"] and "preview_error" not in a
        assert a["offline"] is True and a["exists"] is False
        code, headers, img = request(url, a["preview_url"])
        assert code == 200 and img.startswith(b"\xff\xd8")
        assert "immutable" in headers["Cache-Control"]


def test_report_route_uses_cached_html(workdir, monkeypatch):
    monkeypatch.setattr(report_build, "cached_html", lambda pdir: "<html>cached</html>")
    with client() as url:
        code, headers, body = request(url, "/p/Test/report.html")
        assert code == 200 and body == b"<html>cached</html>"
        assert headers["Content-Type"].startswith("text/html")


def test_prepare_previews_is_an_explicit_job(workdir, monkeypatch):
    seen = {}

    class FakeJob:
        def as_dict(self):
            return {"id": "j1", "kind": "previews", "state": "queued"}

    class FakeJobs:
        def submit(self, kind, title, wd, fn, project=None):
            seen.update(kind=kind, title=title, wd=wd, project=project)
            return FakeJob()

    monkeypatch.setattr(server, "JOBS", FakeJobs())
    monkeypatch.setattr(project, "frames", lambda pdir: [{"path": "/no/such/L1.fits", "type": "light"}])
    monkeypatch.setattr(server, "start_analysis", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scan")))
    out = server.start_prepare_previews("Test")
    assert out["kind"] == "previews"
    assert seen["kind"] == "previews" and seen["project"] == "Test"
    meta = json.load(open(os.path.join(_project(workdir), "analysis", "preview-request.json")))
    assert meta["paths"] == ["/no/such/L1.fits"]


def test_prepared_media_contract_is_served_without_source_touch(workdir, monkeypatch, tmp_path):
    pdir = _project(workdir)
    src = make_fits(str(tmp_path / "data" / "L1.fits"), NINA_LIGHT, shape=(64, 64))
    json.dump([{"path": src, "type": "light", "filter": "Ha", "night": "2026-10-12", "excluded": False}],
              open(os.path.join(pdir, "analysis", "frames.json"), "w"))
    previews.prepare_frames([{"path": src, "bayer": "RGGB"}], os.path.join(pdir, "analysis"))
    assert previews.missing_frames([{"path": src}], os.path.join(pdir, "analysis")) == []
    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(src):
            raise AssertionError("vorbereitete Medien dürfen das Original nicht mehr anfassen")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    with client() as url:
        code, _, body = request(url, "/api/project/Test/frames")
        assert code == 200
        f = json.loads(body)["frames"][0]
        assert f["media_ready"] is True
        assert (f["width"], f["height"]) == (64, 64)
        assert f["preview_width"] and f["preview_height"]
        assert request(url, f["thumb_url"])[0] == 200
        assert request(url, f["preview_url"])[0] == 200


def test_static_photoswipe_routes_are_exact(tmp_path, monkeypatch):
    vendor = tmp_path / "vendor" / "photoswipe"
    vendor.mkdir(parents=True)
    (vendor / "photoswipe.umd.min.js").write_text("window.PhotoSwipe=1", encoding="utf-8")
    (vendor / "photoswipe.css").write_text(".pswp{}", encoding="utf-8")
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    assert set(server.STATIC_FILES) == {("static", "photoswipe.js"), ("static", "photoswipe.css")}
    with client() as url:
        assert request(url, "/static/photoswipe.js")[0] == 200
        assert request(url, "/static/photoswipe.css")[0] == 200
        assert request(url, "/static/../settings.json")[0] == 404


def test_prepare_assets_route_works_without_lights_or_roots(workdir, monkeypatch):
    pdir = _project(workdir)
    src = os.path.join(pdir, "final", "only.jpg")
    os.makedirs(os.path.dirname(src))
    Image.new("RGB", (40, 30), (10, 20, 30)).save(src)
    seen = {}

    class StubJob:
        def set_progress(self, **_kw):
            pass

        def as_dict(self):
            return {"id": "a1", "kind": seen["kind"], "state": "done", "title": seen["title"]}

    class StubJobs:
        def active(self):
            return None

        def submit(self, kind, title, wd, fn, project=None):
            seen.update(kind=kind, title=title, wd=wd, project=project)
            fn(StubJob())
            return StubJob()

    monkeypatch.setattr(server, "JOBS", StubJobs())
    with client() as url:
        code, _, body = request(url, "/api/project/Test/prepare-assets", method="POST",
                                body={}, headers={"X-Astrostack": "1"})
        assert code == 200 and json.loads(body)["kind"] == "assets"
    assert seen["project"] == "Test" and "Bilder vorbereiten" in seen["title"]
    assert os.path.isfile(os.path.join(pdir, "analysis", "inventory-assets.json"))
    assert os.path.isfile(os.path.join(pdir, "analysis", "asset-previews", assets._key(src) + ".jpg"))

    real_stat = os.stat

    def guarded_stat(path, *a, **k):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(src):
            raise AssertionError("Assets-only GET darf das Original nicht staten")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    with client() as url:
        code, _, body = request(url, "/api/project/Test")
        assert code == 200
        a = json.loads(body)["assets"][0]
        assert a["preview_url"] and "preview_error" not in a
        assert request(url, a["preview_url"])[0] == 200


def test_frames_response_carries_selection_status_and_prepare_flag(workdir, monkeypatch):
    pdir = _project(workdir)
    _frame(workdir)
    monkeypatch.setattr(project, "selection_delta",
                        lambda pdir, path=None: dict(revision=9, frame=None, facts={},
                                                     analysis_required=False, selection_ready=True))
    with client() as url:
        code, _, body = request(url, "/api/project/Test/frames")
    assert code == 200
    data = json.loads(body)
    assert data["revision"] == 9 and data["selection_ready"] is True and data["analysis_required"] is False
    assert data["media_preparation_needed"] is True


@pytest.mark.parametrize("case", ["stack", "frame", "linked"])
def test_project_and_thumb_do_not_stat_external_sources(workdir, monkeypatch, tmp_path, case):
    pdir = _project(workdir)
    Image.new("RGB", (20, 20), (1, 2, 3)).save(os.path.join(pdir, "thumb.jpg"))
    external = tmp_path / "nas" / ("stack.xisf" if case == "stack" else "source.jpg" if case == "linked" else "L1.fits")
    if case == "stack":
        project.add_stack(pdir, dict(id="s1", run="Ha", date="2026-11-01T10:00:00", outputs=[
            dict(name="Ha", file=str(external), preview=str(external), exists=True)]))
    elif case == "frame":
        _frame(workdir, str(external))
    else:
        json.dump([str(external)], open(os.path.join(pdir, "assets.json"), "w"))

    real_stat = os.stat
    root = str(tmp_path / "nas")

    def guarded_stat(path, *a, **k):
        if os.path.abspath(os.fspath(path)).startswith(root):
            raise AssertionError("Browse/Thumb darf externe Quellen nicht staten")
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", guarded_stat)
    with client() as url:
        assert request(url, "/api/project/Test")[0] == 200
        assert request(url, "/api/project-thumb/Test")[0] == 200
        if case == "frame":
            assert request(url, "/api/project/Test/frames")[0] == 200
