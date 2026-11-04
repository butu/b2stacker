"""Eigenes PixInsight-Skript ausführen: Prüfungen, Ausgabe, Abbruch, Zeitlimit, App und MCP."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from astrostack import piscript

FAKE = r'''
import os, sys, time
r = [a for a in sys.argv if a.startswith("-r=")][0][3:]
parts = r.split(",")
script, args = parts[0], parts[1:]
first = open(script, encoding="utf-8").readline().strip()
if first != "#engine v8":
    sys.exit(0)                     # wie PixInsight 1.9.6: legacy-Skript läuft still nicht
print("PixInsight Core 1.9.6 (fake)", flush=True)
print("args=" + "|".join(args), flush=True)
for a in args:
    if a.startswith("sleep="):
        time.sleep(float(a[6:]))
    if a.startswith("touch="):
        open(a[6:], "w").write("ok")
print("Script terminated.", flush=True)
sys.exit(3 if "fail" in args else 0)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake_py = tmp_path / "fakepi.py"
    fake_py.write_text(FAKE, encoding="utf-8")
    exe = tmp_path / "PixInsight.exe"
    exe.write_text("")

    def popen(cmd, **kw):
        kw.pop("creationflags", None)
        argv = cmd if isinstance(cmd, list) else __import__("shlex").split(cmd, posix=True)
        return subprocess.Popen([sys.executable, str(fake_py)] + argv[1:], **kw)

    monkeypatch.setattr(piscript, "_popen", popen)
    work = tmp_path / "work"
    work.mkdir()
    return str(exe), work


def _script(work, name="t.js", engine=True):
    p = work / name
    p.write_text(("#engine v8\n" if engine else "") + "console.writeln('x');\n", encoding="utf-8")
    return str(p)


def test_check_rules(tmp_path):
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    ok = _script(work / "sub")
    path, args, notes = piscript.check("sub/t.js", str(work), ["a=1"])
    assert path == ok and args == ["a=1"] and notes == []
    outside = tmp_path / "x.js"
    outside.write_text("#engine v8\n")
    with pytest.raises(piscript.ScriptError, match="Arbeitsverzeichnis"):
        piscript.check(str(outside), str(work))
    with pytest.raises(piscript.ScriptError, match="Arbeitsverzeichnis"):
        piscript.check("../x.js", str(work))
    (work / "a.py").write_text("")
    with pytest.raises(piscript.ScriptError, match="Nur PixInsight"):
        piscript.check("a.py", str(work))
    with pytest.raises(piscript.ScriptError, match="nicht gefunden"):
        piscript.check("fehlt.js", str(work))
    with pytest.raises(piscript.ScriptError, match="Komma"):
        piscript.check(ok, str(work), ["a,b"])
    with pytest.raises(piscript.ScriptError, match="Kein Skript"):
        piscript.check("", str(work))
    _script(work, "legacy.js", engine=False)
    assert "engine v8" in piscript.check("legacy.js", str(work))[2][0]


def test_run_captures_output_and_exit_code(fake):
    exe, work = fake
    s = _script(work)
    lines = []
    marker = work / "made.txt"
    res = piscript.run(exe, s, ["touch=" + str(marker).replace("\\", "/"), "fail"], str(work), log=lines.append)
    assert res["exit_code"] == 3 and res["state"] == "done"
    assert marker.exists()
    assert any("args=touch=" in x for x in res["output_tail"])
    assert any(x.startswith("  PI: Script terminated.") for x in lines)
    text = open(res["logfile"], encoding="utf-8").read()
    assert "PixInsight Core 1.9.6" in text and res["logfile"].startswith(str(work / "logs" / "pixinsight-scripts"))


def test_legacy_script_ends_silently(fake):
    exe, work = fake
    res = piscript.run(exe, _script(work, engine=False), [], str(work), log=lambda *a: None)
    assert res["exit_code"] == 0 and res["output_lines"] == 0


def test_cancel_and_timeout(fake, monkeypatch):
    exe, work = fake
    s = _script(work)
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.time()
    res = piscript.run(exe, s, ["sleep=30"], str(work), log=lambda *a: None, cancel=ev)
    assert res["state"] == "cancelled" and time.time() - t0 < 15
    monkeypatch.setattr(piscript, "MAX_TIMEOUT_S", 1)
    res = piscript.run(exe, s, ["sleep=30"], str(work), log=lambda *a: None, timeout_s=0.5)
    assert res["state"] == "timeout"


def test_missing_pixinsight(tmp_path):
    with pytest.raises(piscript.ScriptError, match="PixInsight nicht gefunden"):
        piscript.run(str(tmp_path / "nix.exe"), "x.js", [], str(tmp_path))


def test_app_and_mcp_tool(fake, tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer

    from astrostack import appclient, mcp
    from astrostack.app import server
    exe, work = fake
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workdir": str(work), "pixinsight": exe}))
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(settings_file))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("B2_APP_URL", "http://127.0.0.1:%d/" % httpd.server_address[1])
    try:
        t = mcp.Tools(appclient.Client(autostart=False))
        (work / "pitest").mkdir()
        _script(work, "pitest/t1.js")
        r = t.run_pixinsight_script({"script": "pitest/t1.js", "args": ["eins=1"]})
        assert r["job"]["title"].endswith(server.VIA_AI)
        j = t.job({"id": r["job"]["id"], "wait_s": 30})
        assert j["state"] == "done", j
        assert j["result"]["exit_code"] == 0 and any("args=eins=1" in x for x in j["result"]["output_tail"])
        with pytest.raises(appclient.AppError, match="Arbeitsverzeichnis"):
            t.run_pixinsight_script({"script": str(tmp_path / "fakepi.py")})
        with pytest.raises(mcp.ToolError):
            t.run_pixinsight_script({"script": "pitest/t1.js", "args": "x"})
    finally:
        httpd.shutdown()
