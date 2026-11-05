"""Neue Oberfläche, Teil 3: Frames löschen, Kalibrier-Bibliothek, Selbsttest/PixInsight/Einstellungen in der neuen App,
Sternmarkierung und Ausrichten mit Drehung."""
import os
import sys

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
        if sys.platform != "win32":                                  # Windows: echter Papierkorb
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


# ---------------------------------------------------------------- Kalibrier-Bibliothek

def _stack_ha(co, pid):
    plan = co.plan(pid)
    ha = next(x["id"] for x in plan["runs"] if x["filter"] == "HaO3")
    t = co.stack(pid, [ha])
    assert co.tasks.wait_idle(60)
    assert co.tasks.get(t["id"]).state == "done", co.tasks.get(t["id"]).error
    return co.tasks.get(t["id"]).result


def test_masters_are_shared_across_projects(proj, tmp_path, monkeypatch):
    co, pid, data = proj
    res = _stack_ha(co, pid)
    kept = res[0]["masters"]
    assert kept and not any(m["reused"] for m in kept)
    lib = co.calibration_library()
    assert {m["type"] for m in lib} >= {"bias", "flat"}
    b = next(m for m in lib if m["type"] == "bias")
    assert b["exists"] and b["size"] and b["stacks"] == 1 and b["info"]["frames"] >= 1 and b["info"]["gain"] is not None
    other = co.create_project("Zweites Projekt", [data])
    assert co.tasks.wait_idle(60)
    res2 = _stack_ha(co, other["id"])
    m2 = res2[0]["masters"]
    assert m2 and all(m["reused"] for m in m2)                       # Master aus dem ersten Projekt
    lib = co.calibration_library()
    assert len(lib) == len(kept)                                     # keine Kopie im zweiten Projekt
    b = next(m for m in lib if m["type"] == "bias")
    assert b["stacks"] == 2 and {p["name"] for p in b["projects"]} == {"vdB 131", "Zweites Projekt"}


def test_delete_master_from_library(proj):
    co, pid, _ = proj
    _stack_ha(co, pid)
    m = co.calibration_library()[0]
    co.delete_master(m["id"])
    assert not os.path.exists(m["path"]) and all(x["id"] != m["id"] for x in co.calibration_library())
    assert history.timeline(co.db, pid)[0]["kind"] == "master_deleted"
    res = _stack_ha(co, pid)                                         # entsteht neu aus den Rohframes
    assert any(not x["reused"] for x in res[0]["masters"])


