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
    assert names["Einstellungen von Hand (Sternerkennung, Rejection)"]["ok"], names
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


# ---------------------------------------------------------------- Handy im Heimnetz

def _lan_call(port, method, path, body=None, cookie=None, follow=False):
    import http.client
    import json as _json
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if method == "POST":
        h["X-Astrostack"] = "1"
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body=_json.dumps(body or {}).encode() if method == "POST" else None, headers=h)
    r = c.getresponse()
    data = r.read()
    try:
        data = _json.loads(data or b"null")
    except ValueError:
        pass
    return r.status, data, dict(r.getheaders())


def test_lan_pairing_and_restrictions(proj, tmp_path, monkeypatch):
    co, pid, _ = proj
    from astrostack.web import lan
    from astrostack.web import server as web
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    s.lan_port = 0
    s.save()
    srv = _serve(co, s)
    try:
        info = web.set_lan(srv, True)
        assert info["enabled"] and info["running"]
        port = srv.lan_server.server_address[1]
        key = web.Settings.load().lan_key
        assert key and "lan_key" not in web.public_settings(web.Settings.load())
        st, _d, _ = _lan_call(port, "GET", "/api/projects")
        assert st == 401                                                     # nicht gekoppelt
        assert _lan_call(port, "GET", "/pair?k=falsch")[0] == 401
        st, _d, h = _lan_call(port, "GET", "/pair?k=" + key)
        assert st == 302 and "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
        ck = h["Set-Cookie"].split(";")[0]
        st, boot, _ = _lan_call(port, "GET", "/api/bootstrap", cookie=ck)
        assert st == 200 and boot["remote"] is True and "lan_key" not in boot["settings"]
        st, html, _ = _lan_call(port, "GET", "/", cookie=ck)
        assert st == 200
        f = [x for x in _lights(co, pid) if x["status"] == "use"][0]
        st, r, _ = _lan_call(port, "POST", "/api/projects/%d/decide" % pid,
                             {"file_ids": [f["id"]], "decision": "delete"}, cookie=ck)
        assert st == 200                                                     # vormerken geht
        for path, body in (("/api/projects/%d/delete-frames" % pid, {"file_ids": [f["id"]], "confirm_count": 1}),
                           ("/api/settings", {"analysis_workers": 3}), ("/api/quit", {}), ("/api/lan", {"enabled": False}),
                           ("/api/mcp-info", {}), ("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                           ("/api/backups/restore", {"name": "x"}), ("/api/library/1/delete", {})):
            assert _lan_call(port, "POST", path, body, cookie=ck)[0] == 403, path
        assert os.path.isfile(f["path"])
        # lokal: Info mit QR-Code, neuer Schlüssel meldet ab
        st, li, _ = call(srv, "POST", "/api/lan-info")
        assert st == 200 and li["running"] and li["qr"] is None or li["qr"].startswith("<svg")
        st, li, _ = call(srv, "POST", "/api/lan-key")
        assert st == 200 and _lan_call(port, "GET", "/api/projects", cookie=ck)[0] == 401
        st, li, _ = call(srv, "POST", "/api/lan", {"enabled": False})
        assert st == 200 and not li["running"] and srv.lan_server is None
    finally:
        _stop(srv)
    assert lan.private_client("192.168.1.20") and lan.private_client("10.0.0.5") and lan.private_client("fe80::1")
    assert not lan.private_client("8.8.8.8") and not lan.private_client("2a00:1450::1")
    assert lan.qr_svg("http://192.168.1.20:8875/pair?k=abc").lstrip().startswith("<svg")


def test_failed_stack_explains_cause_and_keeps_log(proj, monkeypatch):
    co, pid, _ = proj
    monkeypatch.setenv("FAKE_MODE", "nomaster")
    plan = co.plan(pid)
    ha = next(x["id"] for x in plan["runs"] if x["filter"] == "HaO3")
    t = co.stack(pid, [ha])
    assert co.tasks.wait_idle(60)
    res = co.tasks.get(t["id"]).result[0]
    assert res["status"] == "failed"
    assert "kein Light für die Integration" in res["error"] and "keine Light-Gruppe" in res["error"], res["error"]
    st = co.stacks(pid)[0]
    assert st["status"] == "failed" and st["folder"].endswith("fehlgeschlagen")
    pdir = co.ws.project_dir(co.db.value("SELECT folder FROM projects WHERE id = ?", (pid,)))
    prot = os.path.join(pdir, st["folder"], "protokoll")
    assert sorted(os.listdir(prot)) == ["20261105120000.log", "b2-result.json", "b2-status.json", "b2job.json"]
    opened = []
    assert co.open_stack_folder(pid, st["id"], opener=opened.append) == os.path.join(pdir, st["folder"])
    assert opened
    co.delete_stack(pid, st["id"])                                     # Protokoll-Ordner verschwindet mit
    assert not os.path.exists(os.path.join(pdir, st["folder"]))


# ---------------------------------------------------------------- Nachtübersicht, Rig-Hinweise, Bericht

def test_insights_nights_rig_and_report(proj):
    co, pid, _ = proj
    ins = co.insights(pid)
    assert ins["frames"] > 0 and ins["nights"] and ins["kpi"]
    n = ins["nights"][0]
    assert {"night", "filter", "hours", "used_h", "grade", "why", "todo", "moon"} <= set(n)
    assert isinstance(ins["checks"], list)
    for chk in ins["checks"]:
        assert chk["level"] in ("good", "warning", "critical", "info") and chk["title"] and chk["text"]
    again = co.insights(pid)                                      # zwischengespeichert
    assert again["nights"] == ins["nights"]
    used = [f for f in _lights(co, pid) if f["status"] == "use"]
    co.decide(pid, [f["id"] for f in used], "skip")               # Entscheidung → neu gerechnet
    assert sum(x["n_used"] for x in co.insights(pid)["nights"]) == 0
    html = co.report_html(pid)
    assert html.startswith("<!") and "vdB 131" in html and "/*__DATA__*/null" not in html


# ---------------------------------------------------------------- Stacks übernehmen

def test_import_old_and_pixinsight_stacks(proj, tmp_path):
    import json as _json

    import numpy as np

    from astrostack import xisf
    co, pid, _ = proj
    rng = np.random.default_rng(1)

    def master(path, filt, n=20):
        img = (rng.random((3, 120, 160)) * 0.02 + 0.05).astype(np.float32)
        for y, x in ((30, 40), (80, 120), (60, 70), (20, 140)):
            img[:, y - 2:y + 3, x - 2:x + 3] += 0.6
        os.makedirs(os.path.dirname(path), exist_ok=True)
        xisf.write_image(path, img, {"FILTER": filt, "NCOMBINE": n, "EXPTIME": 600.0, "DATE": "2026-08-01T22:00:00"})
        return path
    # 1) WBPP-Ausgabe: masterLight + Autocrop-Variante (nur eine zählt), Zwischenordner werden ignoriert
    wb = tmp_path / "WBPP"
    master(str(wb / "master" / "masterLight_BIN-1_6224x4168_FILTER-Ha_mono.xisf"), "Ha")
    master(str(wb / "master" / "masterLight_BIN-1_6224x4168_FILTER-Ha_mono_autocrop.xisf"), "Ha")
    master(str(wb / "registered" / "masterLight_x.xisf"), "Ha")
    # 2) 0.10-Projektordner mit stacks.json
    old = tmp_path / "alt"
    f = master(str(old / "stacks" / "2026-07-01" / "RGB.xisf"), "RGB")
    (old / "stacks.json").write_text(_json.dumps([dict(id="a", date="2026-07-01T23:00:00Z", hours=4.5, note="erster Versuch",
                                                       outputs=[dict(name="RGB", file=os.path.relpath(f, old))])]))
    res = co.import_stacks(pid, [str(wb), str(old)])
    assert len(res["added"]) == 2 and res["found"] == 2
    assert co.tasks.wait_idle(60)
    st = {s["run_label"]: s for s in co.stacks(pid, measure=False)}
    assert set(st) == {"Ha", "RGB"}
    ha, rgb = st["Ha"], st["RGB"]
    assert ha["imported"] and ha["frames"] == 20 and abs(ha["hours"] - 20 * 600 / 3600) < 0.01
    assert rgb["note"] == "erster Versuch" and rgb["hours"] == 4.5 and rgb["created_at"].startswith("2026-07-01")
    assert ha["preview_url"] and ha["quality"] and not ha["quality"].get("error")    # Vorschau + Qualität
    again = co.import_stacks(pid, [str(wb)])                                           # doppelt: übersprungen
    assert again["added"] == [] and again["skipped"][0]["why"] == "schon übernommen"
    src = ha["files"][0]
    co.delete_stack(pid, ha["id"])                                                     # Eintrag weg, Datei bleibt
    assert os.path.isfile(src) and os.path.isdir(os.path.dirname(src))


def test_claude_desktop_link_endpoints(proj, tmp_path, monkeypatch):
    import json as _json
    co, pid, _ = proj
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    cfg.write_text(_json.dumps({"mcpServers": {"andere": {"command": "x"}}}))
    monkeypatch.setenv("B2_CLAUDE_CONFIG", str(cfg))
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        st, r, _ = call(srv, "GET", "/api/claude")
        assert st == 200 and r["claude_found"] and not r["installed"]
        st, r, _ = call(srv, "POST", "/api/claude/install")
        assert st == 200 and r["installed"] and r["current"]
        d = _json.loads(cfg.read_text())
        assert "andere" in d["mcpServers"] and d["mcpServers"]["b2stacker"]["args"][-1] == "mcp"
        st, r, _ = call(srv, "POST", "/api/claude/uninstall")
        assert st == 200 and not r["installed"]
    finally:
        _stop(srv)


def test_lan_vpn_addresses_and_windows_firewall_diagnosis(monkeypatch):
    import json as _json
    import sys as _sys

    from astrostack.web import lan
    assert lan.private_client("100.101.5.7")                      # Tailscale/CGNAT
    assert lan.private_client("10.8.0.2") and lan.private_client("fd00::5")
    assert not lan.private_client("8.8.8.8") and not lan.private_client("100.128.0.1")
    monkeypatch.setattr(_sys, "platform", "win32")
    rules = [dict(name="b2stacker", enabled="True", dir="Inbound", action="Block", profile="Public", proto="TCP",
                  port="Any"),
             dict(name="b2stacker", enabled="True", dir="Inbound", action="Allow", profile="Private", proto="TCP",
                  port="Any")]
    nets = [dict(name="Zuhause", alias="WLAN", category="Public")]
    monkeypatch.setattr(lan, "_ps", lambda s, timeout=20: "[]" if ("FirewallProduct" in s or "NetIPAddress" in s)
                        else _json.dumps(rules if "Firewall" in s else nets))
    st = lan.firewall_status("C:/x/b2stacker.exe", 8875)
    assert not st["ok"] and any("sperrt" in p for p in st["problems"])
    assert any("Öffentlich" in p for p in st["problems"])
    rules[:] = [dict(name=lan.FW_RULE, enabled="True", dir="Inbound", action="Allow", profile="Any", proto="TCP",
                     port="8875")]
    assert lan.firewall_status("C:/x/b2stacker.exe", 8875)["ok"]
    monkeypatch.setattr(_sys, "platform", "linux")
    assert lan.firewall_status("x", 8875) is None


def test_insights_rows_have_fields_of_old_report(proj, monkeypatch):
    """0.12.0 auf echten Daten: „Rig-Hinweise konnten nicht berechnet werden: 'frame'“ – die alte Diagnose braucht
    Felder aus metrics.csv, die die neuen Messwerte anders nennen."""
    from astrostack.flow import insights
    from astrostack.report import diag
    co, pid, _ = proj
    seen = {}

    def fake(rows, scale, thumbs):
        seen.update(keys=set().union(*(r.keys() for r in rows)), thumb=thumbs(rows[0]))
        return dict(checks=[dict(level="good", area="Test", title="ok", text="ok")])
    monkeypatch.setattr(diag, "rig_diagnostics", fake)
    insights.forget()
    d = co.insights(pid)
    assert {"frame", "file", "pier", "status", "night", "t"} <= seen["keys"]
    assert seen["thumb"].endswith(".npy") and d["checks"][0]["title"] == "ok"
    assert not any("nicht berechnet" in r for r in d["rec"])


def test_lan_connection_test_page_and_arrivals(proj, tmp_path, monkeypatch):
    """Eingebauter Verbindungstest: /test geht ohne Koppeln und wird als „angekommen“ vermerkt."""
    co, pid, _ = proj
    from astrostack.web import lan
    from astrostack.web import server as web
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    s.lan_port = 0
    s.save()
    srv = _serve(co, s)
    try:
        info = web.set_lan(srv, True)
        port = srv.lan_server.server_address[1]
        assert all(u.endswith("/test") for u in info["test_urls"])
        lan.LanServer.seen.clear()
        st, body, _ = _lan_call(port, "GET", "/test")
        assert st == 200 and "klappt" in (body.decode("utf-8") if isinstance(body, bytes) else str(body))
        assert "127.0.0.1" in lan.LanServer.seen                       # Ankunft vermerkt
        assert _lan_call(port, "GET", "/api/projects")[0] == 401        # alles andere bleibt gesperrt
    finally:
        web.set_lan(srv, False)
        _stop(srv)


def test_plan_shows_master_reuse(proj):
    """Feedback 0.12.2: Der Plan zeigt, welcher Master wiederverwendet wird – oder warum nicht."""
    co, pid, _ = proj
    plan = co.plan(pid)
    ha = next(r for r in plan["runs"] if r["filter"] == "HaO3")
    assert ha["masters"] and all(not m["reused"] for m in ha["masters"])
    _stack_ha(co, pid)
    ha2 = next(r for r in co.plan(pid)["runs"] if r["filter"] == "HaO3")
    reused = [m for m in ha2["masters"] if m["reused"]]
    assert reused and all("wiederverwendet" in m["why"] and "vdB 131" in m["why"] for m in reused)
