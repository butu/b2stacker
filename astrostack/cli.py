"""Kommandozeile. Alle Befehle können mit --json maschinenlesbar antworten (für KI-Bedienung)."""
from __future__ import annotations

import argparse
import json
import os
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
                "OSC" if g["is_osc"] else "Mono", ", ".join("%s:%d" % (k[5:] if len(k) > 5 else k, v) for k, v in g["nights"].items())))
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


def _follow(args, c, job: dict) -> dict:
    """Auftrag der App bis zum Ende verfolgen; Fortschritt als Text (oder gar nicht bei --json)."""
    if getattr(args, "no_wait", False):
        return job
    last = [None, 0]

    def show(j):
        if args.json:
            return
        p = j.get("progress") or {}
        line = "%s  %s%s%s" % (j.get("state"), p.get("stage") or "",
                              "  %s/%s" % (p.get("done"), p.get("total")) if p.get("total") else "",
                              "  noch ca. %d min" % max(1, round(p["eta_s"] / 60)) if p.get("eta_s") else "")
        if line != last[0]:
            print(line, flush=True)
            last[0] = line
    j = job
    while j.get("state") in ("queued", "running"):
        j = c.wait_job(job["id"], 30, log_tail=40, on_progress=show)
    if not args.json:
        for line in (j.get("log") or [])[-15:]:
            print("  " + line)
        print("Ergebnis: %s %s" % (j.get("state"), j.get("error") or ""))
    return j


def _app(s: Settings | None = None):
    """Laufende App mit demselben Arbeitsverzeichnis (eine Warteschlange, ein PixInsight) oder None."""
    from . import appclient
    url = appclient.find()
    if not url:
        return None
    c = appclient.Client(url, autostart=False, who="cli")
    if s is not None:
        try:
            same = os.path.normcase(os.path.realpath(c.workdir() or "")) == os.path.normcase(os.path.realpath(s.workdir))
        except appclient.AppError:
            same = False
        if not same:
            return None                       # andere Instanz/andere Einstellungen: nicht deren Projekte anfassen
    return c


def cmd_stack(args, s: Settings):
    from .engines import pixinsight as pi
    c = _app(s)
    if c:                                     # App läuft: dort einreihen statt ein zweites PixInsight zu starten
        name = c.project_name(args.project)
        job = c.post("api/project/%s/stack" % c.q(name), {"only": args.only} if args.only else {})
        if not args.json:
            print("In b2stacker eingereiht: %s (Auftrag %s)" % (job.get("title"), job.get("id")))
        j = _follow(args, c, job)
        _out(args, j, lambda d: None)
        sys.exit(0 if j.get("state") in ("done", "queued", "running") else 1)
    pdir = project.pdir_of(s, args.project)
    project.assert_stack_ready(pdir)
    plan = project.apply_edits(pdir)
    plan["workdir"] = pdir
    if args.only:
        _check_runs(args.only, plan)
        for r in plan["runs"]:
            r["enabled"] = r["name"] in args.only
    log = (lambda *a: None) if args.json else print
    st = pi.run_plan(plan, s, log=log, open_results=not args.no_open)
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


def _kv(kv: str, what: str) -> tuple[str, str]:
    if "=" not in kv:
        raise SystemExit("Fehler: „%s“ – erwartet %s (mit =)" % (kv, what))
    k, v = kv.split("=", 1)
    return k.strip(), v.strip()


def _check_runs(names, plan) -> None:
    known = [r["name"] for r in plan.get("runs", [])]
    bad = [n for n in names if n not in known]
    if bad:
        raise SystemExit("Fehler: unbekannter Lauf %s. Vorhanden: %s" % (", ".join("„%s“" % b for b in bad),
                                                                        ", ".join(known) or "keine"))


