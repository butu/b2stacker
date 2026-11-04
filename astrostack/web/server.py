"""Neuer HTTP-Server des Neubaus auf der Dienst-Schicht ``Core`` (nur Standardbibliothek).

Start: ``python -m astrostack web`` (Port ab 8775, läuft neben der alten App auf 8765) oder ``gui`` mit
``B2_NEW_UI=1``. Ein Server hält genau eine ``Core``-Instanz; beim Start werden alle Projekte im Hintergrund
geprüft (Konzept: „App-Start prüft alle Projekte“).

Sicherheit wie bei der alten App:
- gebunden an 127.0.0.1, ``Host`` muss 127.0.0.1, localhost oder [::1] sein (Schutz gegen DNS-Rebinding),
- jeder POST braucht den Header ``X-Astrostack: 1`` (fremde Webseiten können ihn nicht setzen),
- kein GET liefert Geheimnisse (der MCP-Schlüssel fehlt in allen Antworten),
- ``/mcp`` prüft Schlüssel und Origin selbst (``mcp_http``).

Alle Antworten sind JSON; Fehler kommen als ``{"error": "<Klartext>"}`` (400 Eingabe, 404 unbekannt,
500 intern – dann auch im Protokoll). Live-Ereignisse gibt es per Server-Sent Events unter ``/api/events``.
"""
from __future__ import annotations

import hashlib
import json
import os
import select
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__, library, logbook
from .. import settings as settings_mod
from ..core import Core
from ..settings import Settings
from ..tasks import sse_format
from . import media

