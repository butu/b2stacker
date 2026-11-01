"""Lokale Oberfläche: kleiner HTTP-Server (nur Standardbibliothek) + eine HTML-Seite.

Start: `astrostack gui` – öffnet http://127.0.0.1:8765 im Browser.
Nur von diesem Rechner erreichbar. Schreibende Aufrufe brauchen den Header X-Astrostack
(Schutz davor, dass fremde Webseiten Aufträge auslösen).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__, project, system
from ..engines import pixinsight as pi
from ..jobs import JobManager
from ..settings import Settings

HERE = os.path.dirname(__file__)
JOBS = JobManager()
SETTINGS_LOCK = threading.Lock()


def settings() -> Settings:
    return Settings.load()


# ------------------------------------------------------------------ Aktionen

def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 – eine kaputte Einzelabfrage darf die Oberfläche nicht blockieren
        traceback.print_exc()
        return default


def state() -> dict:
    s = settings()
    free_ram, total_ram = _safe(system.ram_gb, (None, None))
    active = JOBS.active()
    last = JOBS.jobs[-1] if JOBS.jobs else None
    return dict(
        version=__version__,
        settings=asdict(s),
        settings_file=Settings.path(),
        first_run=not os.path.exists(Settings.path()),
        pixinsight_ok=os.path.exists(s.pixinsight),
        wbpp_ok=os.path.exists(s.wbpp_script),
        pixinsight_running=_safe(system.pixinsight_processes, []),
        disk_free_gb=_r(_safe(lambda: system.free_disk_gb(s.workdir), None)),
        ram_free_gb=_r(free_ram), ram_total_gb=_r(total_ram),
        job=(active or last).as_dict(60) if (active or last) else None,
        projects=_safe(lambda: project.list_projects(s), []),
    )


def _r(x, n=1):
    return round(x, n) if x is not None else None


def save_settings(data: dict) -> dict:
    with SETTINGS_LOCK:
        s = settings()
        for k, v in data.items():
            if not hasattr(s, k):
                raise ValueError("unbekannte Einstellung: " + k)
            cur = getattr(s, k)
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            setattr(s, k, v)
        if s.workdir:
            os.makedirs(s.workdir, exist_ok=True)
        s.save()
    return state()


def _pick_folder_dialog(initial: str = ""):
    """Läuft in einem eigenen Prozess (tkinter verträgt sich nicht mit Server-Threads)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    p = filedialog.askdirectory(initialdir=initial or None, title="Ordner wählen")
    sys.stdout.write(p or "")
    sys.stdout.flush()


def _pick_folder_powershell(initial: str):
    """Ersatz unter Windows: der Ordner-Dialog des Systems."""
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description='Ordner wählen';"
          "$d.SelectedPath=$env:AS_INITIAL;"
          "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
          "if($d.ShowDialog((New-Object System.Windows.Forms.Form -Property @{TopMost=$true})) -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}")
    env = dict(os.environ, AS_INITIAL=initial or "")
    return subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env, creationflags=0x08000000)


