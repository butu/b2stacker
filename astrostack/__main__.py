"""Einstiegspunkt für `python -m astrostack` und die fertige AstroStack.exe.

Ohne Argumente (Doppelklick) startet die Oberfläche.
"""
import multiprocessing
import os
import sys


def _attach_std_streams():
    """Die exe hat kein Konsolenfenster. Für die Kommandozeile (z. B. KI-Agenten) Ausgaben trotzdem liefern:
    an eine Umleitung/Pipe des Aufrufers schreiben oder an dessen Konsole andocken."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes
    import io
    import msvcrt
    k32 = ctypes.windll.kernel32
    attached = False
    for name, std in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        h = k32.GetStdHandle(std)
        stream = None
        if h not in (0, -1, None) and k32.GetFileType(h) in (1, 3):      # Datei oder Pipe
            try:
                fd = msvcrt.open_osfhandle(h, 0)
                stream = io.TextIOWrapper(os.fdopen(fd, "wb", buffering=0), encoding="utf-8", errors="replace",
                                          line_buffering=True)
            except OSError:
                stream = None
        if stream is None:
            if not attached:
                attached = bool(k32.AttachConsole(-1))                     # Konsole des Aufrufers
            if attached:
                try:
                    stream = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                except OSError:
                    stream = None
        if stream is not None:
            setattr(sys, name, stream)


def run():
    multiprocessing.freeze_support()          # nötig für die Vermessung in Unterprozessen (exe)
    if len(sys.argv) >= 2:
        _attach_std_streams()
    for stream in (sys.stdout, sys.stderr):   # umgeleitete Ausgabe unter Windows (cp1252): nie an Sonderzeichen abstürzen
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    if len(sys.argv) >= 2 and sys.argv[1] == "_pick-folder":
        from .app.server import _pick_folder_dialog
        _pick_folder_dialog(sys.argv[2] if len(sys.argv) > 2 else "")
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "_selftest":
        sys.exit(selftest())
    if len(sys.argv) == 1 or sys.argv[1:] == ["app"]:      # Doppelklick → eigenes Programmfenster
        from .app import window
        window.run()
        return
    from .cli import main
    main()


def _webview2_version():
    import winreg
    keys = [r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"]
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for k in keys:
            try:
                with winreg.OpenKey(root, k) as h:
                    return winreg.QueryValueEx(h, "pv")[0]
            except OSError:
                continue
    raise RuntimeError("nicht gefunden – Ersatz: Edge-App-Fenster")


def selftest() -> int:
    """Prüft die fertige exe: Ressourcen, Bibliotheken, Mond-Berechnung, Ordner-Dialog-Modul."""
    import os
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            r = fn()
            print("OK    %-28s %s" % (name, r if r is not None else ""))
        except Exception as e:  # noqa: BLE001
            ok = False
            print("FEHLER %-27s %s: %s" % (name, type(e).__name__, e))
    base = os.path.dirname(__file__)
    for rel in ("app/index.html", "report/template.html", "report/vendor/chart.umd.js", "pjsr/split.js"):
        check(rel, lambda rel=rel: os.path.getsize(os.path.join(base, rel)))
    check("numpy/scipy", lambda: __import__("scipy.ndimage").__version__)
    check("astropy.io.fits", lambda: __import__("astropy.io.fits").__version__)

    def moon():
        from types import SimpleNamespace
        from .analyze import moon_info
        r = moon_info(SimpleNamespace(date_obs="2026-10-12T20:37:00", site_lat=48.86, site_lon=15.02, site_elev=500))
        if r[0] is None:
            raise RuntimeError("keine Mond-Daten berechnet")
        return "%s %% beleuchtet, %s°" % r
    check("Mond (astropy)", moon)
    if sys.platform == "win32":
        check("tkinter", lambda: __import__("tkinter").TkVersion)
        check("Programmfenster (pywebview)", lambda: __import__("webview").__name__)
        check("WebView2-Laufzeit", _webview2_version)
    from .settings import Settings
    check("Einstellungen", lambda: Settings.path())
    print("SELBSTTEST " + ("OK" if ok else "FEHLGESCHLAGEN"))
    return 0 if ok else 1


if __name__ == "__main__":
    run()