HERE = os.path.dirname(__file__)
PORT = 8775                       # neben der alten App (8765)
MAX_BODY = 10 * 1024 * 1024
SETTINGS_LOCK = threading.Lock()
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _json_bytes(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


def public_settings(s: Settings) -> dict:
    """Einstellungen für die Oberfläche – ohne den MCP-Schlüssel."""
    return {k: v for k, v in asdict(s).items() if k != "mcp_token"}


class NotFound(Exception):
    """Route oder Objekt unbekannt → 404."""


# ------------------------------------------------------------------ Server

class WebServer(ThreadingHTTPServer):
    """HTTP-Server mit eigener ``Core``. Jede Anfrage läuft in einem eigenen (Daemon-)Thread."""

    daemon_threads = True
    allow_reuse_address = False   # sonst könnten zwei Server denselben Port belegen (Windows)
    allow_reuse_port = False
    heartbeat_s = 15.0            # SSE: Kommentarzeile, damit Proxys/Browser die Verbindung offen halten
    poll_s = 1.0                  # SSE: so oft wird auf Server-Ende und getrennten Client geprüft

    def __init__(self, core: Core, port: int = 0):
        super().__init__(("127.0.0.1", port), Handler)
        self.core = core
        self.stopping = threading.Event()
        self._mcp = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.server_address[1]

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.core.db.release()            # jede Anfrage hat ihren Thread → Verbindung nicht liegen lassen

    def stop(self) -> None:
        """Server beenden: SSE-Ströme enden, ``serve_forever`` kehrt zurück, der Port wird frei."""
        self.stopping.set()
        self.shutdown()
        self.server_close()

    def mcp_handler(self):
        """MCP über HTTP: dieselben Werkzeuge wie in der alten App (sprechen noch deren API, siehe NEUBAU.md)."""
        if self._mcp is None:
            from ..app.server import _mcp_handler
            self._mcp = _mcp_handler(self.url)
        return self._mcp


def create(port: int = PORT, settings: Settings | None = None, core: Core | None = None,
           refresh: bool = True, tries: int = 20) -> WebServer:
    """Core aus den Einstellungen bauen (falls nicht übergeben), Server binden, Projekte prüfen lassen.
    ``port=0`` wählt einen freien Port (Tests)."""
    s = settings or Settings.load()
    own = core is None
    core = core or Core(s.workdir, pixinsight=s.pixinsight, workers=s.analysis_workers)
    srv = None
    for p in ([0] if port == 0 else range(port, port + tries)):
        try:
            srv = WebServer(core, p)
            break
        except OSError:
            continue
    if srv is None:
        if own:
            core.close()
        raise RuntimeError("Kein freier Port ab %d" % port)
    if refresh:
        core.refresh_all()
    return srv


# ------------------------------------------------------------------ Aktionen

def save_settings(srv: WebServer, data: dict) -> dict:
    """Einstellungen prüfen (gleiche Regeln wie die alte App) und speichern; PixInsight-Pfad und Anzahl der
    Analysen gelten sofort, ein neues Arbeitsverzeichnis erst nach einem Neustart."""
    from ..app.server import _setting_value, _site_value
    with SETTINGS_LOCK:
        s = Settings.load()
        for k, v in data.items():
            if k == "mcp_token":
                raise ValueError("Der MCP-Schlüssel wird nicht über die Einstellungen gesetzt.")
            if not hasattr(s, k):
                raise ValueError("unbekannte Einstellung: " + k)
            v = _site_value(k, v) if k in ("site_lat", "site_lon", "site_elev") else _setting_value(k, getattr(s, k), v)
            setattr(s, k, v)
        if (s.site_lat is None) != (s.site_lon is None):
            raise ValueError("Für den Ersatz-Standort Breiten- und Längengrad angeben (oder beide leer lassen)")
        if s.workdir:
            os.makedirs(s.workdir, exist_ok=True)
        s.save()
    srv.core.pixinsight = s.pixinsight
    srv.core.workers = s.analysis_workers
    logbook.event("settings", changed={k: v for k, v in data.items()}, workdir=s.workdir)
    moved = os.path.normcase(os.path.abspath(srv.core.ws.root)) != os.path.normcase(os.path.abspath(s.workdir))
    switched = moved and _switch_workdir(srv, s)
    return dict(settings=public_settings(s), restart_needed=moved and not switched, workdir=srv.core.ws.root)


def _switch_workdir(srv: WebServer, s: Settings) -> bool:
    """Neues Arbeitsverzeichnis sofort übernehmen – nur solange das alte leer ist (Ersteinrichtung) und nichts
    läuft; sonst gilt es erst nach einem Neustart. Offene Live-Verbindungen enden und bekommen ``reset``."""
    old = srv.core
    if old.tasks.active() or old.db.value("SELECT COUNT(*) FROM projects"):
        return False
    srv.core = Core(s.workdir, pixinsight=s.pixinsight, workers=s.analysis_workers, stack_runner=old.stack_runner)
    logbook.setup(s.workdir)
    logbook.event("workdir_switched", old=old.ws.root, new=s.workdir)
    old.close()
    return True


def bootstrap(srv: WebServer) -> dict:
    """Alles für den ersten Bildschirm in einer Antwort; danach hält ``/api/events`` den Stand aktuell."""
    from .. import install
    s = Settings.load()
    core = srv.core
    try:
        inst = install.status()
    except Exception:  # noqa: BLE001 – Installationsstatus ist nur Zusatzinfo
        inst = {}
    return dict(version=__version__, settings=public_settings(s), settings_problems=list(settings_mod.LOAD_PROBLEMS),
                pixinsight_ok=bool(core.pixinsight) and os.path.isfile(core.pixinsight),
                workdir=core.ws.root, first_run=not os.path.exists(Settings.path()), install=inst,
                tasks=core.tasks.snapshot(), projects=core.projects())


def dialogs():
    """Ordner-/Datei-Dialoge und PixInsight-Prüfung der alten App (im Programmfenster nativ, sonst über einen
    Hilfsprozess). Eigene Funktion, damit Tests sie ersetzen können."""
    from ..app import server as old
    return old


def quit_server(srv: WebServer) -> dict:
    """Programm beenden: laufende Aufträge abbrechen, Server stoppen (``main`` räumt danach auf)."""
    for t in srv.core.tasks.active():
        srv.core.tasks.cancel(t.id)
    logbook.event("app_quit", ui="web")

    def bye():
        time.sleep(0.3)                       # die Antwort soll noch ankommen
        srv.stop()
        from ..app import window              # im Programmfenster: Fenster schließen
        w = window.STATE.get("window")
        if w is not None:
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=bye, daemon=True, name="quit").start()
    return {"ok": True}


