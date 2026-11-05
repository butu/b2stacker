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
                "pick-files", "check-pixinsight", "mcp-info", "mcp-token", "lan", "lan-key", "lan-info", "lan-firewall", "focus",
                "clear-temp", "selftest", "library", "claude"}
BLOCKED_PROJECT_POST = {"delete-frames", "import-stacks"}   # Pfade am Computer wählen
BLOCKED_STACK_POST = {"open-folder"}               # öffnet ein Fenster am Computer – vom Handy sinnlos


CGNAT = ipaddress.ip_network("100.64.0.0/10")         # Tailscale und andere VPNs (nie im Internet geroutet)


def private_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in CGNAT)


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
    segno.make(text, error="m").save(buf, kind="svg", border=2, dark="#000", light="#fff", xmldecl=False,
                                     svgns=True, omitsize=True)      # nur viewBox → skaliert in jede Box
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

    rejected: dict = {}
    seen: dict = {}                 # jede ankommende Verbindung (für den Verbindungstest in den Einstellungen)

    def verify_request(self, request, client_address) -> bool:
        import time as _t
        LanServer.seen[client_address[0]] = _t.time()
        ok = private_client(client_address[0])
        if not ok:                                     # sichtbar machen statt still abbrechen
            import time
            first = client_address[0] not in LanServer.rejected
            LanServer.rejected[client_address[0]] = time.time()
            if first:
                try:
                    from .. import logbook
                    logbook.event("lan_rejected", client=client_address[0])
                except Exception:  # noqa: BLE001
                    pass
        return ok

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.primary.core.db.release()

    @property
    def url(self) -> str:
        ips = lan_ips()
        return "http://%s:%d/" % (ips[0] if ips else "127.0.0.1", self.server_address[1])


# ------------------------------------------------------------------ Windows-Firewall

FW_RULE = "b2stacker (Handy im Heimnetz)"
FW_REMOTE = "LocalSubnet,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7,fe80::/10"


def _ps(script: str, timeout: float = 20) -> str:
    import subprocess
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       timeout=timeout, creationflags=0x08000000)
    return (r.stdout or b"").decode("utf-8", "replace")


