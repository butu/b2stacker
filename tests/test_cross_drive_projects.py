"""Cross-drive references: assets stay usable and stack previews stay allow-listed."""
import json
import ntpath
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from astrostack import assets, project
from astrostack.app import server
from astrostack.settings import Settings


def _pdir(tmp_path):
    pdir = os.path.join(str(tmp_path), "work", "projects", "Test")
    for sub in ("final", "stacks", "analysis"):
        os.makedirs(os.path.join(pdir, sub))
    return pdir


@pytest.mark.parametrize("root, source", [
    (r"G:\b2stacker\projects\Test", r"F:\Astro\Test\stack.jpg"),
    (r"G:\b2stacker\projects\Test", r"\\nas\astro\Test\stack.jpg"),
    (r"\\nas\work\projects\Test", r"\\nas\photos\stack.jpg"),
])
def test_stored_cross_drive_reference_stays_absolute(monkeypatch, root, source):
    monkeypatch.setattr(assets, "os", SimpleNamespace(path=ntpath))
    assert assets._stored(root, source) == source


def test_stored_internal_reference_stays_relative(monkeypatch):
    monkeypatch.setattr(assets, "os", SimpleNamespace(path=ntpath))
    assert assets._stored(r"G:\b2stacker\projects\Test",
                          r"G:\b2stacker\projects\Test\stacks\stack.jpg") == "stacks/stack.jpg"


def test_missing_external_asset_is_metadata_not_an_error(tmp_path):
    pdir = _pdir(tmp_path)
    missing = os.path.join(str(tmp_path), "mounted", "gone.jpg")
    entry = assets._entry(pdir, missing, "linked")
    assert entry["exists"] is False and entry["path"] == missing


@pytest.fixture
def api(tmp_path, monkeypatch):
    pdir = _pdir(tmp_path)
    with open(os.path.join(pdir, "project.json"), "w", encoding="utf-8") as fh:
        json.dump({"schema": 2, "name": "Test", "target": "Test"}, fh)
    with open(os.path.join(pdir, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump({"target": "Test", "total_frames": 1, "options": {}, "runs": []}, fh)
    with open(os.path.join(pdir, "sources.json"), "w", encoding="utf-8") as fh:
        json.dump({"roots": [], "target": "Test"}, fh)
    ext = os.path.join(str(tmp_path), "mounted", "preview.jpg")
    os.makedirs(os.path.dirname(ext))
    with open(ext, "wb") as fh:
        fh.write(b"\xff\xd8external-preview")
    secret = os.path.join(str(tmp_path), "secret.jpg")
    with open(secret, "wb") as fh:
        fh.write(b"\xff\xd8secret")

    s = Settings(workdir=os.path.join(str(tmp_path), "work"))
    monkeypatch.setattr(server, "settings", lambda: s)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], pdir, ext, secret
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_registered_external_preview_is_served_unknown_is_forbidden(api):
    base, pdir, ext, secret = api
    project.add_stack(pdir, dict(id="s1", run="RGB", date="2026-11-01T10:00:00",
                                 outputs=[dict(name="RGB", preview=ext)]))
    status, body = _get(base + "/api/project/Test")
    assert status == 200, body
    detail = json.loads(body)
    url = detail["stacks"][0]["outputs"][0]["preview_url"]
    status, served = _get(base + url)
    assert status == 200 and served == open(ext, "rb").read()

    assert _get(base + "/api/stack-preview/Test?file=" + urllib.parse.quote(secret))[0] == 403
    assert _get(base + "/api/stack-preview/Test?file=" + urllib.parse.quote("../../secret.jpg"))[0] == 403


def test_missing_external_preview_keeps_project_open(api):
    base, pdir, _ext, _secret = api
    missing = os.path.join(str(os.path.dirname(pdir)), "mounted-missing.jpg")
    project.add_stack(pdir, dict(id="s2", run="RGB", date="2026-11-01T11:00:00",
                                 outputs=[dict(name="RGB", file=missing, preview=missing)]))
    status, body = _get(base + "/api/project/Test")
    assert status == 200, body
    detail = json.loads(body)
    assert detail["stacks"][0]["outputs"][0]["exists"] is False


def test_missing_linked_asset_keeps_project_open(api):
    base, pdir, ext, _secret = api
    missing = os.path.join(os.path.dirname(ext), "missing.jpg")
    with open(os.path.join(pdir, "assets.json"), "w", encoding="utf-8") as fh:
        json.dump([missing], fh)
    status, body = _get(base + "/api/project/Test")
    assert status == 200, body
    asset = json.loads(body)["assets"][0]
    assert asset["path"] == missing and asset["exists"] is False
    assert asset["preview_error"] == "Datei fehlt"
    assert _get(base + "/api/asset-preview/Test?id=" + asset["id"])[0] == 422


def test_project_detail_survives_mount_error(api, monkeypatch):
    base, pdir, ext, _secret = api
    project.add_stack(pdir, dict(id="s3", run="RGB", date="2026-11-01T12:00:00",
                                 outputs=[dict(name="RGB", preview=ext)]))

    class MountFailPath:
        def __getattr__(self, name):
            return getattr(os.path, name)

        def relpath(self, *args, **kwargs):
            raise ValueError("path is on mount 'F:', start on mount 'G:'")

    class FakeOS:
        path = MountFailPath()

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(server, "os", FakeOS())
    monkeypatch.setattr(project, "os", FakeOS())
    detail = server.project_detail("Test")
    preview_url = detail["stacks"][0]["outputs"][0]["preview_url"]
    assert urllib.parse.parse_qs(urllib.parse.urlparse(preview_url).query)["file"] == [ext.replace("\\", "/")]
