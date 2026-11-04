"""MCP über HTTP („Streamable HTTP“) direkt in der laufenden App – für OpenCode, Claude Code und andere Agenten.

- Adresse: ``http://127.0.0.1:<port>/mcp`` (nur dieser Rechner).
- Jeder Aufruf braucht ``Authorization: Bearer <schlüssel>``; der Schlüssel steht in den Einstellungen
  (KI-Bedienung) und lässt sich dort neu erzeugen. Ohne Schlüssel kann keine Webseite und kein fremdes
  Programm b2stacker steuern; Anfragen mit fremder ``Origin`` werden zusätzlich abgelehnt.
- Antworten kommen als einfaches JSON (keine Server-Sent Events nötig): eine JSON-RPC-Nachricht oder eine
  Liste davon rein, die Antwort(en) zurück. Benachrichtigungen → 202 ohne Inhalt.
- Dieselben Werkzeuge und Regeln wie beim stdio-Server (``mcp.Tools``); Aufträge tragen „· per Claude“.

Der stdio-Server, den die Claude-Desktop-App startet, leitet an diesen Endpunkt weiter: Werkzeugliste und
Verhalten kommen so immer von der laufenden App, auch wenn die eingetragene exe älter ist.
"""
from __future__ import annotations

import hmac
import json
import secrets

PATH = "/mcp"


def ensure_token(settings) -> str:
    """Schlüssel aus den Einstellungen; beim ersten Mal erzeugen und speichern."""
    tok = getattr(settings, "mcp_token", "") or ""
    if len(tok) < 24:
        tok = secrets.token_urlsafe(32)
        settings.mcp_token = tok
        settings.save()
    return tok


def new_token(settings) -> str:
    settings.mcp_token = secrets.token_urlsafe(32)
    settings.save()
    return settings.mcp_token


def authorized(headers, token: str) -> bool:
    auth = headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer ") or not token:
        return False
    return hmac.compare_digest(auth[7:].strip().encode(), token.encode())


def origin_ok(headers) -> bool:
    """Schutz gegen DNS-Rebinding: Browser senden Origin – erlaubt sind nur lokale Seiten (oder keine)."""
    o = (headers.get("Origin") or "").lower()
    if not o:
        return True
    host = o.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "[::1]")


def info(base_url: str, token: str) -> dict:
    """Alles, was die Einstellungen anzeigen: URL, Schlüssel und fertige Konfig-Schnipsel."""
    url = base_url.rstrip("/") + PATH
    header = {"Authorization": "Bearer " + token}
    return dict(
        url=url, token=token,
        opencode=json.dumps({"mcp": {"b2stacker": {"type": "remote", "url": url, "enabled": True,
                                                   "headers": header}}}, indent=2),
        claude_code="claude mcp add --transport http b2stacker %s --header \"Authorization: Bearer %s\"" % (url, token),
        generic=json.dumps({"mcpServers": {"b2stacker": {"type": "http", "url": url, "headers": header}}}, indent=2),
        note="Nur auf diesem Rechner erreichbar. Den Schlüssel wie ein Passwort behandeln.")


class Handler:
    """Verarbeitet einen HTTP-Body mit JSON-RPC – synchron, ohne eigenen Thread-Pool."""

    def __init__(self, server):
        self.server = server                      # mcp.Server (liefert handle/call_tool)

    def handle_body(self, raw: bytes) -> tuple[int, object | None]:
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace") or "null")
        except ValueError:
            return 400, self.server._err(None, -32700, "Ungültiges JSON")
        batch = isinstance(msg, list)
        out = []
        for m in (msg if batch else [msg]):
            if not isinstance(m, dict):
                out.append(self.server._err(None, -32600, "Ungültige Anfrage"))
                continue
            r = self.one(m)
            if r is not None:
                out.append(r)
        if not out:
            return 202, None
        return 200, (out if batch else out[0])

    def one(self, m: dict):
        if m.get("method") == "tools/call" and m.get("id") is not None:
            p = m.get("params") or {}
            name, args = p.get("name"), p.get("arguments") or {}
            if name not in self.server.known:
                return self.server._err(m["id"], -32602, "Unbekanntes Werkzeug: %s" % name)
            if not isinstance(args, dict):
                return self.server._err(m["id"], -32602, "arguments muss ein Objekt sein")
            return self.server._ok(m["id"], self.server.call_tool(name, args))
        try:
            return self.server.handle(m)
        except Exception as e:  # noqa: BLE001
            return self.server._err(m.get("id"), -32603, str(e))
