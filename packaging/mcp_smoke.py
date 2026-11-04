"""Prüft die fertige exe als MCP-Server – genau so, wie die Claude-Desktop-App sie startet (stdin/stdout-Pipes).
Aufruf: python packaging/mcp_smoke.py dist/b2stacker.exe   (startet b2stacker bei Bedarf ohne Fenster)"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

exe = sys.argv[1]
env = dict(os.environ, B2_MCP_HEADLESS="1")
p = subprocess.Popen([exe, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
q: "queue.Queue[dict]" = queue.Queue()
threading.Thread(target=lambda: [q.put(json.loads(x)) for x in iter(p.stdout.readline, b"") if x.strip()],
                 daemon=True).start()


def send(m):
    p.stdin.write((json.dumps(m) + "\n").encode("utf-8"))
    p.stdin.flush()


def answer(mid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            m = q.get(timeout=1)
        except queue.Empty:
            continue
        if m.get("id") == mid:
            return m, time.time() - t0
    raise SystemExit("Keine Antwort auf %s nach %d s. stderr: %s" % (mid, timeout, p.stderr.read1(4000) if p.poll() is not None else "–"))


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}})
m, dt = answer(1, 30)
print("initialize: %s %s (%.1f s)" % (m["result"]["serverInfo"]["name"], m["result"]["serverInfo"]["version"], dt))
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
m, dt = answer(2)
print("Werkzeuge: %d (%.1f s)" % (len(m["result"]["tools"]), dt))
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status", "arguments": {}}})
m, dt = answer(3)
r = m["result"]
if r.get("isError"):
    raise SystemExit("status: " + r["content"][0]["text"])
st = json.loads(r["content"][0]["text"])
print("status: App %s, Version %s, neu gestartet %s, %d Projekte (%.1f s)" % (st["app"], st["version"], st["started_now"],
                                                                          st["projects"], dt))
send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}})
m, dt = answer(4)
print("list_projects: %s (%.2f s)" % (m["result"]["content"][0]["text"][:200], dt))
p.stdin.close()
p.wait(timeout=30)
print("MCP-Server der exe OK")