def cmd_plan(args, s: Settings):
    pdir = project.pdir_of(s, args.project)
    if args.set or args.enable or args.disable or args.reference:
        current = project.apply_edits(pdir)
        _check_runs((args.enable or []) + (args.disable or []) + [_kv(x, "lauf=referenz")[0] for x in args.reference or []],
                    current)
        ed = {"options": {}, "runs": {}}
        for kv in args.set or []:
            k, v = _kv(kv, "option=wert")
            ed["options"][k] = {"true": True, "false": False, "ja": True, "nein": False}.get(v.lower(), v)
        for n in args.enable or []:
            ed["runs"].setdefault(n, {})["enabled"] = True
        for n in args.disable or []:
            ed["runs"].setdefault(n, {})["enabled"] = False
        for kv in args.reference or []:
            n, v = _kv(kv, "lauf=referenz")
            ed["runs"].setdefault(n, {})["reference"] = v
        plan = project.save_edits(pdir, ed)
    else:
        plan = project.apply_edits(pdir)

    def text(p):
        print("%s – %d Frames, Platz %.0f GB nötig / %s GB frei, RAM %.0f GB" % (
            p["target"], p["total_frames"], p["need_gb"], p["free_gb"], p["need_ram_gb"]))
        for r in p["runs"]:
            print("  [%s] %-8s %-8s %4d Lights  Flats %3d  Bias %3d  Ref %s" % (
                "x" if r.get("enabled", True) else " ", r["name"], r["mode"], r["lightCount"], r["n_flats"], r["n_bias"], r.get("reference")))
        print("  Optionen: " + ", ".join("%s=%s" % kv for kv in p["options"].items()))
    lists = ("lights", "flats", "bias", "darks", "darkflats")
    slim = [{k: v for k, v in r.items() if k not in lists} | {"n_" + k: len(r.get(k) or []) for k in lists}
            for r in plan["runs"]]
    _out(args, {k: v for k, v in plan.items() if k != "runs"} | {"runs": slim}, text)


def cmd_override(args, s: Settings):
    pdir = project.pdir_of(s, args.project)
    ov = None
    for p in args.paths:
        ov = project.set_override(pdir, p, None if args.value == "auto" else args.value)
    _out(args, ov, lambda d: print("%d manuelle Entscheidungen. Danach: astrostack report … (Plan neu berechnen)" % len(d)))


def cmd_reanalyze(args, s: Settings):
    c = _app(s)
    if c:
        name = c.project_name(args.project)
        job = c.post("api/project/%s/reanalyze" % c.q(name))
        if not args.json:
            print("In b2stacker eingereiht: %s (Auftrag %s)" % (job.get("title"), job.get("id")))
        j = _follow(args, c, job)
        _out(args, j, lambda d: None)
        sys.exit(0 if j.get("state") in ("done", "queued", "running") else 1)
    pdir = project.pdir_of(s, args.project)
    roots = project.sources(pdir).get("roots") or []
    if not roots:
        sys.exit("Fehler: Für dieses Projekt sind keine Quellordner gespeichert.")
    target = project.meta(pdir)["target"]
    res = project.run_report(roots, target, s, (lambda *a: None) if args.json else print, into=target)
    _out(args, res, lambda d: None)


def cmd_frames(args, s: Settings):
    from .mcp import _frame
    pdir = project.pdir_of(s, args.project)
    rows = [f for f in project.frames(pdir) if args.type == "all" or f.get("type") == args.type]
    if args.status == "problems":
        rows = [f for f in rows if f.get("status") != "ok" or not f.get("selected")]
    elif args.status in ("used", "unused"):
        rows = [f for f in rows if bool(f.get("selected")) == (args.status == "used")]
    elif args.status:
        rows = [f for f in rows if f.get("status") == args.status]
    data = [_frame(f) for f in rows]

    def text(d):
        for f in d:
            print("%s %-8s %-10s %-9s FWHM %-6s Sterne %-5s %s" % (
                "x" if f.get("used") else " ", f.get("status", ""), f.get("night", "")[5:], (f.get("filter") or "")[:9],
                f.get("fwhm_px", "–"), int(f["stars"]) if f.get("stars") is not None else "–",
                f["file"] + ("  ← " + "; ".join(f["reasons"]) if f.get("reasons") else "")
                + ("  ← " + f["error"] if f.get("error") else "")))
        print("%d Frames, %d verwendet" % (len(d), sum(1 for f in d if f.get("used"))))
    _out(args, data, text)


