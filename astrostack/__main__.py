"""Einstiegspunkt für `python -m astrostack` und die fertige AstroStack.exe.

Ohne Argumente (Doppelklick) startet die Oberfläche.
"""
import multiprocessing
import sys


def run():
    multiprocessing.freeze_support()          # nötig für die Vermessung in Unterprozessen (exe)
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
    from .cli import main
    main()


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
    from .settings import Settings
    check("Einstellungen", lambda: Settings.path())
    print("SELBSTTEST " + ("OK" if ok else "FEHLGESCHLAGEN"))
    return 0 if ok else 1


if __name__ == "__main__":
    run()
