"""Eigenes Programmfenster (statt Browser): pywebview mit Microsofts WebView2 (in Windows 10/11 enthalten).

Ablauf beim Doppelklick auf die exe:
  1. Läuft b2stacker schon? → dieses Fenster nach vorne holen, fertig.
  2. Lokalen Server im Hintergrund starten (127.0.0.1, nur dieser Rechner).
  3. Programmfenster öffnen. Schließen beendet das Programm – bei laufendem Auftrag mit Rückfrage.
Fällt WebView2 aus, öffnet sich ein randloses Edge-App-Fenster, notfalls der Standard-Browser.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from .. import __version__, logbook
from . import server

STATE = {"window": None}
TITLE = "b2stacker"


def _post(url: str, path: str):
    req = urllib.request.Request(url + path, data=b"{}", method="POST",
                                 headers={"X-Astrostack": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read() or b"{}")


# ------------------------------------------------------------ vom Server aufgerufen

def pick_folder(initial: str = "") -> str | None | bool:
    """Nativer Ordner-Dialog im Programmfenster. False = kein Fenster (Server nimmt dann seinen Weg)."""
    w = STATE["window"]
    if w is None:
        return False
    import webview
    kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
    if kind is None:
        kind = webview.FOLDER_DIALOG
    res = w.create_file_dialog(kind, directory=initial or "")
    if not res:
        return None
    p = res[0] if isinstance(res, (list, tuple)) else res
    return os.path.normpath(p)


def focus() -> bool:
    w = STATE["window"]
    if w is None:
        return False
    try:
        w.restore()
        w.show()
        w.on_top = True
        time.sleep(0.3)
        w.on_top = False
    except Exception:  # noqa: BLE001
        pass
    return True


# ------------------------------------------------------------ Start

def _storage_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "b2stacker", "webview")
    os.makedirs(d, exist_ok=True)
    return d


def _start_server(port: int) -> str:
    httpd = server.serve(port, open_browser=False)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    STATE["httpd"] = httpd
    return "http://127.0.0.1:%d/" % httpd.server_address[1]


def _edge_app(url: str) -> bool:
    """Ersatz: Edge im App-Modus (eigenes Fenster ohne Adressleiste)."""
    if sys.platform != "win32":
        return False
    for exe in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if os.path.exists(exe):
            subprocess.Popen([exe, "--app=" + url, "--window-size=1440,920"], creationflags=0x08000000)
            return True
    return False


def run(port: int = 8765):
    url = server.already_running(port)
    attach = False
    if url:                                   # läuft schon: vorhandenes Fenster nach vorne holen …
        try:
            if _post(url, "api/focus").get("ok"):
                return
        except Exception:  # noqa: BLE001
            pass
        attach = True                         # … oder (Server ohne Fenster) ein Fenster dafür öffnen
    else:
        url = _start_server(port)
    logbook.event("window_start", url=url, attach=attach)
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        logbook.error("Programmfenster nicht verfügbar (pywebview): %s" % e)
        _fallback(url)
        return

    w = webview.create_window(TITLE + " " + __version__, url, width=1440, height=920, min_size=(900, 600),
                              background_color="#0e0f12", text_select=True)
    STATE["window"] = w

    def on_closing():
        j = None if attach else server.JOBS.active()
        if not j:
            return True
        ok = w.create_confirmation_dialog(
            "b2stacker beenden?",
            "Es läuft gerade: %s.\n\nBeim Schließen wird der Auftrag abgebrochen (PixInsight wird beendet). "
            "Trotzdem schließen?" % j.title)
        if not ok:
            return False
        j.cancel.set()
        j.write("Abbruch: Programmfenster wurde geschlossen")
        for _ in range(40):                    # PixInsight sauber beenden lassen (max. ~10 s)
            if j.state not in ("queued", "running"):
                break
            time.sleep(0.25)
        return True

    def on_loaded():
        logbook.event("window_loaded", url=url)
        auto = os.environ.get("B2_AUTOCLOSE")
        if auto:                               # für den automatischen Test auf GitHub
            threading.Timer(float(auto), w.destroy).start()

    w.events.closing += on_closing
    w.events.loaded += on_loaded
    w.events.shown += lambda: logbook.event("window_shown")
    try:
        logbook.event("webview_start", gui=getattr(webview, "guilib", None) and str(webview.guilib))
        webview.start(private_mode=False, storage_path=_storage_dir(), debug=bool(os.environ.get("B2_DEBUG")))
    except Exception as e:  # noqa: BLE001
        logbook.error("Programmfenster konnte nicht starten: %s" % e)
        STATE["window"] = None
        _fallback(url)
        return
    STATE["window"] = None
    logbook.event("app_exit")


def _fallback(url: str):
    """Ohne WebView2: Edge-App-Fenster oder Browser – Server läuft weiter, bis der Prozess beendet wird."""
    if not _edge_app(url):
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
