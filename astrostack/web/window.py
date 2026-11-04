"""Programmfenster (pywebview) für den neuen Server – wie ``app/window.py`` für die alte App.

Wird beim Doppelklick genutzt, wenn ``B2_NEW_UI=1`` gesetzt ist (siehe ``__main__.py``). Ablauf:
  1. Läuft der neue Server schon? → dessen Fenster nach vorne holen, fertig.
  2. Server im Hintergrund starten (127.0.0.1), alle Projekte im Hintergrund prüfen.
  3. Fenster öffnen. Schließen beendet das Programm – bei laufendem Auftrag mit Rückfrage.

Ordner-/Datei-Dialoge laufen über ``app.window.STATE["window"]``: dieselben Funktionen wie in der alten App.
"""
from __future__ import annotations

import os
import threading
import time

from .. import __version__, logbook
from ..app import window as oldwin
from ..settings import Settings
from . import server


def _start(port: int) -> server.WebServer:
    s = Settings.load()
    logbook.setup(s.workdir)
    logbook.event("app_start", ui="web-window", workdir=s.workdir, pixinsight=s.pixinsight)
    srv = server.create(port, s)
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    return srv


def run(port: int = server.PORT) -> None:
    url = server.already_running(port)
    srv = None
    if url:                                    # läuft schon: vorhandenes Fenster nach vorne holen …
        try:
            if oldwin._post(url, "api/focus").get("ok"):
                return
        except Exception:  # noqa: BLE001
            pass
    else:
        srv = _start(port)
        url = srv.url
    logbook.event("window_start", url=url, ui="web", attach=srv is None)
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        logbook.error("Programmfenster nicht verfügbar (pywebview): %s" % e)
        oldwin._fallback(url)
        return

    w = webview.create_window(oldwin.TITLE + " " + __version__, url, width=1440, height=920, min_size=(900, 600),
                              background_color="#0b0b0c", text_select=True)
    oldwin.STATE["window"] = w

    def on_closing():
        active = [t for t in srv.core.tasks.active() if t.state == "running"] if srv else []
        if not active:
            return True
        ok = w.create_confirmation_dialog(
            "b2stacker beenden?",
            "Es läuft gerade: %s.\n\nBeim Schließen wird der Auftrag abgebrochen. Trotzdem schließen?" % active[0].title)
        if not ok:
            return False
        for t in srv.core.tasks.active():
            srv.core.tasks.cancel(t.id)
        for _ in range(40):                    # PixInsight sauber beenden lassen (max. ~10 s)
            if not [t for t in srv.core.tasks.active() if t.state == "running"]:
                break
            time.sleep(0.25)
        return True

    def on_loaded():
        logbook.event("window_loaded", url=url, ui="web")
        auto = os.environ.get("B2_AUTOCLOSE")
        if auto:                               # für den automatischen Test auf GitHub
            threading.Timer(float(auto), w.destroy).start()

    w.events.closing += on_closing
    w.events.loaded += on_loaded
    try:
        webview.start(private_mode=False, storage_path=oldwin._storage_dir(), debug=bool(os.environ.get("B2_DEBUG")))
    except Exception as e:  # noqa: BLE001
        logbook.error("Programmfenster konnte nicht starten: %s" % e)
        oldwin.STATE["window"] = None
        oldwin._fallback(url)
        return
    finally:
        oldwin.STATE["window"] = None
    if srv is not None:
        srv.stopping.set()
        srv.server_close()
        srv.core.close()
    logbook.event("app_exit", ui="web")
