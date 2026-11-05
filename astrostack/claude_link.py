"""b2stacker in der Claude-Desktop-App als MCP-Server eintragen (oder wieder austragen).

Die Datei ``claude_desktop_config.json`` gehört der Claude-App und enthält evtl. andere Server und Einstellungen:
nur der Eintrag ``mcpServers.b2stacker`` wird geändert, vorher wird eine Sicherung angelegt, und eine
unlesbare Datei wird nie überschrieben.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

KEY = "b2stacker"
FILE = "claude_desktop_config.json"


def config_candidates() -> list[str]:
    """Mögliche Orte der Konfiguration (normale Installation und Microsoft-Store-Version unter Windows)."""
    if os.environ.get("B2_CLAUDE_CONFIG"):                     # Tests / Sonderfälle
        return [os.environ["B2_CLAUDE_CONFIG"]]
    out = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        out.append(os.path.join(appdata, "Claude", FILE))
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        # Store-/MSIX-Installation (Paketname je nach Version) und ältere Installationsorte
        # Store-/MSIX-Installation: der Paketordner existiert auch, wenn die Konfiguration noch fehlt
        for pkg in sorted(glob.glob(os.path.join(local, "Packages", "Claude_*"))
                          + glob.glob(os.path.join(local, "Packages", "*Claude*"))):
            hit = os.path.join(pkg, "LocalCache", "Roaming", "Claude", FILE)
            if hit not in out:
                out.append(hit)
        for pattern in (("AnthropicClaude", FILE), ("Claude", FILE)):
            for hit in sorted(glob.glob(os.path.join(local, *pattern))):
                if hit not in out:
                    out.append(hit)
    elif sys.platform == "darwin":
        out.append(os.path.expanduser("~/Library/Application Support/Claude/" + FILE))
    else:
        out.append(os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "Claude", FILE))
    return out


def server_entry(command: str | None = None) -> dict:
    """Wie die Claude-App b2stacker startet: die installierte exe (``command``), sonst dieselbe exe bzw. derselbe
    Python wie jetzt."""
    if command:
        entry = {"command": command, "args": ["mcp"]}
    elif getattr(sys, "frozen", False):
        entry = {"command": sys.executable, "args": ["mcp"]}
    else:
        entry = {"command": sys.executable, "args": ["-m", "astrostack", "mcp"]}
    if os.environ.get("ASTROSTACK_SETTINGS"):
        entry["env"] = {"ASTROSTACK_SETTINGS": os.environ["ASTROSTACK_SETTINGS"]}
    return entry


def _targets() -> list[str]:
    """Vorhandene Konfigurationsdateien; sonst die erste, deren Claude-Ordner existiert."""
    cands = config_candidates()
    have = [p for p in cands if os.path.exists(p)]
    if have:
        return have
    dirs = [p for p in cands if os.path.isdir(os.path.dirname(p))]
    return dirs[:1]


def _read(p: str) -> dict:
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return {}
    try:
        with open(p, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError("Die Claude-Konfiguration %s ist nicht lesbar (%s) – sie wird nicht verändert. "
                         "Bitte in Claude unter Einstellungen → Entwickler prüfen." % (p, e)) from None
    if not isinstance(d, dict):
        raise ValueError("Die Claude-Konfiguration %s hat ein unerwartetes Format – sie wird nicht verändert." % p)
    return d


def _write(p: str, d: dict) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        bak = p + ".b2stacker-" + time.strftime("%Y%m%d-%H%M%S") + ".bak"
        with open(p, "rb") as a, open(bak, "wb") as b:
            b.write(a.read())
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def status() -> dict:
    """Ist b2stacker eingetragen, und zeigt der Eintrag auf dieses Programm?"""
    cands = config_candidates()
    claude = any(os.path.isdir(os.path.dirname(p)) for p in cands)
    if not claude and sys.platform == "win32":     # MSIX: Paket da, aber LocalCache\Roaming\Claude noch nicht
        claude = any(os.path.isdir(p.split(os.sep + "LocalCache")[0]) for p in cands if os.sep + "LocalCache" in p)
    want = server_entry()
    files = []
    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            e = (_read(p).get("mcpServers") or {}).get(KEY)
        except ValueError as err:
            files.append(dict(file=p, error=str(err)))
            continue
        files.append(dict(file=p, installed=bool(e), current=bool(e) and e.get("command") == want["command"]
                          and e.get("args") == want["args"], command=e and e.get("command")))
    installed = any(f.get("installed") for f in files)
    current = installed and all(f.get("current") for f in files if f.get("installed"))
    if not claude:
        text = "Claude-Desktop-App nicht gefunden (gesucht: %s)." % "; ".join(os.path.dirname(p) for p in cands)
    elif not installed:
        text = "Nicht verbunden."
    elif not current:
        text = "Verbunden, aber mit einem anderen Programmpfad – bitte neu verbinden."
    else:
        text = "Verbunden. Claude kann b2stacker bedienen."
    return dict(claude_found=claude, installed=installed, current=current, files=files, entry=want, text=text)


def install(command: str | None = None) -> dict:
    targets = _targets()
    if not targets:
        raise ValueError("Claude-Desktop-App nicht gefunden. Gesucht in: %s. Falls Claude installiert ist: in Claude "
                         "unter Einstellungen → Entwickler → „Konfiguration bearbeiten“ den Ordner öffnen und den Pfad "
                         "melden – oder dort beim Eintrag „b2stacker“ den Programmpfad von Hand setzen: %s"
                         % ("; ".join(config_candidates()) or "–", server_entry()["command"]))
    for p in targets:
        d = _read(p)                                         # unlesbar → Abbruch, nichts wird geschrieben
        servers = d.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            raise ValueError("mcpServers in %s hat ein unerwartetes Format – nichts geändert." % p)
        d["mcpServers"] = dict(servers or {}, **{KEY: server_entry(command)})
        _write(p, d)
    return dict(status(), changed=targets,
                next="Claude-App ganz beenden (auch im Infobereich) und neu starten, damit sie b2stacker lädt.")


def uninstall() -> dict:
    changed = []
    for p in config_candidates():
        if not os.path.exists(p):
            continue
        d = _read(p)
        if KEY in (d.get("mcpServers") or {}):
            d["mcpServers"] = {k: v for k, v in d["mcpServers"].items() if k != KEY}
            _write(p, d)
            changed.append(p)
    return dict(status(), changed=changed)
