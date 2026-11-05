"""b2stacker vom Handy im Heimnetz bedienen.

Der Hauptserver lauscht nur auf 127.0.0.1. Ist „Im Heimnetz erreichbar“ an, kommt ein zweiter Zugang dazu
(``LanServer``, alle Netzwerkkarten, eigener Port – Standard 8875), der dieselbe ``Core`` benutzt. Regeln:

- Nur Geräte mit privater Adresse (10.x, 172.16–31.x, 192.168.x, fe80::/fd00::) – nie aus dem Internet.
- Nur gekoppelte Geräte: Der QR-Code in den Einstellungen enthält ``/pair?k=<schlüssel>``; das Handy bekommt
  den Schlüssel als Cookie (HttpOnly, SameSite=Strict, ein Jahr). „Handys abmelden“ erzeugt einen neuen Schlüssel.
  Das Cookie schützt auch gegen DNS-Rebinding (der Browser schickt es nur an genau diese Adresse).
- Was am Handy nicht geht: Rohdaten löschen, Einstellungen, Sicherung zurückspielen, Beenden, Update/Installation,
  Datei-Dialoge, Kalibrier-Master entfernen, MCP. Ansehen, Frames entscheiden, Plan ändern und Stacken gehen.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import socket
from http.server import ThreadingHTTPServer

COOKIE = "b2key"

# POST-Ziele unter /api/…, die vom Handy aus abgelehnt werden (erstes bzw. letztes Pfadstück)
BLOCKED_POST = {"settings", "backups", "backup", "quit", "install", "update", "pick-folder", "pick-file",
                "pick-files", "check-pixinsight", "mcp-info", "mcp-token", "lan", "lan-key", "lan-info", "focus",
                "clear-temp", "selftest", "library"}
BLOCKED_PROJECT_POST = {"delete-frames"}


def private_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def lan_ips() -> list[str]:
    """IPv4-Adressen dieses Rechners im Heimnetz (die erste ist die mit der Standard-Route)."""
    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))           # sendet nichts – wählt nur die passende Netzwerkkarte
            out.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out:
                out.append(ip)
    except OSError:
        pass
    return [ip for ip in out if not ip.startswith("127.") and private_client(ip)]


def new_key() -> str:
    return secrets.token_urlsafe(24)


def key_ok(cookie_header: str | None, key: str) -> bool:
    if not key or not cookie_header:
        return False
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and hmac.compare_digest(value.encode(), key.encode()):
            return True
    return False


def cookie(key: str) -> str:
    return "%s=%s; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict" % (COOKIE, key)


def qr_svg(text: str) -> str:
    """QR-Code als SVG (weiß auf dunkel lesbar: schwarze Module auf weißem Grund)."""
    import io

    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff",
                                     xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")


class LanServer(ThreadingHTTPServer):
    """Zweiter Zugang für das Heimnetz. Alle anderen Attribute (``core``, ``restoring`` …) kommen vom Hauptserver,
    damit ein Wechsel der ``Core`` (Wiederherstellen, neues Arbeitsverzeichnis) hier sofort mitgilt."""

    daemon_threads = True
    allow_reuse_address = False
    lan = True

    def __init__(self, primary, port: int, handler):
        self.primary = primary
        super().__init__(("0.0.0.0", port), handler)

    def __getattr__(self, name):
        if name == "primary":
            raise AttributeError(name)
        return getattr(self.primary, name)

    def verify_request(self, request, client_address) -> bool:
        return private_client(client_address[0])

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.primary.core.db.release()

    @property
    def url(self) -> str:
        ips = lan_ips()
        return "http://%s:%d/" % (ips[0] if ips else "127.0.0.1", self.server_address[1])
