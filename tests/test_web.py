"""Neuer HTTP-Server (astrostack/web) auf Core: Sicherheit, JSON-API, Bilder, Server-Sent Events, Start."""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from astrostack import cli, core
from astrostack.settings import Settings
from astrostack.web import server as web
from astrostack.workspace import Workspace

from .test_core import c  # noqa: F401
from .test_flow import _calib_tree, env  # noqa: F401

TOKEN = "geheim-" * 6


def _settings(tmp_path, monkeypatch, workdir, pixinsight=""):
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"workdir": workdir, "pixinsight": pixinsight, "mcp_token": TOKEN}), encoding="utf-8")
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(sf))
    return Settings.load()


def _serve(co, s):
    srv = web.create(0, settings=s, core=co, refresh=False)
    srv.heartbeat_s, srv.poll_s = 0.4, 0.05
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    srv.thread = th
    return srv


def _stop(srv):
    if not srv.stopping.is_set():
        srv.stop()


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """Server ohne Projekte (schnell) – für Sicherheit und Ereignisse."""
    s = _settings(tmp_path, monkeypatch, str(tmp_path / "work"))
    co = core.Core(s.workdir, workers=1)
    srv = _serve(co, s)
    yield srv
    _stop(srv)
    co.close()


@pytest.fixture
def full(c, tmp_path, monkeypatch):  # noqa: F811
    """Server mit Rohdaten und nachgebautem PixInsight (aus test_core)."""
    co, data = c
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    yield srv, data
    _stop(srv)


