"""MCP-Server: Protokoll, echter Unterprozess über Pipes und der komplette Ablauf bis zum fertigen Stack."""
import io
import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from astrostack import appclient, claude_link, mcp
from astrostack.app import server
from astrostack.engines import pixinsight as pi

from .conftest import NINA_LIGHT, make_fits
from .fake_pixinsight import ProcRunner
from .test_calibcheck import _flat_header, synth_flat
from .test_hardening import star_field


def _data(tmp_path):
    """Kleiner, aber realistischer Satz: 4 Lights (ruhige Sterne), 6 Flats, 6 Bias."""
    root = tmp_path / "data"
    for i in range(4):
        make_fits(str(root / "vdB 131" / "2026-10-12" / "HaO3" / ("L_%d.fits" % i)),
                  dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)}), data=star_field(i))
    for i in range(6):
        make_fits(str(root / "FLAT" / "FLAT_2026-10-12" / "HaO3" / ("flat_%d.fits" % i)),
                  _flat_header(FILTER="HaO3", **{"DATE-OBS": "2026-10-12T18:00:%02d" % i}), data=synth_flat(h=400, w=600))
    for i in range(6):
        make_fits(str(root / "BIAS" / "2026-10-12" / ("b_%d.fits" % i)),
                  dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0001, EXPTIME=0.0001, FILTER="HaO3"),
                  shape=(400, 600), value=500)
    return str(root)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ Protokoll

class _FakeTools:
    def status(self, a):
        return {"ok": True, "a": a}

    def project(self, a):
        raise mcp.ToolError("Projekt fehlt")

    def frames(self, a):
        raise RuntimeError("Absturz im Werkzeug")


def _srv():
    out = io.BytesIO()
    s = mcp.Server(tools=_FakeTools(), out=out)
    return s, out


def _responses(out):
    return [json.loads(x) for x in out.getvalue().decode("utf-8").splitlines() if x.strip()]


def test_initialize_negotiates_version_and_has_instructions():
    s, _ = _srv()
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert r["result"]["serverInfo"]["name"] == "b2stacker" and "tools" in r["result"]["capabilities"]
    assert "Originaldateien" in r["result"]["instructions"]
    r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] == mcp.PROTOCOLS[0]


def test_tool_list_is_valid():
    tools = mcp.tool_list()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) == len(mcp.TOOLS)
    for t in tools:
        assert hasattr(mcp.Tools, t["name"]), t["name"]
        sch = t["inputSchema"]
        assert sch["type"] == "object" and set(sch.get("required", [])) <= set(sch["properties"])
        assert len(t["description"]) > 30
        assert t["annotations"]["destructiveHint"] is False if not t["annotations"]["readOnlyHint"] else True
    ro = {t["name"] for t in tools if t["annotations"]["readOnlyHint"]}
    assert {"status", "project", "frames", "job", "logs"} <= ro and not ({"stack", "set_frames", "analyze"} & ro)


def test_protocol_edge_cases():
    s, out = _srv()
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {}
    assert s.handle({"jsonrpc": "2.0", "id": 4, "method": "gibts/nicht"})["error"]["code"] == -32601
    assert s.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm_rf"}})["error"]["code"] == -32602
    assert s.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                     "params": {"name": "status", "arguments": "x"}})["error"]["code"] == -32602
    assert s.handle({"jsonrpc": "2.0", "id": 7, "result": {}}) is None        # Antwort eines Clients
    s.serve(io.BytesIO(b"kein json\n\n[1]\n"))
    codes = [r["error"]["code"] for r in _responses(out)]
    assert codes == [-32700, -32600]


def test_tool_errors_are_results_not_crashes():
    s, out = _srv()
    req = lambda i, name: json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",       # noqa: E731
                                      "params": {"name": name, "arguments": {"x": 1}}}).encode() + b"\n"
    s.serve(io.BytesIO(req(1, "status") + req(2, "project") + req(3, "frames")))
    by_id = {r["id"]: r["result"] for r in _responses(out)}
    assert by_id[1]["isError"] is False and json.loads(by_id[1]["content"][0]["text"]) == {"ok": True, "a": {"x": 1}}
    assert by_id[2]["isError"] and "Projekt fehlt" in by_id[2]["content"][0]["text"]
    assert by_id[3]["isError"] and "Absturz im Werkzeug" in by_id[3]["content"][0]["text"]


