"""MCP über HTTP: Schlüssel, Origin, JSON-RPC, Weiterleitung des stdio-Servers an die laufende App."""
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from astrostack import appclient, mcp, mcp_http
from astrostack.app import server
from astrostack.settings import Settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"workdir": str(tmp_path / "work")}))
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(sf))
    server._MCP.update(handler=None, url=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    monkeypatch.setenv("B2_APP_URL", url)
    yield url
    httpd.shutdown()


def _post(url, body, token=None, origin=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(url + "mcp", data=json.dumps(body).encode(), method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def _info(url):
    req = urllib.request.Request(url + "api/mcp-info", data=b"{}", method="POST",
                                 headers={"X-Astrostack": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_info_token_and_snippets(app):
    i = _info(app)
    assert i["url"] == app + "mcp" and len(i["token"]) >= 32
    assert i["token"] in i["claude_code"] and json.loads(i["opencode"])["mcp"]["b2stacker"]["type"] == "remote"
    assert Settings.load().mcp_token == i["token"]
    assert _info(app)["token"] == i["token"]                         # bleibt stabil
    req = urllib.request.Request(app + "api/mcp-token", data=b"{}", method="POST",
                                 headers={"X-Astrostack": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert json.loads(r.read())["token"] != i["token"]


def test_auth_and_origin(app):
    tok = _info(app)["token"]
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    assert _post(app, init)[0] == 401
    assert _post(app, init, token="falsch")[0] == 401
    assert _post(app, init, token=tok, origin="https://boese.example")[0] == 403
    code, r = _post(app, init, token=tok, origin="http://localhost:3000")
    assert code == 200 and r["result"]["serverInfo"]["name"] == "b2stacker"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(app + "mcp", timeout=5)
    assert e.value.code == 405


def test_tools_list_call_batch_and_notifications(app):
    tok = _info(app)["token"]
    code, r = _post(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token=tok)
    names = {t["name"] for t in r["result"]["tools"]}
    assert code == 200 and {"status", "run_pixinsight_script"} <= names
    code, r = _post(app, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "status", "arguments": {}}}, token=tok)
    assert code == 200 and r["result"]["isError"] is False
    assert json.loads(r["result"]["content"][0]["text"])["version"]
    code, r = _post(app, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "gibtsnicht", "arguments": {}}}, token=tok)
    assert r["error"]["code"] == -32602
    code, r = _post(app, [{"jsonrpc": "2.0", "id": 5, "method": "ping"},
                          {"jsonrpc": "2.0", "method": "notifications/initialized"}], token=tok)
    assert code == 200 and isinstance(r, list) and len(r) == 1 and r[0]["id"] == 5
    assert _post(app, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token=tok)[0] == 202


def test_stdio_server_forwards_to_app(app):
    out = io.BytesIO()
    client = appclient.Client(autostart=False)
    s = mcp.Server(tools=mcp.Tools(client), out=out, forward=mcp.app_forwarder(client))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}}]
    s.serve(io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in msgs)))
    res = {r["id"]: r for r in (json.loads(x) for x in out.getvalue().splitlines())}
    assert any(t["name"] == "run_pixinsight_script" for t in res[1]["result"]["tools"])
    assert res[2]["result"]["isError"] is False


def test_stdio_falls_back_without_endpoint(tmp_path, monkeypatch):
    out = io.BytesIO()
    s = mcp.Server(tools=mcp.Tools(appclient.Client(autostart=False)), out=out, forward=lambda m: None)
    s.serve(io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n"))
    r = json.loads(out.getvalue())
    assert len(r["result"]["tools"]) == len(mcp.TOOLS)


def test_origin_rules():
    assert mcp_http.origin_ok({}) and mcp_http.origin_ok({"Origin": "http://127.0.0.1:8765"})
    assert not mcp_http.origin_ok({"Origin": "http://127.0.0.1.evil.com"})
    assert not mcp_http.authorized({"Authorization": "Bearer x"}, "")
