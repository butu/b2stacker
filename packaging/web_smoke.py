"""Prüft die fertige exe mit der neuen Oberfläche: Server ohne Fenster starten, Projekt mit Testdaten anlegen,
auf das automatische Einlesen warten, Frames und MCP abfragen, sauber beenden.
Aufruf: python packaging/web_smoke.py dist/b2stacker.exe <ordner-mit-testdaten>"""
import json
import subprocess
import sys
import time
import urllib.request

exe, data = sys.argv[1], sys.argv[2]
p = subprocess.Popen([exe, "web", "--no-browser"])


def get(url, path, timeout=10):
    with urllib.request.urlopen(url + path, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def post(url, path, body=None, headers=None):
    h = {"Content-Type": "application/json", "X-Astrostack": "1"}
    h.update(headers or {})
    req = urllib.request.Request(url + path, data=json.dumps(body or {}).encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


url, t0 = None, time.time()
while not url and time.time() - t0 < 60:
    for port in range(8775, 8795):
        try:
            if get("http://127.0.0.1:%d/" % port, "api/ping", 1).get("app") == "b2stacker":
                url = "http://127.0.0.1:%d/" % port
                break
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.5)
if not url:
    p.kill()
    raise SystemExit("Neue Oberfläche antwortet nicht")
boot = get(url, "api/bootstrap")
print("Neue Oberfläche: %s, Version %s, PixInsight gefunden: %s" % (url, boot["version"], boot["pixinsight_ok"]))
html = urllib.request.urlopen(url, timeout=10).read()
assert b"/api/bootstrap" in html, "Seite fehlt in der exe"
proj = post(url, "api/projects", {"name": "Rauchtest", "folders": [data]})
pid = proj["id"]
t0 = time.time()
while time.time() - t0 < 180:
    ps = {x["id"]: x for x in get(url, "api/projects")}
    if ps.get(pid, {}).get("lights") and not ps[pid].get("busy"):
        break
    time.sleep(1)
else:
    raise SystemExit("Projekt wurde nicht eingelesen: %s" % ps.get(pid))
fr = get(url, "api/projects/%d/frames?type=light" % pid)
plan = get(url, "api/projects/%d/plan" % pid)
print("Projekt eingelesen: %d Lights, %d mit Vorschaubild, Plan: %d Läufe (%.1f s)"
      % (fr["total"], sum(1 for f in fr["frames"] if f.get("thumb_url")), len((plan or {}).get("runs") or []),
         time.time() - t0))
assert fr["total"] > 0 and any(f.get("thumb_url") for f in fr["frames"])
info = post(url, "api/mcp-info")
m = post(url, "mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
         headers={"Authorization": "Bearer " + info["token"]})
print("MCP über HTTP: %d Werkzeuge" % len(m["result"]["tools"]))
post(url, "api/quit")
try:
    p.wait(timeout=30)
except subprocess.TimeoutExpired:
    p.kill()
    raise SystemExit("Programm hat sich nach „beenden“ nicht beendet")
print("Neue Oberfläche der exe OK (Exit %s)" % p.returncode)