# ------------------------------------------------------------------ App + Werkzeuge im selben Prozess

@pytest.fixture
def app(tmp_path, monkeypatch):
    """Echter b2stacker-HTTP-Server mit nachgebautem PixInsight (eigener Prozess je Lauf)."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workdir": str(tmp_path / "work"), "pixinsight": sys.executable,
                                         "open_results": False, "min_free_ram_gb": 0.1, "analysis_workers": 1}))
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(settings_file))
    monkeypatch.setattr(pi, "Runner", lambda exe: ProcRunner())
    monkeypatch.setattr(pi.system, "pixinsight_processes", lambda: [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    monkeypatch.setenv("B2_APP_URL", url)
    try:
        yield mcp.Tools(appclient.Client(autostart=False)), tmp_path
    finally:
        httpd.shutdown()


def test_full_flow_create_analyze_review_stack(app):
    t, tmp = app
    root = _data(tmp)
    st = t.status({})
    assert st["version"] and st["pixinsight_ok"] and st["workdir"].endswith("work")
    scan = t.scan_folders({"folders": [root]})
    assert any(x["name"] == "vdB 131" for x in scan["targets"])
    cp = t.create_project({"title": "Test MCP", "folders": [root]})
    assert cp["job"]["title"].endswith(server.VIA_AI), "Auftrag ist im Fenster als KI-Auftrag erkennbar"
    j = t.job({"id": cp["job"]["id"], "wait_s": 60})
    assert j["state"] == "done", j
    p = t.project({"name": "test mcp"})
    assert p["stack_ready"] and p["runs"][0]["lights"] == 4 and p["frames"]["lights"] == 4
    fr = t.frames({"project": "Test MCP", "limit": 3})
    assert fr["matching"] == 4 and len(fr["frames"]) == 3 and fr["more"] == 1
    one = fr["frames"][0]["file"]
    res = t.set_frames({"project": "Test MCP", "frames": [one], "decision": "out"})
    assert res["changed"] == 1
    assert t.project({"name": "Test MCP"})["runs"][0]["lights"] == 3
    t.set_frames({"project": "Test MCP", "frames": [one], "decision": "auto"})
    assert t.project({"name": "Test MCP"})["runs"][0]["lights"] == 4
    with pytest.raises(mcp.ToolError, match="Nichts geändert"):
        t.set_frames({"project": "Test MCP", "frames": [one, "gibtsnicht.fits"], "decision": "out"})
    assert t.project({"name": "Test MCP"})["runs"][0]["lights"] == 4, "alles oder nichts"
    pl = t.change_plan({"project": "Test MCP", "options": {"local_normalization": False}})
    assert pl["changed"] and pl["options"]["local_normalization"] is False
    with pytest.raises(appclient.AppError):
        t.change_plan({"project": "Test MCP", "runs": {"Quatsch": {"enabled": False}}})
    run = p["runs"][0]["name"]
    sk = t.stack({"project": "Test MCP", "runs": [run]})
    assert sk["stacking"][0]["lights"] == 4
    j = t.job({"id": sk["job"]["id"], "wait_s": 90, "log_lines": 50})
    assert j["state"] == "done", (j.get("error"), j.get("log"))
    p = t.project({"name": "Test MCP"})
    assert p["stacks"] and p["stacks"][-1]["outputs"][0]["exists"]
    assert any(e.get("kind") == "job_end" for e in t.logs({"last": 50})), "Protokoll lesbar"
    with pytest.raises(appclient.AppError, match="nicht gefunden"):
        t.project({"name": "gibtsnicht"})
    with pytest.raises(appclient.AppError):
        t.stack({"project": "Test MCP", "runs": ["Quatsch"]})


def test_project_names_never_guess(app):
    t, _tmp = app
    t.create_project({"title": "MCP Test M31", "analyze": False})
    t.create_project({"title": "M 33", "analyze": False})
    with pytest.raises(appclient.AppError, match="Meintest du"):
        t.project({"name": "M31"})
    assert t.project({"name": "m33"})["title"] == "M 33"
    assert t.project({"name": "mcp test m31"})["title"] == "MCP Test M31"


def test_stack_refuses_with_clear_reason(app):
    t, _tmp = app
    t.create_project({"title": "Leer", "analyze": False})
    with pytest.raises(mcp.ToolError, match="Stacken nicht möglich"):
        t.stack({"project": "Leer"})


# ------------------------------------------------------------------ echter Unterprozess (stdin/stdout-Pipes)

def _rpc_lines(*msgs):
    return "".join(json.dumps(m) + "\n" for m in msgs).encode("utf-8")


def test_subprocess_speaks_mcp_over_pipes(tmp_path):
    env = dict(os.environ, ASTROSTACK_SETTINGS=str(tmp_path / "s.json"), B2_APP_URL="http://127.0.0.1:9/",
               PYTHONPATH=ROOT)
    (tmp_path / "s.json").write_text(json.dumps({"workdir": str(tmp_path / "w")}))
    msgs = _rpc_lines({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                      {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                      {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "list_projects", "arguments": {}}},
                      {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "project", "arguments": {"name": "Ä ö ü – 漢字"}}})
    r = subprocess.run([sys.executable, "-m", "astrostack", "mcp"], input=msgs, capture_output=True, env=env,
                       timeout=60, cwd=ROOT)
    lines = [json.loads(x) for x in r.stdout.decode("utf-8").splitlines() if x.strip()]
    by_id = {m["id"]: m for m in lines}
    assert set(by_id) == {1, 2, 3, 4}, (r.stdout[:500], r.stderr[-800:])
    assert len(by_id[2]["result"]["tools"]) == len(mcp.TOOLS)
    assert by_id[3]["result"]["isError"] and "läuft nicht" in by_id[3]["result"]["content"][0]["text"]
    assert r.returncode == 0
    assert by_id[4]["result"]["isError"]
    assert all(set(m) <= {"jsonrpc", "id", "result", "error"} for m in lines), "nur JSON-RPC auf stdout"


def test_official_client_if_installed(app):
    pytest.importorskip("mcp.client.stdio")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    t, _tmp = app
    t.create_project({"title": "SDK", "analyze": False})

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "astrostack", "mcp"],
                                       env=dict(os.environ, PYTHONPATH=ROOT), cwd=ROOT)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                res = await s.call_tool("list_projects", {})
                return [x.name for x in tools.tools], json.loads(res.content[0].text)
    names, projects = anyio.run(go)
    assert "stack" in names and [p["title"] for p in projects] == ["SDK"]


# ------------------------------------------------------------------ Claude-Desktop-Konfiguration

def test_claude_config_merge_backup_and_remove(tmp_path, monkeypatch):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setenv("B2_CLAUDE_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="nicht gefunden"):
        claude_link.install()                            # Claude-Ordner fehlt
    cfg.parent.mkdir()
    st = claude_link.status()
    assert st["claude_found"] and not st["installed"]
    cfg.write_text(json.dumps({"mcpServers": {"andere": {"command": "x"}}, "globalShortcut": "Ctrl+Space"}))
    res = claude_link.install()
    d = json.loads(cfg.read_text())
    assert d["globalShortcut"] == "Ctrl+Space" and d["mcpServers"]["andere"] == {"command": "x"}
    assert d["mcpServers"]["b2stacker"]["args"][-1] == "mcp" and res["installed"] and res["current"]
    assert list(cfg.parent.glob("*.bak")), "Sicherung vor dem Ändern"
    d["mcpServers"]["b2stacker"]["command"] = "C:/alt/b2stacker.exe"
    cfg.write_text(json.dumps(d))
    assert claude_link.status()["installed"] and not claude_link.status()["current"]
    claude_link.uninstall()
    d = json.loads(cfg.read_text())
    assert "b2stacker" not in d["mcpServers"] and d["mcpServers"]["andere"] == {"command": "x"}


def test_claude_config_broken_file_is_never_overwritten(tmp_path, monkeypatch):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    cfg.write_text('{"mcpServers": {kaputt')
    monkeypatch.setenv("B2_CLAUDE_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="nicht verändert"):
        claude_link.install()
    assert cfg.read_text() == '{"mcpServers": {kaputt'
    assert claude_link.status()["files"][0]["error"]


def test_claude_link_api(app, tmp_path, monkeypatch):
    cfg = tmp_path / "C" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    monkeypatch.setenv("B2_CLAUDE_CONFIG", str(cfg))
    c = appclient.Client(autostart=False)
    assert c.get("api/claude")["installed"] is False
    assert c.post("api/claude/install")["installed"] is True
    assert c.post("api/claude/uninstall")["installed"] is False