# ------------------------------------------------------------------ Eingaben

def _int(q: dict, key: str, default: int | None) -> int | None:
    raw = (q.get(key) or [""])[0].strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError("%s: „%s“ ist keine ganze Zahl" % (key, raw)) from None


def _str(q: dict, key: str, default: str | None = None) -> str | None:
    raw = (q.get(key) or [""])[0].strip()
    return raw or default


def _list(b: dict, key: str, required: bool = True) -> list | None:
    v = b.get(key)
    if v is None and not required:
        return None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise ValueError("%s: Liste erwartet" % key)
    return v


def _paths(b: dict, key: str, required: bool = True) -> list[str]:
    v = _list(b, key, required) or []
    if not all(isinstance(x, str) for x in v):
        raise ValueError("%s: nur Ordnerpfade (Text) erlaubt" % key)
    return v


def _ids(b: dict, key: str) -> list[int]:
    out = []
    for x in _list(b, key):
        if isinstance(x, bool) or not isinstance(x, (int, str)) or not str(x).strip().isdigit():
            raise ValueError("%s: nur Datei-IDs (Zahlen) erlaubt" % key)
        out.append(int(x))
    return out


def _dict(b: dict, key: str) -> dict | None:
    v = b.get(key)
    if v is not None and not isinstance(v, dict):
        raise ValueError("%s: Objekt erwartet" % key)
    return v


