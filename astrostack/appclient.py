"""Verbindung zur laufenden b2stacker-App (lokaler HTTP-Server auf 127.0.0.1).

KI-Werkzeuge (MCP-Server) und Kommandozeile reihen Aufträge über die App ein, statt selbst zu rechnen:
eine Warteschlange, nie zwei PixInsight-Instanzen, und im Programmfenster sieht man live, was passiert.
Läuft die App nicht, kann sie gestartet werden (Programmfenster bzw. ohne Fenster, wenn keins möglich ist).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORTS = range(8765, 8785)            # dieselben Ports, die der Server beim Start durchprobiert
START_TIMEOUT = 45.0


class AppError(Exception):
    """Fehler der App in Klartext (z. B. „Projekt nicht gefunden“)."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _probe(url: str, timeout: float = 0.6) -> dict | None:
    try:
        with urllib.request.urlopen(url + "api/sys", timeout=timeout) as r:
            d = json.loads(r.read() or b"{}")
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001 – kein Server, anderer Dienst, Zeitüberschreitung
        return None


def find() -> str | None:
    """URL der laufenden App oder None. B2_APP_URL überschreibt die Suche (Tests, anderer Port)."""
    env = os.environ.get("B2_APP_URL")
    if env:
        url = env.rstrip("/") + "/"
        return url if _probe(url, 2.0) is not None else None
    for port in PORTS:
        url = "http://127.0.0.1:%d/" % port
        if _probe(url) is not None:
            try:
                with urllib.request.urlopen(url + "api/steps", timeout=1.0) as r:   # wirklich b2stacker?
                    if "analyze" in json.loads(r.read() or b"{}"):
                        return url
            except Exception:  # noqa: BLE001
                continue
    return None


def _start_command(window: bool) -> list[str]:
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, "-m", "astrostack"]
    return base if window else base + ["gui", "--no-browser"]


def _can_show_window() -> bool:
    if os.environ.get("B2_MCP_HEADLESS"):
        return False
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def start(window: bool | None = None, timeout: float = START_TIMEOUT) -> str:
    """App im Hintergrund starten (losgelöst: läuft weiter, wenn der Aufrufer endet) und auf sie warten."""
    window = _can_show_window() if window is None else window
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(_start_command(window), **kw)
    t0 = time.time()
    while time.time() - t0 < timeout:
        url = find()
        if url:
            return url
        time.sleep(0.4)
    raise AppError("b2stacker ließ sich nicht starten (nach %d s keine Antwort). Bitte b2stacker von Hand öffnen."
                   % timeout)


class Client:
    def __init__(self, url: str | None = None, autostart: bool = True, who: str = "mcp"):
        self.url = url
        self.who = who
        self.autostart = autostart
        self.started = False

    def base(self) -> str:
        if self.url and _probe(self.url, 1.5) is not None:
            return self.url
        self.url = find()
        if not self.url:
            if os.environ.get("B2_APP_URL"):           # feste Adresse: dort läuft nichts – nicht woanders starten
                raise AppError("b2stacker läuft nicht unter %s." % os.environ["B2_APP_URL"])
            if not self.autostart:
                raise AppError("b2stacker läuft nicht. Bitte b2stacker öffnen.")
            self.url = start()
            self.started = True
        return self.url

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 60.0):
        url = self.base() + path.lstrip("/")
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-Astrostack": "1", "Content-Type": "application/json",
                                              "X-B2-Client": self.who})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                msg = json.loads(raw or b"{}").get("error") or "HTTP %d" % e.code
            except ValueError:
                msg = "HTTP %d" % e.code
            raise AppError(msg, e.code) from None
        except urllib.error.URLError as e:
            raise AppError("Keine Verbindung zu b2stacker (%s)" % e.reason) from None
        return json.loads(raw or b"null")

    def get(self, path: str, **query):
        q = {k: v for k, v in query.items() if v is not None}
        return self._call("GET", path + ("?" + urllib.parse.urlencode(q) if q else ""))

    def post(self, path: str, body: dict | None = None, timeout: float = 120.0):
        return self._call("POST", path, body, timeout)

    # -------------------------------------------------------------- Bequemlichkeit

    @staticmethod
    def q(name: str) -> str:
        return urllib.parse.quote(name, safe="")

    def project_name(self, name: str, projects: list | None = None) -> str:
        """Projekt per Ordnername, Titel oder Objekt finden – nur eindeutige, exakte Treffer
        (Groß-/Kleinschreibung und Leerzeichen egal: „m31“ = „M 31“). Nie raten: „M 31“ ist nicht „MCP Test M31“."""
        projects = projects if projects is not None else self.get("api/state").get("projects") or []
        norm = lambda x: str(x or "").lower().replace(" ", "").replace("_", "")    # noqa: E731
        want = norm(name)
        for key in ("name", "title", "target"):
            hits = {p["name"] for p in projects if norm(p.get(key)) == want}
            if len(hits) == 1:
                return hits.pop()
            if len(hits) > 1:
                raise AppError("„%s“ ist nicht eindeutig: %s" % (name, ", ".join(sorted(hits))))
        names = ", ".join("„%s“" % p.get("title") for p in projects) or "keine"
        similar = [p.get("title") for p in projects if want and want in norm(p.get("title"))]
        raise AppError("Projekt „%s“ nicht gefunden.%s Vorhanden: %s" % (
            name, (" Meintest du „%s“?" % "“ oder „".join(similar)) if similar else "", names))

    def workdir(self) -> str | None:
        return (self.get("api/state").get("settings") or {}).get("workdir")

    def wait_job(self, job_id: str, timeout: float, log_tail: int = 30, on_progress=None) -> dict:
        """Auf einen Auftrag warten (höchstens timeout s). Fragt alle 1 s – leicht für die App."""
        t0 = time.time()
        while True:
            j = self.get("api/job/" + self.q(job_id), tail=log_tail)
            if on_progress:
                on_progress(j)
            if j.get("state") not in ("queued", "running") or time.time() - t0 >= timeout:
                return j
            time.sleep(min(1.0, max(0.05, timeout - (time.time() - t0))))