def cmd_selftest(args, s: Settings):
    from . import appclient
    c = _app(s) or appclient.Client(who="cli")  # Selbsttest läuft immer in der App (startet sie bei Bedarf)
    job = c.post("api/selftest", {"folder": args.folder or s.workdir})
    j = _follow(args, c, job)
    _out(args, j, lambda d: None)
    sys.exit(0 if j.get("state") == "done" else 1)


def cmd_mcp(args, s: Settings):
    from . import claude_link
    if args.install or args.uninstall or args.check:
        res = claude_link.install() if args.install else claude_link.uninstall() if args.uninstall else claude_link.status()
        _out(args, res, lambda d: print(d["text"] + ("\n" + d["next"] if d.get("next") else "")
                                        + "".join("\n  " + f for f in d.get("changed") or [])))
        return
    from . import mcp
    mcp.main()


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
        from dataclasses import fields
        types = {f.name: f.type for f in fields(Settings)}
        for kv in args.set:
            k, v = _kv(kv, "schluessel=wert")
            if k not in types:
                sys.exit("Fehler: unbekannte Einstellung „%s“. Vorhanden: %s" % (k, ", ".join(types)))
            t = str(types[k])
            try:
                if "bool" in t:
                    if v.lower() not in ("1", "0", "true", "false", "ja", "nein"):
                        raise ValueError
                    val = v.lower() in ("1", "true", "ja")
                elif "float" in t:
                    val = None if v == "" and "None" in t else float(v.replace(",", "."))
                elif "int" in t:
                    val = int(v)
                elif "list" in t or "dict" in t:
                    val = json.loads(v)
                else:
                    val = v
            except ValueError:
                sys.exit("Fehler: „%s“ ist kein gültiger Wert für %s (%s)" % (v, k, t))
            setattr(s, k, val)
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
    p.add_argument("--no-wait", action="store_true", help="nur einreihen, nicht auf das Ende warten (wenn b2stacker läuft)")
    p = sub.add_parser("reanalyze", help="Projekt neu analysieren (über b2stacker, falls es läuft)")
    p.add_argument("project")
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("frames", help="Frames eines Projekts mit Bewertung und Gründen")
    p.add_argument("project")
    p.add_argument("--type", default="light", choices=["light", "flat", "bias", "dark", "darkflat", "all"])
    p.add_argument("--status", choices=["ok", "warn", "suggest", "rejected", "error", "used", "unused", "problems"])
    p = sub.add_parser("selftest", help="Selbsttest mit echtem PixInsight (läuft in b2stacker)")
    p.add_argument("--folder", help="Ordner für die Testdaten (Standard: Arbeitsverzeichnis)")
    p.add_argument("--no-wait", action="store_true")
    p = sub.add_parser("mcp", help="MCP-Server für KI-Assistenten (stdin/stdout); --install trägt ihn in Claude ein")
    p.add_argument("--install", action="store_true", help="in der Claude-Desktop-App eintragen")
    p.add_argument("--uninstall", action="store_true", help="aus der Claude-Desktop-App austragen")
    p.add_argument("--check", action="store_true", help="nur prüfen, ob eingetragen")
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
    from . import settings as settings_mod
    if args.cmd not in ("settings",):
        from . import logbook
        logbook.setup(s.workdir)
        for msg in settings_mod.LOAD_PROBLEMS:
            logbook.event("settings_problem", msg=msg)
    for msg in settings_mod.LOAD_PROBLEMS:
        print("Hinweis: " + msg, file=sys.stderr)
    if args.cmd == "app":
        from .app import window
        return window.run()
    from .appclient import AppError
    try:
        {"scan": cmd_scan, "check-calib": cmd_check_calib, "report": cmd_report, "settings": cmd_settings,
         "gui": cmd_gui, "plan": cmd_plan, "override": cmd_override, "stack": cmd_stack, "status": cmd_status,
         "logs": cmd_logs, "reanalyze": cmd_reanalyze, "frames": cmd_frames, "selftest": cmd_selftest,
         "mcp": cmd_mcp}[args.cmd](args, s)
    except (AppError, FileNotFoundError, ValueError) as e:      # bekannte Fehler: Klartext statt Traceback
        sys.exit("Fehler: %s" % e)


if __name__ == "__main__":
    main()
