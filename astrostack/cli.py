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


def cmd_gui(args, s: Settings):
    from .app import server
    server.main(args.port, not args.no_browser)


def cmd_stack(args, s: Settings):
    from . import engines
    from .engines.pixinsight import StackError
    pdir = project.pdir_of(s, args.project)
    project.assert_stack_ready(pdir)
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir
    if args.only:
        for r in plan["runs"]:
            r["enabled"] = r["name"] in args.only
    log = (lambda *a: None) if args.json else print
    try:
        st = engines.run_plan(plan, s, log=log, open_results=not args.no_open)
    except StackError as e:
        print("FEHLER: %s" % e, file=sys.stderr)
        sys.exit(1)
    _out(args, st.as_dict(), lambda d: print("Status: %s %s" % (d["state"], d["error"])))
    sys.exit(0 if st.state == "done" else 1)


def cmd_status(args, s: Settings):
    import glob
    import os
    files = sorted(glob.glob(os.path.join(s.workdir, "jobs", "*.json")), key=os.path.getmtime)
    jobs = []
    for f in files[-args.last:]:
        try:
            d = json.load(open(f, encoding="utf-8"))
            d["log"] = d.get("log", [])[-args.tail:]
            jobs.append(d)
        except (OSError, ValueError):
            pass
    runs = sorted(glob.glob(os.path.join(s.workdir, "projects", "*", "runs", "*", "status.json")), key=os.path.getmtime)
    data = dict(jobs=jobs, stack_runs=[dict(json.load(open(r, encoding="utf-8")), file=r) for r in runs[-args.last:]],
                projects=project.list_projects(s))

    def text(d):
        for j in d["jobs"]:
            p = j.get("progress") or {}
            print("%s  %-10s %-30s %s %s" % (j["created"], j["state"], j["title"][:30], p.get("stage", ""),
                                            "%s/%s" % (p.get("done"), p.get("total")) if p.get("total") else ""))
        for r in d["stack_runs"]:
            print("Stacking %s: %s %s %s/%s %s" % (r["started"], r["state"], r["run"], r["done"], r["total"], r["error"]))
    _out(args, data, text)


def cmd_plan(args, s: Settings):
    pdir = project.pdir_of(s, args.project)
    if args.set or args.enable or args.disable or args.reference:
        ed = {"options": {}, "runs": {}}
        for kv in args.set or []:
            k, v = kv.split("=", 1)
            ed["options"][k] = {"true": True, "false": False, "ja": True, "nein": False}.get(v.lower(), v)
        for n in args.enable or []:
            ed["runs"].setdefault(n, {})["enabled"] = True
        for n in args.disable or []:
            ed["runs"].setdefault(n, {})["enabled"] = False
        for kv in args.reference or []:
            n, v = kv.split("=", 1)
            ed["runs"].setdefault(n, {})["reference"] = v
        plan = project.save_edits(pdir, ed)
    else:
        plan = project.apply_edits(pdir)

    def text(p):
        print("%s – %d Frames, Platz %.0f GB nötig / %s GB frei, RAM %.0f GB" % (
            p["target"], p["total_frames"], p["need_gb"], p["free_gb"], p["need_ram_gb"]))
        for r in p["runs"]:
            print("  [%s] %-8s %-8s %4d Lights  Flats %3d  Bias %3d  Ref %s" % (
                "x" if r.get("enabled", True) else " ", r["name"], r["mode"], r["lightCount"], len(r["flats"]), len(r["bias"]), r["reference"]))
        print("  Optionen: " + ", ".join("%s=%s" % kv for kv in p["options"].items()))
    _out(args, {k: v for k, v in plan.items() if k != "runs"} | {"runs": [{k: v for k, v in r.items() if k not in ("lights", "flats", "bias", "darks", "darkflats")} for r in plan["runs"]]}, text)