def pick_folder(initial: str = "") -> str | None:
    """Nativer Ordner-Dialog. In der exe ruft sich das Programm selbst auf, sonst python -m astrostack."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_pick-folder", initial or ""]
    else:
        cmd = [sys.executable, "-m", "astrostack", "_pick-folder", initial or ""]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                             creationflags=0x08000000 if sys.platform == "win32" else 0)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Ordner-Dialog nicht verfügbar (%s) – Pfad bitte eintippen." % e)
    if out.returncode != 0 and sys.platform == "win32":
        out = _pick_folder_powershell(initial)
    if out.returncode != 0:
        raise RuntimeError("Ordner-Dialog nicht verfügbar – Pfad bitte eintippen.")
    p = out.stdout.strip()
    return os.path.normpath(p) if p else None


def start_analysis(folders: list[str], target: str | None) -> dict:
    folders = [f.strip().strip('"') for f in folders if f and f.strip()]
    if not folders:
        raise ValueError("Bitte mindestens einen Ordner angeben.")
    for f in folders:
        if not os.path.isdir(f):
            raise ValueError("Ordner nicht gefunden: " + f)
    s = settings()
    # zuletzt verwendete Ordner merken
    s.sources = list(dict.fromkeys(folders + s.sources))[:8]
    s.save()

    def fn(job):
        return project.run_report(folders, target or None, s, log=job.write,
                                  progress=lambda **kw: job.set_progress(**kw), cancel=job.cancel)
    job = JOBS.submit("analyze", "Analyse " + (target or ", ".join(os.path.basename(f) or f for f in folders)), s.workdir, fn)
    return job.as_dict()


def reanalyze(name: str) -> dict:
    pdir = project.pdir_of(settings(), name)
    src = project.sources(pdir)
    if not src.get("roots"):
        raise ValueError("Für dieses Projekt sind keine Quellordner gespeichert – bitte neu analysieren.")
    return start_analysis(src["roots"], src.get("target"))


def start_stack(name: str) -> dict:
    s = settings()
    pdir = project.pdir_of(s, name)
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir

    def fn(job):
        st = pi.run_plan(plan, s, log=job.write, cancel=job.cancel,
                         on_status=lambda x: job.set_progress(stage=(x.run + " · " + x.stage) if x.run else x.stage,
                                                              done=x.done, total=x.total, message=x.message))
        return st.as_dict()
    job = JOBS.submit("stack", "Stacking " + plan["target"], s.workdir, fn)
    return job.as_dict()


def open_path(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_in_pixinsight(paths: list[str]):
    s = settings()
    if not os.path.exists(s.pixinsight):
        raise FileNotFoundError("PixInsight nicht gefunden: " + s.pixinsight)
    subprocess.Popen([s.pixinsight] + paths)


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "astrostack/" + __version__

    def log_message(self, fmt, *args):  # leise
        pass

    # Antworten
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._json({"error": "nicht gefunden"}, 404)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # Routen
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        u = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
        q = urllib.parse.parse_qs(u.query)
        try:
            if not parts:
                return self._file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
            if parts == ["api", "state"]:
                return self._json(state())
            if parts[:2] == ["api", "job"] and len(parts) == 3:
                j = JOBS.get(parts[2])
                return self._json(j.as_dict(int(q.get("tail", ["300"])[0])) if j else {"error": "unbekannt"}, 200 if j else 404)
            if parts[:2] == ["api", "project"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                fdir = os.path.join(pdir, "final")
                finals = sorted(f for f in os.listdir(fdir) if f.endswith(".xisf")) if os.path.isdir(fdir) else []
                return self._json(dict(plan=project.load_plan(pdir), sources=project.sources(pdir),
                                       overrides=project._read(os.path.join(pdir, "overrides.json"), {}),
                                       finals=finals, pdir=pdir))
            if parts[0] == "p" and len(parts) == 3 and parts[2] == "report.html":
                return self._file(os.path.join(project.pdir_of(settings(), parts[1]), "report.html"), "text/html; charset=utf-8")
            return self._json({"error": "nicht gefunden"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Astrostack") != "1":
            return self._json({"error": "forbidden"}, 403)
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.strip("/").split("/") if p]
        try:
            b = self._body()
            if parts == ["api", "settings"]:
                return self._json(save_settings(b))
            if parts == ["api", "pick-folder"]:
                return self._json({"path": pick_folder(b.get("initial", ""))})
            if parts == ["api", "analyze"]:
                return self._json(start_analysis(b.get("folders", []), b.get("target")))
            if parts[:2] == ["api", "job"] and len(parts) == 4 and parts[3] == "cancel":
                j = JOBS.get(parts[2])
                if j:
                    j.cancel.set()
                    j.write("Abbruch angefordert …")
                return self._json({"ok": bool(j)})
            if parts == ["api", "open"]:
                open_path(b["path"])
                return self._json({"ok": True})
            if parts == ["api", "open-pixinsight"]:
                open_in_pixinsight(b["paths"])
                return self._json({"ok": True})
            if parts[:2] == ["api", "project"] and len(parts) == 4:
                name, action = parts[2], parts[3]
                pdir = project.pdir_of(settings(), name)
                if action == "plan":
                    return self._json(project.save_edits(pdir, b))
                if action == "override":
                    return self._json(project.set_override(pdir, b["path"], b.get("value")))
                if action == "reanalyze":
                    return self._json(reanalyze(name))
                if action == "stack":
                    return self._json(start_stack(name))
            return self._json({"error": "nicht gefunden"}, 404)
        except (ValueError, RuntimeError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        except FileNotFoundError as e:
            return self._json({"error": "nicht gefunden: %s" % e}, 404)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-800:]}, 500)


def serve(port: int = 8765, open_browser: bool = True) -> ThreadingHTTPServer:
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("Kein freier Port ab %d" % port)
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("astrostack läuft: " + url + "  (dieses Fenster offen lassen – Schließen beendet das Programm)", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return httpd


def already_running(port: int) -> str | None:
    """Läuft schon eine Instanz? Dann nur den Browser öffnen statt einen zweiten Server zu starten."""
    import urllib.request
    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url + "api/state", timeout=1.5) as r:
            if json.loads(r.read()).get("version"):
                return url
    except Exception:  # noqa: BLE001
        return None
    return None


def main(port: int = 8765, open_browser: bool = True):
    url = already_running(port)
    if url:
        print("astrostack läuft bereits: " + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    httpd = serve(port, open_browser)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