def call(srv, method, path, body=None, headers=None, raw=False):
    h = {"Content-Type": "application/json"}
    if method == "POST":
        h["X-Astrostack"] = "1"
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(srv.url + path.lstrip("/"), data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = r.read()
            return r.status, (out if raw else json.loads(out or b"null")), r.headers
    except urllib.error.HTTPError as e:
        out = e.read()
        try:
            return e.code, (out if raw else json.loads(out or b"null")), e.headers
        except ValueError:
            return e.code, out, e.headers


# ---------------------------------------------------------------- Sicherheit

def test_host_and_header_checks(bare):
    assert call(bare, "GET", "api/projects", headers={"Host": "evil.example"})[0] == 403
    assert call(bare, "GET", "api/projects", headers={"Host": "localhost:8775"})[0] == 200
    assert call(bare, "GET", "api/projects", headers={"Host": "[::1]:8775"})[0] == 200
    assert call(bare, "POST", "api/backup", headers={"Host": "evil.example"})[0] == 403
    st, body, _ = call(bare, "POST", "api/backup", headers={"X-Astrostack": None})
    assert st == 403 and body["error"]
    st, body, _ = call(bare, "POST", "api/backup")
    assert st == 200 and body["path"].endswith(".db")
    assert call(bare, "GET", "api/gibt-es-nicht")[0] == 404
    assert call(bare, "POST", "api/gibt-es-nicht")[0] == 404
    st, body, _ = call(bare, "POST", "api/projects", body=["kein", "objekt"])
    assert st == 400 and "JSON-Objekt" in body["error"]


def test_mcp_endpoint_checks_token_and_origin(bare):
    assert call(bare, "GET", "mcp", raw=True)[0] == 405
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert call(bare, "POST", "mcp", init, headers={"X-Astrostack": None})[0] == 401
    assert call(bare, "POST", "mcp", init, headers={"Authorization": "Bearer falsch"})[0] == 401
    assert call(bare, "POST", "mcp", init, headers={"Authorization": "Bearer " + TOKEN,
                                                   "Origin": "https://evil.example"})[0] == 403
    st, body, _ = call(bare, "POST", "mcp", init, headers={"Authorization": "Bearer " + TOKEN})
    assert st == 200 and body["result"]["serverInfo"]["name"] == "b2stacker"


def test_bootstrap_and_settings_never_return_the_token(bare):
    st, raw, _ = call(bare, "GET", "api/bootstrap", raw=True)
    assert st == 200 and TOKEN.encode() not in raw
    b = json.loads(raw)
    assert "mcp_token" not in b["settings"] and b["projects"] == [] and b["first_run"] is False
    assert b["tasks"]["last_event"] >= 0 and b["workdir"] == bare.core.ws.root and b["pixinsight_ok"] is False
    assert "frozen" in b["install"] and b["version"]
    st, body, _ = call(bare, "POST", "api/settings", {"mcp_token": "x"})
    assert st == 400
    st, body, _ = call(bare, "POST", "api/settings", {"analysis_workers": 99})
    assert st == 400 and "zwischen" in body["error"]
    st, body, _ = call(bare, "POST", "api/settings", {"analysis_workers": "3", "pixinsight": "/nirgends/PixInsight"})
    assert st == 200 and body["restart_needed"] is False and "mcp_token" not in body["settings"]
    assert bare.core.workers == 3 and bare.core.pixinsight == "/nirgends/PixInsight"
    assert Settings.load().mcp_token == TOKEN                          # Schlüssel bleibt erhalten
    st, raw, _ = call(bare, "GET", "api/tasks", raw=True)
    assert st == 200 and json.loads(raw)["running"] == []


def test_internal_error_is_500_with_plain_text_and_logged(bare, monkeypatch):
    from astrostack import logbook
    logged = []
    monkeypatch.setattr(logbook, "error", lambda msg, **kw: logged.append(msg))
    monkeypatch.setattr(bare.core, "projects", lambda: 1 / 0)
    st, body, _ = call(bare, "GET", "api/projects")
    assert st == 500 and body["error"].startswith("Interner Fehler (ZeroDivisionError")
    assert logged == ["HTTP GET /api/projects"]


def test_request_threads_release_db_connections(bare):
    for _ in range(15):
        assert call(bare, "GET", "api/projects")[0] == 200
    assert len(bare.core.db._all) <= 4


# ---------------------------------------------------------------- JSON-API mit echten Daten

def test_project_api_end_to_end(full):
    srv, data = full
    co = srv.core
    st, r, _ = call(srv, "POST", "api/projects", {"name": "vdB 131", "folders": [data], "description": "Iris"})
    assert st == 200 and r["task"]["kind"] == "refresh"
    pid = r["id"]
    assert co.tasks.wait_idle(60)
    assert call(srv, "POST", "api/projects", {"name": "vdB 131"})[1]["error"].startswith("Ein Projekt")
    assert call(srv, "POST", "api/projects", {"name": "X", "folders": [1]})[0] == 400
    st, tiles, _ = call(srv, "GET", "api/projects")
    assert tiles[0]["name"] == "vdB 131" and tiles[0]["lights"] == 6
    st, p, _ = call(srv, "GET", "api/projects/%d" % pid)
    assert p["plan"]["ready"] and p["description"] == "Iris"
    assert call(srv, "GET", "api/projects/vdB%20131")[1]["id"] == pid      # auch per Name
    st, body, _ = call(srv, "GET", "api/projects/999")
    assert st == 404 and "nicht gefunden" in body["error"]
    assert call(srv, "POST", "api/projects/gibt-es-nicht/refresh")[0] == 404
    assert call(srv, "GET", "api/history?project=999")[0] == 404

    st, fr, _ = call(srv, "GET", "api/projects/%d/frames?type=light&status=use&limit=2" % pid)
    assert st == 200 and fr["total"] == 6 and len(fr["frames"]) == 2
    assert all(f["thumb_url"].startswith("/api/thumb/%d?v=" % f["id"]) for f in fr["frames"])
    assert call(srv, "GET", "api/projects/%d/frames?limit=viele" % pid)[0] == 400
    first = fr["frames"][0]
    st, d, _ = call(srv, "POST", "api/projects/%d/decide" % pid,
                    {"file_ids": [first["id"]], "decision": "skip", "reason": "Flugzeug"})
    assert st == 200 and d["changed"] == 1
    assert call(srv, "POST", "api/projects/%d/decide" % pid, {"file_ids": [first["id"]], "decision": "weg"})[0] == 400
    assert call(srv, "POST", "api/projects/%d/decide" % pid, {"file_ids": "x"})[0] == 400

    st, plan, _ = call(srv, "GET", "api/projects/%d/plan" % pid)
    assert st == 200 and plan["runs"]
    st, plan, _ = call(srv, "POST", "api/projects/%d/plan" % pid, {"options": {"local_normalization": True}})
    assert any(s["edited"] for r in plan["runs"] for s in r["settings"])
    assert call(srv, "POST", "api/projects/%d/plan" % pid, {"options": {"gibts": 1}})[0] == 400

    # Thumbnail aus dem gespeicherten .npy, mit ETag und optional dauerhaft cachebar
    st, img, h = call(srv, "GET", "api/thumb/%d" % first["id"], raw=True)
    assert st == 200 and img[:2] == b"\xff\xd8" and h["Content-Type"] == "image/jpeg" and h["Cache-Control"] == "no-cache"
    assert call(srv, "GET", "api/thumb/%d" % first["id"], headers={"If-None-Match": h["ETag"]}, raw=True)[0] == 304
    assert "immutable" in call(srv, "GET", first["thumb_url"], raw=True)[2]["Cache-Control"]
    assert call(srv, "GET", "api/thumb/999999", raw=True)[0] == 404
    assert call(srv, "GET", "api/thumb/abc", raw=True)[0] == 404
    st, img, h = call(srv, "GET", "api/frame/%d/preview" % first["id"], raw=True)
    assert st == 200 and img[:2] == b"\xff\xd8" and h["ETag"]

    assert call(srv, "POST", "api/projects/%d/stack" % pid, {"run_ids": [1]})[0] == 400
    st, t, _ = call(srv, "POST", "api/projects/%d/stack" % pid, {})
    assert st == 200 and t["lane"] == "stack"
    assert co.tasks.wait_idle(60)
    assert co.tasks.get(t["id"]).state == "done"
    st, stacks, _ = call(srv, "GET", "api/projects/%d/stacks" % pid)
    sid = stacks[0]["id"]
    assert stacks[0]["status"] == "done" and stacks[0]["files"]
    assert call(srv, "POST", "api/projects/%d/stacks/%d/note" % (pid, sid), {"text": "gut"})[1] == {"ok": True}
    assert call(srv, "GET", "api/projects/%d/stacks" % pid)[1][0]["note"] == "gut"
    assert call(srv, "POST", "api/projects/%d/stacks/%d/note" % (pid, sid + 99), {"text": "x"})[0] == 404
    assert call(srv, "POST", "api/projects/%d/stacks/abc/delete" % pid)[0] == 404
    st, d, _ = call(srv, "POST", "api/projects/%d/stacks/%d/delete" % (pid, sid))
    assert st == 200 and d["removed"]
    assert call(srv, "GET", "api/projects/%d/stacks" % pid)[1] == []

    st, hist, _ = call(srv, "GET", "api/history?project=%d&limit=50" % pid)
    kinds = [h["kind"] for h in hist]
    assert {"project_created", "frames_decided", "stack_done", "stack_deleted"} <= set(kinds)
    assert call(srv, "GET", "api/history")[1][0]["project"] == "vdB 131"

    assert call(srv, "POST", "api/projects/%d/describe" % pid, {"text": "Neu"})[1] == {"ok": True}
    assert call(srv, "POST", "api/projects/%d/rename" % pid, {"name": "Iris-Nebel"})[1]["id"] == pid
    st, p, _ = call(srv, "POST", "api/projects/%d/open" % pid)
    assert p["name"] == "Iris-Nebel" and p["description"] == "Neu"
    assert co.tasks.wait_idle(60)
    extra = data + "/vdB 131"
    st, r, _ = call(srv, "POST", "api/projects/%d/folders" % pid, {"folders": [extra]})
    assert st == 200 and r["task"]
    assert co.tasks.wait_idle(60)
    fid = [f for f in co.project(pid)["folders"] if f["path"].endswith("vdB 131")][0]["id"]
    assert call(srv, "POST", "api/projects/%d/folders/%d/remove" % (pid, fid))[1] == {"ok": True}
    st, t, _ = call(srv, "POST", "api/projects/%d/refresh" % pid)
    assert st == 200 and t["kind"] == "refresh"
    assert call(srv, "POST", "api/tasks/%s/cancel" % t["id"])[0] == 200
    assert call(srv, "POST", "api/tasks/gibt-es-nicht/cancel")[0] == 404
    assert co.tasks.wait_idle(60)
    assert call(srv, "POST", "api/clear-temp")[0] == 200


# ---------------------------------------------------------------- Server-Sent Events

class SSE:
    """Minimaler SSE-Client über einen rohen Socket (damit sich das Trennen genau prüfen lässt)."""

    def __init__(self, srv, query="", headers=None):
        self.sock = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
        lines = ["GET /api/events%s HTTP/1.1" % query, "Host: 127.0.0.1", "Accept: text/event-stream"]
        lines += ["%s: %s" % kv for kv in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.f = self.sock.makefile("rb")
        self.status = int(self.f.readline().split()[1])
        while self.f.readline().strip():
            pass

    def next(self, timeout=5.0):
        """Nächstes Ereignis (Kommentare/Heartbeats übersprungen) → dict(id, event, data)."""
        end = time.monotonic() + timeout
        ev = {}
        while time.monotonic() < end:
            line = self.f.readline()
            if not line:
                return None
            line = line.decode().rstrip("\n")
            if not line:
                if "event" in ev:
                    ev["data"] = json.loads(ev.get("data") or "null")
                    return ev
                ev = {}
                continue
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            ev[k] = int(v) if k == "id" else v
        raise TimeoutError

    def close(self):
        self.f.close()
        self.sock.close()


def test_sse_task_events_during_refresh(full):
    srv, data = full
    co = srv.core
    co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    sse = SSE(srv, "?last=%d" % co.tasks.hub.last_id)
    assert sse.status == 200
    t = call(srv, "POST", "api/projects/vdB%20131/refresh")[1]
    seen = []
    while True:
        ev = sse.next(30)
        seen.append(ev)
        if ev["event"] == "task" and ev["data"]["id"] == t["id"] and ev["data"]["state"] in ("done", "failed"):
            break
    sse.close()
    states = [e["data"]["state"] for e in seen if e["event"] == "task"]
    assert "running" in states and states[-1] == "done"
    assert any(e["event"] == "project" for e in seen)
    assert [e["id"] for e in seen] == sorted(e["id"] for e in seen)


def test_sse_resume_with_last_event_id(bare):
    hub = bare.core.tasks.hub
    ids = [hub.publish("test", {"n": i}) for i in range(3)]
    sse = SSE(bare, headers={"Last-Event-ID": str(ids[0])})
    got = [sse.next(), sse.next()]
    assert [(e["id"], e["data"]["n"]) for e in got] == [(ids[1], 1), (ids[2], 2)]
    nid = hub.publish("test", {"n": 3})                               # live hinterher
    assert sse.next()["id"] == nid
    sse.close()
    sse = SSE(bare, "?last=%d" % ids[1])
    assert sse.next()["id"] == ids[2]
    sse.close()
    sse = SSE(bare)                                                   # ohne Nummer: ab jetzt
    nid = hub.publish("test", {"n": 4})
    assert sse.next()["id"] == nid
    sse.close()


def test_sse_reset_on_gap_or_foreign_id(bare):
    hub = bare.core.tasks.hub
    for i in range(600):                                              # mehr als der Puffer hält
        hub.publish("test", {"n": i})
    for q in ("?last=1", "?last=999999", "?last=abc"):
        sse = SSE(bare, q)
        ev = sse.next()
        assert ev["event"] == "reset" and ev["data"]["last_event"] == hub.last_id == ev["id"]
        nid = hub.publish("test", {"n": "neu"})
        assert sse.next()["id"] == nid                                # danach geht es lückenlos weiter
        sse.close()


def _request_threads():
    return [t for t in threading.enumerate() if "process_request" in t.name]


def test_sse_disconnect_frees_thread_and_shutdown_does_not_hang(bare):
    base = len(_request_threads())
    sse = SSE(bare)
    assert len(_request_threads()) == base + 1
    sse.close()
    end = time.monotonic() + 5
    while len(_request_threads()) > base and time.monotonic() < end:
        time.sleep(0.05)
    assert len(_request_threads()) == base
    open_client = SSE(bare)                                           # ein verbundener Client hält nichts auf
    t0 = time.monotonic()
    bare.stop()
    bare.thread.join(5)
    assert not bare.thread.is_alive() and time.monotonic() - t0 < 3
    assert open_client.next(5) is None                                # Strom endet sauber
    open_client.close()


def test_sse_heartbeat(bare):
    sse = SSE(bare)
    sse.sock.settimeout(3)
    assert sse.f.readline().startswith(b"retry:")
    lines = [sse.f.readline() for _ in range(3)]
    assert b": ping\n" in lines
    sse.close()


# ---------------------------------------------------------------- Start und Beenden

def test_quit_stops_server(bare):
    st, body, _ = call(bare, "POST", "api/quit")
    assert st == 200 and body == {"ok": True}
    bare.thread.join(5)
    assert not bare.thread.is_alive()


def test_static_page(bare):
    st, html, h = call(bare, "GET", "", raw=True)
    assert st == 200 and b"/api/bootstrap" in html and h["Content-Type"].startswith("text/html")


def test_cli_web_and_new_ui_switch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(web, "main", lambda port, open_browser: calls.append(("web", port, open_browser)))
    from astrostack.app import server as old
    monkeypatch.setattr(old, "main", lambda port, open_browser: calls.append(("old", port, open_browser)))
    _settings(tmp_path, monkeypatch, str(tmp_path / "work"))
    monkeypatch.setenv("B2_OLD_UI", "1")
    cli.main(["gui", "--no-browser"])
    cli.main(["web", "--no-browser"])
    cli.main(["web", "--port", "9000"])
    monkeypatch.delenv("B2_OLD_UI")
    cli.main(["gui", "--no-browser"])
    assert calls == [("old", 8765, False), ("web", 8775, False), ("web", 9000, True), ("web", 8775, False)]


# ---------------------------------------------------------------- Dialoge, MCP-Infos, Programmfenster

def test_pickers_and_pixinsight_check(bare, monkeypatch, tmp_path):
    from types import SimpleNamespace
    seen = []

    def no_dialog(initial=""):
        raise RuntimeError("Ordner-Dialog nicht verfügbar – Pfad bitte eintippen.")
    fake = SimpleNamespace(pick_folder=lambda initial="": seen.append(("folder", initial)) or "/daten/M31",
                           pick_file=lambda initial="", kind="": seen.append(("file", initial, kind)) or None,
                           check_pixinsight=lambda path: {"path": path, "ok": False, "message": "Datei nicht gefunden."})
    monkeypatch.setattr(web, "dialogs", lambda: fake)
    assert call(bare, "POST", "api/pick-folder", {"initial": "/daten"})[1] == {"path": "/daten/M31"}
    assert call(bare, "POST", "api/pick-file", {"initial": "C:/x", "kind": "image"})[1] == {"path": None}
    assert seen == [("folder", "/daten"), ("file", "C:/x", "image")]
    st, r, _ = call(bare, "POST", "api/check-pixinsight", {"path": str(tmp_path)})
    assert st == 200 and r["ok"] is False and r["message"]
    fake.pick_folder = no_dialog
    st, r, _ = call(bare, "POST", "api/pick-folder", {})
    assert st == 400 and "eintippen" in r["error"]
    assert call(bare, "POST", "api/pick-folder", {"initial": 5})[0] == 400
    # die echte Prüfung der alten App ist eingehängt
    monkeypatch.undo()
    st, r, _ = call(bare, "POST", "api/check-pixinsight", {"path": str(tmp_path / "nichts.exe")})
    assert st == 200 and r["ok"] is False and r["exists"] is False


def test_mcp_info_and_new_token(bare):
    st, info, _ = call(bare, "POST", "api/mcp-info")
    assert st == 200 and info["token"] == TOKEN and info["url"] == bare.url + "mcp"
    assert TOKEN in info["claude_code"] and "b2stacker" in info["opencode"]
    assert call(bare, "GET", "api/mcp-info")[0] == 404                  # nie per GET
    st, info, _ = call(bare, "POST", "api/mcp-token")
    assert st == 200 and info["token"] != TOKEN and Settings.load().mcp_token == info["token"]
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert call(bare, "POST", "mcp", init, headers={"Authorization": "Bearer " + TOKEN})[0] == 401
    assert call(bare, "POST", "mcp", init, headers={"Authorization": "Bearer " + info["token"]})[0] == 200


def test_double_click_opens_new_window_unless_old_switch(monkeypatch):
    import sys

    from astrostack import __main__ as entry
    from astrostack.app import window as oldwin
    from astrostack.web import window as webwin
    calls = []
    monkeypatch.setattr(oldwin, "run", lambda *a, **k: calls.append("alt"))
    monkeypatch.setattr(webwin, "run", lambda *a, **k: calls.append("neu"))
    monkeypatch.setattr(sys, "argv", ["b2stacker.exe"])
    monkeypatch.setenv("B2_OLD_UI", "1")
    entry.run()
    monkeypatch.delenv("B2_OLD_UI")
    entry.run()
    assert calls == ["alt", "neu"]


def test_new_workdir_applies_at_once_while_empty(bare, tmp_path):
    old = bare.core
    sse = SSE(bare, "?last=%d" % old.tasks.hub.last_id)
    new = str(tmp_path / "anderswo")
    st, r, _ = call(bare, "POST", "api/settings", {"workdir": new})
    assert st == 200 and r["restart_needed"] is False and r["workdir"] == bare.core.ws.root
    assert bare.core is not old and bare.core.ws.root == Workspace(new).root
    assert call(bare, "GET", "api/bootstrap")[1]["workdir"] == bare.core.ws.root
    assert sse.next(5) is None                                         # Live-Verbindung endet → Client verbindet neu
    sse.close()
    call(bare, "POST", "api/projects", {"name": "M 31"})               # jetzt nicht mehr leer → Neustart nötig
    assert bare.core.tasks.wait_idle(30)
    st, r, _ = call(bare, "POST", "api/settings", {"workdir": str(tmp_path / "dritter")})
    assert st == 200 and r["restart_needed"] is True and bare.core.ws.root == Workspace(new).root
    bare.core.close()
