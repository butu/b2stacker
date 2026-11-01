"""Kommandozeile. Alle Befehle können mit --json maschinenlesbar antworten (für KI-Bedienung)."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, calibcheck, project
from .settings import Settings


def _out(args, data, text_fn):
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        text_fn(data)


def cmd_scan(args, s: Settings):
    inv = project.scan(args.folders, s)
    data = inv.summary()

    def text(d):
        print("LIGHTS")
        for g in d["lights"]:
            print("  %-20s %-12s %-18s %4d Frames (%d ausgeschl.)  %6.2f h  %s  %s" % (
                g["target"][:20], (g["filter"] or "–")[:12], g["filter_kind"], g["frames"], g["excluded"], g["hours"],
                "OSC" if g["is_osc"] else "Mono", ", ".join("%s:%d" % (k[5:], v) for k, v in g["nights"].items())))
        print("KALIBRIERUNG")
        for c in d["calib"]:
            print("  %-8s %-10s %4d Frames  %-8s %-22s %s" % (c["type"], (c["filter"] or "")[:10], c["frames"],
                                                          c["exposure"], (c["readout"] or "")[:22], c["folder"]))
            for w in c["warnings"]:
                print("           ! " + w)
        if d["unknown"]:
            print("UNBEKANNT: %d Dateien" % len(d["unknown"]))
        for p, e in d["errors"]:
            print("FEHLER " + p + ": " + e)
    _out(args, data, text)


def cmd_check_calib(args, s: Settings):
    inv = project.scan(args.folders, s)
    res = calibcheck.public(calibcheck.check_all(inv))

    def text(d):
        for k, v in d["flat_checks"].items():
            print("[%s] %s" % (v["verdict"].upper(), k))
            for f in v["findings"]:
                print("     %s: %s" % (f["title"], f["text"]))
        for m in d["matches"]:
            g = m["group"]
            print("\n%s / %s:" % (g["target"], g["filter"]))
            best = next((c for c in m["flats"] if c["usable"]), None)
            print("  Flats: " + (best["set"]["folder"] + ("  (" + "; ".join(best["reasons"]) + ")" if best["reasons"] else "") if best else "KEINE"))
            for f in m["findings"]:
                print("  [%s] %s – %s" % (f["level"], f["title"], f["text"]))
    _out(args, res, text)


def cmd_report(args, s: Settings):
    log = (lambda *a: None) if args.json else print
    res = project.run_report(args.folders, args.target, s, log)
    _out(args, res, lambda d: None)


def cmd_settings(args, s: Settings):
    if args.set:
        for kv in args.set:
            k, v = kv.split("=", 1)
            if not hasattr(s, k):
                sys.exit("unbekannte Einstellung: " + k)
            cur = getattr(s, k)
            setattr(s, k, type(cur)(v) if isinstance(cur, (int, float, bool)) and not isinstance(cur, bool) else
                    (v.lower() in ("1", "true", "ja")) if isinstance(cur, bool) else v)
        print("gespeichert: " + s.save())
    from dataclasses import asdict
    _out(args, asdict(s) | {"settings_file": Settings.path()}, lambda d: [print("%-22s %s" % (k, v)) for k, v in d.items()])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="astrostack", description="Rohdaten sichten, bewerten und mit PixInsight stacken.")
    ap.add_argument("--version", action="version", version="astrostack " + __version__)
    ap.add_argument("--json", action="store_true", help="maschinenlesbare Ausgabe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="Ordner scannen und Frames gruppieren")
    p.add_argument("folders", nargs="+")
    p = sub.add_parser("check-calib", help="Flats/Bias/Darks prüfen und zuordnen")
    p.add_argument("folders", nargs="+")
    p = sub.add_parser("report", help="vermessen und Bericht + Plan erzeugen")
    p.add_argument("folders", nargs="+")
    p.add_argument("--target", help="nur dieses Ziel (sonst alle)")
    p = sub.add_parser("settings", help="Einstellungen anzeigen/ändern")
    p.add_argument("--set", nargs="*", help="schluessel=wert")
    args = ap.parse_args(argv)
    s = Settings.load()
    {"scan": cmd_scan, "check-calib": cmd_check_calib, "report": cmd_report, "settings": cmd_settings}[args.cmd](args, s)


if __name__ == "__main__":
    main()