def test_library_http_and_mcp(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    _stack_ha(co, pid)
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        st, lib, _ = call(srv, "GET", "/api/library")
        assert st == 200 and lib and lib[0]["info"]
        from .test_web_part2 import mcp_call
        out, err = mcp_call(srv, "calibration_library")
        assert not err and out["total"] == len(lib) and out["masters"][0]["frames"]
        st, r, _ = call(srv, "POST", "/api/library/%d/delete" % lib[0]["id"])
        assert st == 200
    finally:
        _stop(srv)


# ---------------------------------------------------------------- Selbsttest, PixInsight öffnen, Einstellungen per MCP

def test_selftest_runs_twice_and_reuses_masters(c, tmp_path):  # noqa: F811
    co, _ = c
    folder = tmp_path / "nas"
    folder.mkdir()
    from astrostack import handover
    main = handover.wbpp_main(co.pixinsight)
    os.makedirs(os.path.dirname(main))
    open(main, "w").write("// WBPP")
    t = co.selftest(str(folder))
    assert co.tasks.wait_idle(240)
    task = co.tasks.get(t["id"])
    assert task.state == "done", task.error
    res = task.result
    names = {s["name"]: s for s in res["steps"]}
    assert names["Ablauf (Entdecken bis Plan)"]["ok"], names
    assert names["Stacken – erster Durchlauf"]["ok"], names["Stacken – erster Durchlauf"]
    assert names["Master wiederverwendet"]["ok"]
    second = res["stacks"][1][0]["masters"]
    assert {m["kind"] for m in second if m["reused"]} >= {"bias", "dark", "flat"}, second
    assert res["ok"] and os.path.isfile(res["report"])
    assert os.path.isdir(folder / "b2stacker-selbsttest, mit Komma")
    assert co.projects() == []                                       # Testprojekt bleibt unsichtbar
    rep = co.selftest_reports()
    assert rep and rep[0]["ok"] and rep[0]["kind"] == "neu"


def test_open_in_pixinsight(proj):
    co, pid, _ = proj
    with pytest.raises(ValueError, match="erst stacken"):
        co.open_in_pixinsight(pid, popen=lambda cmd: None)
    _stack_ha(co, pid)
    calls = []
    r = co.open_in_pixinsight(pid, popen=calls.append)
    assert calls and calls[0][0] == co.pixinsight and r["opened"] and all(os.path.isfile(f) for f in r["opened"])


def test_mcp_settings_selftest_tools(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        from .test_web_part2 import mcp_call
        out, err = mcp_call(srv, "settings")
        assert not err and out["settings"]["workdir"] == co.ws.root and "mcp_token" not in out["settings"]
        out, err = mcp_call(srv, "settings", {"changes": {"analysis_workers": 3}})
        assert not err and out["settings"]["analysis_workers"] == 3 and co.workers == 3
        out, err = mcp_call(srv, "settings", {"changes": {"mcp_token": "x"}})
        assert err
        st, names, _ = call(srv, "POST", "mcp", {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                            headers={"Authorization": "Bearer " + __import__("tests.test_web", fromlist=["TOKEN"]).TOKEN})
        tools = {t["name"] for t in names["result"]["tools"]}
        assert {"selftest", "open_in_pixinsight", "settings", "calibration_library"} <= tools
        assert not any("delete" in t for t in tools)
    finally:
        _stop(srv)


# ---------------------------------------------------------------- Vergleich mit Drehung, Sternmarkierung

@pytest.mark.parametrize("deg", [3.0, -12.0, 178.0])
def test_rotation_between_finds_angle_and_side(deg):
    import numpy as np
    from scipy import ndimage as ndi

    from astrostack import stackview as sv

    from .test_web_part2 import star_image
    A = star_image(1, shape=(400, 600))
    B = np.stack([ndi.rotate(ch, deg, reshape=False, order=1, mode="nearest")
                  for ch in star_image(2, shape=(400, 600), shift=(5, -7))])
    ang, _conf = sv.rotation_between(A, B)
    assert ang is not None and abs(ang + deg) < 0.3
    assert sv.rotation_between(A, star_image(3, shape=(400, 600), shift=(4, 2)))[0] is None


def test_compare_aligns_rotated_stack(c, tmp_path):  # noqa: F811
    import numpy as np
    from scipy import ndimage as ndi

    from .test_web_part2 import real_masters, star_image
    co, data = c
    rot = np.stack([ndi.rotate(ch, 7.0, reshape=False, order=1, mode="nearest")
                    for ch in star_image(2, shift=(3.0, -4.0), noise=0.002)])
    co.stack_runner = real_masters(co.stack_runner, [star_image(1, noise=0.003), rot])
    r = co.create_project("Drehung", [data])
    assert co.tasks.wait_idle(60)
    for _ in range(2):
        _stack_ha(co, r["id"])
    st = co.stacks(r["id"])
    assert co.tasks.wait_idle(60)
    st = co.stacks(r["id"])
    out = co.compare(r["id"], st[1]["id"], st[0]["id"])
    al = out["align"]
    assert al["aligned"] and abs(al["rotation"] - 7.0) < 0.5, al


def test_frame_stars_endpoint(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        f = _lights(co, pid)[0]
        st, d, _ = call(srv, "GET", "/api/frame/%d/stars" % f["id"])
        assert st == 200 and d["width"] > 0 and d["height"] > 0 and isinstance(d["stars"], list)
        assert d["nstars"] >= len(d["stars"]) and all(len(x) == 5 for x in d["stars"])
        st, _d, _ = call(srv, "GET", "/api/frame/999999/stars")
        assert st == 404
    finally:
        _stop(srv)


def test_appclient_finds_new_app_and_stdio_mcp_forwards(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    from astrostack import appclient, mcp
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        port = srv.server_address[1]
        monkeypatch.delenv("B2_APP_URL", raising=False)
        monkeypatch.setattr(appclient, "PORTS", [port])
        assert appclient.find() == srv.url
        fwd = mcp.app_forwarder(appclient.Client(autostart=False))
        r = fwd({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert "calibration_library" in {t["name"] for t in r["result"]["tools"]}
    finally:
        _stop(srv)


def test_update_and_install_endpoints(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    from astrostack import install, updater
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        monkeypatch.setattr(updater, "check", lambda force=False: {"current": "0.10.2", "latest": "0.11.0",
                                                                   "newer": True, "can_install": False})
        st, r, _ = call(srv, "POST", "/api/update/check")
        assert st == 200 and r["newer"] and r["latest"] == "0.11.0"
        st, r, _ = call(srv, "GET", "/api/update")
        assert st == 200 and "current" in r
        st, r, _ = call(srv, "POST", "/api/update/install")
        assert st == 400 and "Quellcode" in r["error"]                     # nicht als exe gestartet
        st, r, _ = call(srv, "POST", "/api/install")
        assert st == 400 and "exe" in r["error"]
        monkeypatch.setattr(install, "install", lambda quit_fn=None: dict(text="Installiert nach X"))
        st, r, _ = call(srv, "POST", "/api/install")
        assert st == 200 and r["text"].startswith("Installiert")
    finally:
        _stop(srv)


@pytest.mark.stack_options
def test_stack_options_ram_limit_and_open_results(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    import json as _json

    from astrostack import system
    sf = tmp_path / "s.json"
    sf.write_text(_json.dumps({"workdir": co.ws.root, "min_free_ram_gb": 64, "open_results": True,
                               "keep_awake": False}), encoding="utf-8")
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(sf))
    monkeypatch.setattr(system, "ram_gb", lambda: (3.5, 16.0))
    with pytest.raises(ValueError, match="3,5|3.5 GB"):
        co.stack(pid)
    monkeypatch.setattr(system, "ram_gb", lambda: (70.0, 128.0))
    opened = []
    co.result_opener = opened.append
    _stack_ha(co, pid)
    assert opened and opened[0][0] == co.pixinsight