def _text(b: dict, key: str) -> str:
    v = b.get(key, "")
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("%s: Text erwartet" % key)
    return v


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server: WebServer
    server_version = "b2stacker/" + __version__

    def log_message(self, fmt, *args):  # leise
        pass

    @property
    def core(self) -> Core:
        return self.server.core

    # ---------------------------------------------------------------- Antworten
    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200, headers: dict | None = None):
        self._send(code, _json_bytes(data), "application/json; charset=utf-8", headers=headers)

    def _error(self, code: int, msg: str):
        self._json({"error": msg}, code)

    def _image(self, path: str, version: str, immutable: bool):
        """JPEG mit ETag; mit passender ``?v=`` in der URL darf der Browser es dauerhaft behalten."""
        etag = '"%s"' % hashlib.sha1(version.encode("utf-8")).hexdigest()[:20]
        cache = "private, max-age=31536000, immutable" if immutable else "no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error(404, "Bild nicht gefunden.")
        self._send(200, body, "image/jpeg", cache=cache, headers={"ETag": etag})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host.split("]", 1)[0] + "]"
        else:
            host = host.split(":", 1)[0]
        return host in LOCAL_HOSTS

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Ungültige Länge der Anfrage") from None
        if n > MAX_BODY:
            raise ValueError("Anfrage zu groß")
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw.strip():
            return {}
        try:
            b = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Ungültiges JSON in der Anfrage") from None
        if not isinstance(b, dict):
            raise ValueError("Anfrage muss ein JSON-Objekt sein")
        return b

    def _parts(self):
        u = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        return parts, urllib.parse.parse_qs(u.query)

    def _guarded(self, fn, *args):
        try:
            return fn(*args)
        except NotFound as e:
            return self._error(404, str(e) or "nicht gefunden")
        except media.MediaError as e:
            return self._error(e.status, str(e))
        except ValueError as e:
            return self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception as e:  # noqa: BLE001 – Klartext an die Oberfläche, Einzelheiten ins Protokoll
            logbook.error("HTTP %s %s" % (self.command, self.path), kind_detail="http_error")
            return self._error(500, "Interner Fehler (%s: %s) – Einzelheiten im Protokoll." % (type(e).__name__, e))

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "Zugriff nur von diesem Rechner.")
        parts, q = self._parts()
        if parts == ["mcp"]:                      # MCP über HTTP: nur POST
            return self._send(405, b"", "text/plain", headers={"Allow": "POST"})
        if parts == ["api", "events"]:
            return self._events(q)
        return self._guarded(self._get, parts, q)

    def _get(self, parts: list[str], q: dict):
        c = self.core
        if not parts or parts == ["index.html"]:
            with open(os.path.join(HERE, "static", "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8", cache="no-cache")
        if parts[0] != "api":
            raise NotFound("nicht gefunden")
        api = parts[1:]
        if api == ["bootstrap"]:
            return self._json(bootstrap(self.server))
        if api == ["projects"]:
            return self._json(c.projects())
        if api == ["history"]:
            project = _str(q, "project")
            project = self._project(project) if project is not None else None
            return self._json(c.history(project, limit=_int(q, "limit", 200)))
        if api == ["tasks"]:
            return self._json(c.tasks.snapshot())
        if len(api) == 2 and api[0] == "thumb":
            path, version = media.thumb(c.ws, c.db, self._fid(api[1]))
            return self._image(path, version, immutable=_str(q, "v") == version)
        if len(api) == 3 and api[0] == "frame" and api[2] == "preview":
            path, version = media.preview(c.ws, c.db, self._fid(api[1]))
            return self._image(path, version, immutable=False)
        if len(api) >= 2 and api[0] == "projects":
            pid, rest = self._project(api[1]), api[2:]
            if not rest:
                return self._json(c.project(pid))
            if rest == ["frames"]:
                fr = c.frames(pid, type_=_str(q, "type", "light"), status=_str(q, "status"),
                              offset=_int(q, "offset", 0), limit=_int(q, "limit", 200))
                for f in fr["frames"]:                    # dauerhaft cachebare Bild-Adresse für die Kacheln
                    f["thumb_url"] = ("/api/thumb/%d?v=%s" % (f["id"], media.thumb_version(f["thumb"]))
                                      if f.get("thumb") else None)
                return self._json(fr)
            if rest == ["plan"]:
                return self._json(c.plan(pid))
            if rest == ["stacks"]:
                return self._json(c.stacks(pid))
        raise NotFound("nicht gefunden")

    def _project(self, key: str) -> int:
        """Projekt per ID oder Name; unbekannt → 404 (statt 400 aus ``Core``)."""
        try:
            return library.find_project(self.core.db, key)
        except library.LibraryError as e:
            raise NotFound(str(e)) from None

    def _stack(self, pid: int, raw: str) -> int:
        if not raw.isdigit() or self.core.db.value("SELECT COUNT(*) FROM stacks WHERE id = ? AND project_id = ?",
                                                  (int(raw), pid)) == 0:
            raise NotFound("Stack nicht gefunden.")
        return int(raw)

    @staticmethod
    def _fid(raw: str) -> int:
        if not raw.isdigit():
            raise NotFound("Datei nicht gefunden.")
        return int(raw)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "Zugriff nur von diesem Rechner.")
        parts, _ = self._parts()
        if parts == ["mcp"]:
            return self._mcp_post()
        if self.headers.get("X-Astrostack") != "1":
            return self._error(403, "Anfrage ohne Kennung abgelehnt.")
        return self._guarded(self._post, parts)

    def _post(self, parts: list[str]):
        c = self.core
        b = self._body()
        if not parts or parts[0] != "api":
            raise NotFound("nicht gefunden")
        api = parts[1:]
        if api == ["projects"]:
            return self._json(c.create_project(_text(b, "name"), _paths(b, "folders", required=False),
                                               _text(b, "description")))
        if api == ["settings"]:
            return self._json(save_settings(self.server, b))
        if api == ["backup"]:
            return self._json({"path": c.backup()})
        if api == ["clear-temp"]:
            return self._json({"removed": c.clear_temp()})
        if api == ["quit"]:
            return self._json(quit_server(self.server))
        if api in (["pick-folder"], ["pick-file"]):
            try:
                path = (dialogs().pick_folder(_text(b, "initial")) if api == ["pick-folder"] else
                        dialogs().pick_file(_text(b, "initial"), _text(b, "kind")))
            except RuntimeError as e:                 # kein Dialog verfügbar → Klartext, Pfad eintippen
                raise ValueError(str(e)) from None
            return self._json({"path": path})
        if api == ["check-pixinsight"]:
            return self._json(dialogs().check_pixinsight(_text(b, "path")))
        if api == ["mcp-info"]:
            from .. import mcp_http
            return self._json(mcp_http.info(self.server.url, mcp_http.ensure_token(Settings.load())))
        if api == ["mcp-token"]:
            from .. import mcp_http
            with SETTINGS_LOCK:
                tok = mcp_http.new_token(Settings.load())
            logbook.event("mcp_token_renewed", ui="web")
            return self._json(mcp_http.info(self.server.url, tok))
        if api == ["focus"]:
            from ..app import window
            return self._json({"ok": window.focus()})
        if len(api) == 3 and api[0] == "tasks" and api[2] == "cancel":
            t = c.tasks.cancel(api[1])
            if t is None:
                raise NotFound("Auftrag nicht gefunden.")
            return self._json(t.as_dict())
        if len(api) >= 3 and api[0] == "projects":
            pid, rest = self._project(api[1]), api[2:]
            if rest == ["open"]:
                return self._json(c.open_project(pid))
            if rest == ["rename"]:
                return self._json(c.rename(pid, _text(b, "name")))
            if rest == ["describe"]:
                c.describe(pid, _text(b, "text"))
                return self._json({"ok": True})
            if rest == ["folders"]:
                return self._json(c.add_folders(pid, _paths(b, "folders")))
            if len(rest) == 3 and rest[0] == "folders" and rest[2] == "remove":
                if not rest[1].isdigit():
                    raise ValueError("Ordner-ID muss eine Zahl sein")
                c.remove_folder(pid, int(rest[1]))
                return self._json({"ok": True})
            if rest == ["refresh"]:
                return self._json(c.refresh(pid, reason="von Hand").as_dict())
            if rest == ["decide"]:
                return self._json(c.decide(pid, _ids(b, "file_ids"), _text(b, "decision"), _text(b, "reason")))
            if rest == ["plan"]:
                return self._json(c.edit_plan(pid, _dict(b, "options"), _dict(b, "runs"), bool(b.get("reset"))))
            if rest == ["stack"]:
                run_ids = _list(b, "run_ids", required=False) or None
                if run_ids and not all(isinstance(x, str) for x in run_ids):
                    raise ValueError("run_ids: nur Lauf-Kennungen (Text) erlaubt")
                return self._json(c.stack(pid, run_ids))
            if len(rest) == 3 and rest[0] == "stacks" and rest[2] in ("note", "delete"):
                sid = self._stack(pid, rest[1])
                if rest[2] == "note":
                    c.note(pid, sid, _text(b, "text"))
                    return self._json({"ok": True})
                return self._json({"removed": c.delete_stack(pid, sid)})
        raise NotFound("nicht gefunden")

    def _mcp_post(self):
        """MCP über HTTP: Origin und Schlüssel prüfen, JSON-RPC verarbeiten (wie die alte App)."""
        from .. import mcp_http
        try:
            if not mcp_http.origin_ok(self.headers):
                return self._error(403, "forbidden origin")
            if not mcp_http.authorized(self.headers, mcp_http.ensure_token(Settings.load())):
                return self._send(401, _json_bytes({"error": "Schlüssel fehlt oder ist falsch (Einstellungen → "
                                                             "KI-Bedienung)"}),
                                  "application/json; charset=utf-8", headers={"WWW-Authenticate": 'Bearer realm="b2stacker"'})
            n = int(self.headers.get("Content-Length") or 0)
            code, body = self.server.mcp_handler().handle_body(self.rfile.read(n) if n > 0 else b"")
            if body is None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json(body, code)
        except Exception as e:  # noqa: BLE001
            logbook.error("MCP über HTTP", kind_detail="mcp_http")
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}, 500)

    # ---------------------------------------------------------------- Server-Sent Events
    def _client_gone(self) -> bool:
        """Hat der Browser die Verbindung geschlossen? (lesbarer Socket ohne Daten = EOF)"""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if not r:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _events(self, q: dict):
        """Live-Ereignisse aus ``core.tasks.hub``. Wiederaufnahme über ``Last-Event-ID`` oder ``?last=``;
        ist die Lücke nicht mehr vollständig im Puffer, kommt ``reset`` (Client lädt ``/api/bootstrap`` neu)."""
        hub = self.core.tasks.hub
        srv = self.server
        raw = (self.headers.get("Last-Event-ID") or _str(q, "last") or "").strip()
        reset = False
        if not raw:
            last = hub.last_id
        else:
            try:
                last = int(raw)
            except ValueError:
                last, reset = hub.last_id, True
            if last > hub.last_id or last < 0:         # Nummer aus einem früheren Programmlauf
                last, reset = hub.last_id, True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n: verbunden\n\n")
            if reset:
                self.wfile.write(sse_format(last, "reset", json.dumps({"last_event": last})))
            next_beat = time.monotonic() + srv.heartbeat_s
            while not srv.stopping.is_set() and self.core.tasks.hub is hub:   # neues Arbeitsverzeichnis → neu verbinden
                items, complete = hub.wait(last, srv.poll_s)
                if not complete:
                    last = hub.last_id
                    self.wfile.write(sse_format(last, "reset", json.dumps({"last_event": last})))
                    next_beat = time.monotonic() + srv.heartbeat_s
                    continue
                if items:
                    self.wfile.write(b"".join(sse_format(seq, kind, json.dumps(data, ensure_ascii=False, default=str))
                                              for seq, kind, data in items))
                    last = items[-1][0]
                    next_beat = time.monotonic() + srv.heartbeat_s
                elif time.monotonic() >= next_beat:
                    self.wfile.write(b": ping\n\n")
                    next_beat = time.monotonic() + srv.heartbeat_s
                if self._client_gone():
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        self.close_connection = True


# ------------------------------------------------------------------ Start

def already_running(port: int) -> str | None:
    """Läuft der neue Server schon auf diesem Port? Dann nur den Browser öffnen."""
    import urllib.request
    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url + "api/bootstrap", timeout=1.5) as r:
            if json.loads(r.read()).get("version"):
                return url
    except Exception:  # noqa: BLE001
        return None
    return None


def main(port: int = PORT, open_browser: bool = True) -> None:
    url = already_running(port)
    if url:
        print("b2stacker (neue Oberfläche) läuft bereits: " + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    s = Settings.load()
    logbook.setup(s.workdir)
    logbook.event("app_start", ui="web", workdir=s.workdir, pixinsight=s.pixinsight,
                  pixinsight_ok=os.path.exists(s.pixinsight), frozen=bool(getattr(sys, "frozen", False)),
                  python=sys.version.split()[0], platform=sys.platform)
    srv = create(port, s)
    print("b2stacker (neue Oberfläche) läuft: " + srv.url + "  (Strg+C beendet das Programm)", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(srv.url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping.set()
        srv.server_close()
        srv.core.close()
        logbook.event("app_exit", ui="web")
