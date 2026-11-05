"""Neue Oberfläche, Teil 3: Frames löschen, Kalibrier-Bibliothek, Selbsttest/PixInsight/Einstellungen in der neuen App,
Sternmarkierung und Ausrichten mit Drehung."""
import os

import pytest

from astrostack import history, trash

from .test_core import c  # noqa: F401
from .test_flow import env  # noqa: F401
from .test_web import _serve, _settings, _stop, call


@pytest.fixture
def proj(c, tmp_path, monkeypatch):  # noqa: F811
    co, data = c
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    r = co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    return co, r["id"], data


def _lights(co, pid):
    return co.frames(pid)["frames"]


# ---------------------------------------------------------------- Frames löschen

def test_mark_for_deletion_is_separate_from_skip(proj):
    co, pid, _ = proj
    fr = _lights(co, pid)
    used = [f for f in fr if f["status"] == "use"]
    co.decide(pid, [used[0]["id"]], "delete")
    f = next(x for x in _lights(co, pid) if x["id"] == used[0]["id"])
    assert f["status"] == "delete" and f["origin"] == "manual" and f["reason"] == "zum Löschen vorgemerkt"
    assert os.path.isfile(f["path"])                                  # vormerken löscht nichts
    plan = co.plan(pid)
    assert all(used[0]["id"] not in r["light_ids"] for r in plan["runs"])
    d = co.deletion_list(pid)
    assert d["count"] == 1 and d["bytes"] > 0 and d["frames"][0]["trash"] is True and not d["frames"][0]["blocked"]
    co.decide(pid, [used[0]["id"]], "auto")
    assert co.deletion_list(pid)["count"] == 0


def test_delete_needs_matching_confirmation_and_goes_to_trash(proj, tmp_path):
    co, pid, _ = proj
    fr = [f for f in _lights(co, pid) if f["status"] == "use"][:2]
    ids = [f["id"] for f in fr]
    co.decide(pid, ids, "delete")
    with pytest.raises(ValueError, match="Auswahl hat sich geändert"):
        co.delete_frames(pid, ids, confirm_count=1)
    with pytest.raises(ValueError, match="nur in der Oberfläche"):
        co.delete_frames(pid, ids, confirm_count=2, origin="mcp")
    r = co.delete_frames(pid, ids, confirm_count=2)
    assert r["deleted"] == 2 and r["trash"] == 2 and r["permanent"] == 0 and not r["failed"]
    for f in fr:
        assert not os.path.exists(f["path"])
        assert os.path.isfile(tmp_path / "xdg" / "Trash" / "files" / os.path.basename(f["path"]))
    assert all(f["id"] not in ids for f in _lights(co, pid))
    tl = history.timeline(co.db, pid)
    assert tl[0]["kind"] == "frames_deleted" and "2 Frames gelöscht" in tl[0]["title"]
    assert "2 in den Papierkorb" in tl[0]["detail"]


def test_delete_without_trash_requires_explicit_permanent(proj, monkeypatch):
    co, pid, _ = proj
    f = [x for x in _lights(co, pid) if x["status"] == "use"][0]
    co.decide(pid, [f["id"]], "delete")
    monkeypatch.setattr(trash, "can_trash", lambda p: False)
    d = co.deletion_list(pid)
    assert d["permanent"] == 1
    with pytest.raises(ValueError, match="ohne Papierkorb"):
        co.delete_frames(pid, [f["id"]], confirm_count=1)
    r = co.delete_frames(pid, [f["id"]], confirm_count=1, allow_permanent=True)
    assert r["permanent"] == 1 and not os.path.exists(f["path"])


def test_file_used_by_another_project_is_blocked(proj):
    co, pid, data = proj
    other = co.create_project("Zweites", [data])
    assert co.tasks.wait_idle(60)
    f = [x for x in _lights(co, pid) if x["status"] == "use"][0]
    co.decide(pid, [f["id"]], "delete")
    d = co.deletion_list(pid)
    assert d["count"] == 0 and "Zweites" in d["frames"][0]["blocked"]
    with pytest.raises(ValueError):
        co.delete_frames(pid, [f["id"]], confirm_count=1)
    co.decide(other["id"], [f["id"]], "delete")                      # dort auch vorgemerkt → frei
    assert co.deletion_list(pid)["count"] == 1


def test_http_deletion_and_mcp_cannot_mark(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        f = [x for x in _lights(co, pid) if x["status"] == "use"][0]
        st, r, _ = call(srv, "POST", "/api/projects/%d/decide" % pid, {"file_ids": [f["id"]], "decision": "delete"})
        assert st == 200
        st, d, _ = call(srv, "GET", "/api/projects/%d/deletion" % pid)
        assert st == 200 and d["count"] == 1
        st, r, _ = call(srv, "POST", "/api/projects/%d/delete-frames" % pid, {"file_ids": [f["id"]]})
        assert st == 400
        st, r, _ = call(srv, "POST", "/api/projects/%d/delete-frames" % pid,
                        {"file_ids": [f["id"]], "confirm_count": 1})
        assert st == 200 and r["deleted"] == 1
        from .test_web_part2 import mcp_call
        out, err = mcp_call(srv, "set_frames", {"project": pid, "frames": [f["id"]], "decision": "delete"})
        assert err
    finally:
        _stop(srv)