def cmd_override(args, s: Settings):
    pdir = project.pdir_of(s, args.project)
    ov = None
    for p in args.paths:
        ov = project.set_override(pdir, p, None if args.value == "auto" else args.value)
    _out(args, ov, lambda d: print("%d manuelle Entscheidungen. Danach: astrostack report … (Plan neu berechnen)" % len(d)))


def cmd_logs(args, s: Settings):
    from . import logbook
    ev = logbook.read_events(s.workdir, args.last, args.kind, args.project)

    def text(d):
        for e in d:
            extra = {k: v for k, v in e.items() if k not in ("ts", "v", "kind", "trace")}
            print("%s  %-15s %s" % (e["ts"], e["kind"], json.dumps(extra, ensure_ascii=False, default=str)[:300]))
            if e.get("trace") and args.trace:
                print(e["trace"])
    _out(args, ev, text)


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
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("scan", help="Ordner scannen und Frames gruppieren")
    p.add_argument("folders", nargs="+")
    p = sub.add_parser("check-calib", help="Flats/Bias/Darks prüfen und zuordnen")
    p.add_argument("folders", nargs="+")
    p = sub.add_parser("report", help="vermessen und Bericht + Plan erzeugen")
    p.add_argument("folders", nargs="+")
    p.add_argument("--target", help="nur dieses Ziel (sonst alle)")
    p = sub.add_parser("app", help="Programmfenster öffnen (Standard bei Doppelklick)")
    p = sub.add_parser("gui", help="Oberfläche im Browser öffnen (--no-browser: nur Server, z. B. für Tests)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("plan", help="Stacking-Plan eines Projekts anzeigen/ändern")
    p.add_argument("project")
    p.add_argument("--set", nargs="*", help="option=wert, z. B. local_normalization=false")
    p.add_argument("--enable", nargs="*", help="Lauf-Namen aktivieren")
    p.add_argument("--disable", nargs="*", help="Lauf-Namen deaktivieren")
    p.add_argument("--reference", nargs="*", help="lauf=auto | lauf=master:RGB | lauf=file:<pfad>")
    p = sub.add_parser("override", help="Frames manuell aufnehmen/ausschließen")
    p.add_argument("project")
    p.add_argument("value", choices=["in", "out", "auto"])
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("stack", help="Plan eines Projekts mit PixInsight stacken")
    p.add_argument("project")
    p.add_argument("--only", nargs="*", help="nur diese Läufe")
    p.add_argument("--no-open", action="store_true", help="Ergebnisse nicht in PixInsight öffnen")
    p = sub.add_parser("status", help="letzte Aufträge und Stacking-Läufe")
    p.add_argument("--last", type=int, default=5)
    p.add_argument("--tail", type=int, default=20)
    p = sub.add_parser("logs", help="Protokoll: letzte Ereignisse (für Fehlersuche)")
    p.add_argument("--last", type=int, default=40)
    p.add_argument("--kind", help="z. B. error, job_end, stack_step, analysis")
    p.add_argument("--project")
    p.add_argument("--trace", action="store_true", help="Tracebacks mit ausgeben")
    p = sub.add_parser("settings", help="Einstellungen anzeigen/ändern")
    p.add_argument("--set", nargs="*", help="schluessel=wert")
    args = ap.parse_args(argv)
    if not args.cmd:                       # ohne Befehl → Programmfenster
        args = ap.parse_args(["app"])
    s = Settings.load()
    if args.cmd not in ("settings",):
        from . import logbook
        logbook.setup(s.workdir)
    if args.cmd == "app":
        from .app import window
        return window.run()
    {"scan": cmd_scan, "check-calib": cmd_check_calib, "report": cmd_report, "settings": cmd_settings, "gui": cmd_gui,
     "plan": cmd_plan, "override": cmd_override, "stack": cmd_stack, "status": cmd_status, "logs": cmd_logs}[args.cmd](args, s)


if __name__ == "__main__":
    main()