def firewall_status(exe: str | None, port: int) -> dict | None:
    """Windows: Netzwerk-Profil (Privat/Öffentlich) und Firewall-Regeln für b2stacker. Ohne Windows: None.
    Häufigste Ursache, wenn das Handy nichts erreicht: Der Firewall-Hinweis beim ersten Start wurde weggeklickt
    (dann legt Windows eine Sperr-Regel an) oder das WLAN ist als „Öffentlich“ eingestuft."""
    import json
    import sys
    if sys.platform != "win32":
        return None
    out: dict = dict(exe=exe, port=port, rules=[], networks=[], problems=[])
    esc = (exe or "").replace("'", "''")
    try:
        txt = _ps("$ErrorActionPreference='SilentlyContinue';"
                  "$r=@(); $r+=Get-NetFirewallApplicationFilter -Program '%s' | Get-NetFirewallRule;"
                  "$r+=Get-NetFirewallRule -DisplayName '%s';"
                  "$r | Where-Object {$_} | Sort-Object Name -Unique | ForEach-Object { $p=$_ | Get-NetFirewallPortFilter;"
                  " [pscustomobject]@{name=$_.DisplayName; enabled=\"$($_.Enabled)\"; dir=\"$($_.Direction)\";"
                  " action=\"$($_.Action)\"; profile=\"$($_.Profile)\"; proto=\"$($p.Protocol)\";"
                  " port=\"$($p.LocalPort)\"} } | ConvertTo-Json -Compress" % (esc, FW_RULE))
        rules = json.loads(txt) if txt.strip() else []
        out["rules"] = rules if isinstance(rules, list) else [rules]
        txt = _ps("Get-NetConnectionProfile | ForEach-Object { [pscustomobject]@{name=$_.Name;"
                  " alias=$_.InterfaceAlias; category=\"$($_.NetworkCategory)\"} } | ConvertTo-Json -Compress")
        nets = json.loads(txt) if txt.strip() else []
        out["networks"] = nets if isinstance(nets, list) else [nets]
        txt = _ps("Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and "
                  "$_.IPAddress -notlike '169.254.*' } | ForEach-Object { [pscustomobject]@{alias=$_.InterfaceAlias;"
                  " ip=$_.IPAddress; prefix=$_.PrefixLength} } | ConvertTo-Json -Compress")
        ads = json.loads(txt) if txt.strip() else []
        out["adapters"] = ads if isinstance(ads, list) else [ads]
        txt = _ps("Get-CimInstance -Namespace root/SecurityCenter2 -ClassName FirewallProduct -ErrorAction "
                  "SilentlyContinue | ForEach-Object { $_.displayName } | ConvertTo-Json -Compress")
        prods = json.loads(txt) if txt.strip() else []
        out["security"] = [p for p in (prods if isinstance(prods, list) else [prods]) if isinstance(p, str) and p]
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
        return out
    inbound = [r for r in out["rules"] if r.get("dir") == "Inbound" and r.get("enabled") == "True"]
    blocks = [r for r in inbound if r.get("action") == "Block"]
    allows = [r for r in inbound if r.get("action") == "Allow"
              and r.get("proto") in ("TCP", "Any", "") and r.get("port") in (str(port), "Any", "")]
    public = [n for n in out["networks"] if n.get("category") == "Public"]
    if blocks:
        out["problems"].append("Die Windows-Firewall sperrt b2stacker (%d Sperr-Regel%s – entsteht, wenn der "
                               "Firewall-Hinweis beim ersten Start abgelehnt oder weggeklickt wurde)."
                               % (len(blocks), "" if len(blocks) == 1 else "n"))
    if not allows:
        out["problems"].append("Keine Freigabe in der Windows-Firewall für eingehende Verbindungen auf Port %d." % port)
    elif public and not any(("Any" in (r.get("profile") or "") or "Public" in (r.get("profile") or "")) for r in allows):
        out["problems"].append("Der Netzwerkadapter „%s“ ist in Windows als „Öffentlich“ eingestuft, die Freigabe gilt "
                               "aber nur für private Netzwerke." % (public[0].get("alias") or public[0].get("name")))
    if out.get("security"):
        out["problems"].append("Zusätzliche Firewall aktiv: %s. Sie kann das Handy auch dann sperren, wenn die "
                               "Windows-Firewall aus ist – dort b2stacker.exe bzw. Port %d im Heimnetz erlauben."
                               % (", ".join(out["security"]), port))
    out["ok"] = not out["problems"]
    return out


def firewall_fix(exe: str, port: int) -> None:
    """Sperr-Regeln für b2stacker entfernen und eine Freigabe nur für Heimnetz-/VPN-Adressen anlegen.
    Braucht Administratorrechte: Windows fragt einmal nach (UAC)."""
    import os
    import subprocess
    import tempfile
    esc = exe.replace("'", "''")
    script = ("$ErrorActionPreference='SilentlyContinue'\n"
              "Get-NetFirewallApplicationFilter -Program '%s' | Get-NetFirewallRule | "
              "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | Remove-NetFirewallRule\n"
              "Remove-NetFirewallRule -DisplayName '%s'\n"
              "New-NetFirewallRule -DisplayName '%s' -Direction Inbound -Action Allow -Program '%s' -Protocol TCP "
              "-LocalPort %d -Profile Any -RemoteAddress %s | Out-Null\n"
              % (esc, FW_RULE, FW_RULE, esc, int(port), FW_REMOTE))
    path = os.path.join(tempfile.gettempdir(), "b2stacker_firewall.ps1")
    with open(path, "w", encoding="utf-8-sig") as fh:
        fh.write(script)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                        "'-NoProfile','-ExecutionPolicy','Bypass','-File','%s'" % path.replace("'", "''")],
                       capture_output=True, timeout=180, creationflags=0x08000000)
    try:
        os.remove(path)
    except OSError:
        pass
    if r.returncode != 0:
        raise ValueError("Freigabe nicht eingerichtet – die Windows-Nachfrage wurde abgelehnt oder ist "
                         "fehlgeschlagen.")
