"""Kommandozeile für den Neubau: ``python -m astrostack neu <befehl>``.

Spricht mit dem laufenden neuen Server (gleiches Arbeitsverzeichnis, eine Warteschlange, ein PixInsight) und
nimmt sonst ``Core`` direkt (dann laufen Aufträge in diesem Prozess, und der Befehl wartet auf ihr Ende).
Die alten Befehle (``stack``, ``plan`` …) bleiben unverändert für die alte App.

    neu projects                    alle Projekte
    neu project NAME                Überblick (Plan, Stacks)
    neu refresh NAME [--no-wait]    jetzt auf neue Dateien prüfen
    neu frames NAME [--type T] [--status use|skip] [--limit N]
    neu stack NAME [--runs ID …] [--no-wait]
    neu stacks NAME                 Stacks mit Qualitätswerten
    neu history [NAME] [--limit N]  Verlauf in Klartext

``B2_WEB_URL`` setzt die Server-Adresse fest (Tests: tote Adresse → immer direkt).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import server as web


class Remote:
    """Laufender neuer Server über HTTP."""

    def __init__(self, url: str):
        self.url = url.rstrip("/") + "/"

    def get(self, path: str, **q):
        qs = ("?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})) if q else ""
        return self._req(urllib.request.Request(self.url + path + qs))

    def post(self, path: str, body: dict | None = None):
        return self._req(urllib.request.Request(self.url + path, data=json.dumps(body or {}).encode("utf-8"),
                                                method="POST", headers={"Content-Type": "application/json",
                                                                        "X-Astrostack": "1"}))

    @staticmethod
    def _req(req):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except Exception:  # noqa: BLE001
                msg = None
            raise ValueError(msg or "HTTP %d" % e.code) from None

    def p(self, name) -> str:
        return "api/projects/" + urllib.parse.quote(str(name), safe="")

    def projects(self):
        return self.get("api/projects")

    def project(self, name):
        return self.get(self.p(name))

    def refresh(self, name):
        return self.post(self.p(name) + "/refresh")

    def frames(self, name, type_, status, limit):
        return self.get(self.p(name) + "/frames", type=type_, status=status, limit=limit)

    def stack(self, name, runs):
        return self.post(self.p(name) + "/stack", {"run_ids": runs} if runs else {})

    def stacks(self, name):
        return self.get(self.p(name) + "/stacks")

    def history(self, name, limit):
        return self.get("api/history", project=name, limit=limit)

    def task(self, tid):
        snap = self.get("api/tasks")
        for t in snap["running"] + snap["queued"] + snap["finished"]:
            if t["id"] == tid:
                return t
        return None

    def close(self):
        pass


class Local:
    """``Core`` direkt (kein Server läuft)."""

    def __init__(self, s):
        from ..core import Core
        self.core = Core(s.workdir, pixinsight=s.pixinsight, workers=s.analysis_workers, backup_dir=s.backup_dir)

    def projects(self):
        return self.core.projects()

    def project(self, name):
        return self.core.project(name)

    def refresh(self, name):
        return self.core.refresh(name, reason="Kommandozeile").as_dict()

    def frames(self, name, type_, status, limit):
        return self.core.frames(name, type_=type_, status=status, limit=limit)

    def stack(self, name, runs):
        return self.core.stack(name, runs or None, origin="cli")

    def stacks(self, name):
        return self.core.stacks(name, measure=False)

    def history(self, name, limit):
        return self.core.timeline(name, limit=limit)

    def task(self, tid):
        t = self.core.tasks.get(tid)
        return t.as_dict() if t else None

    def close(self):
        self.core.tasks.wait_idle(5)
        self.core.close()


def find_server(s) -> str | None:
    """Adresse des laufenden neuen Servers mit demselben Arbeitsverzeichnis (oder None)."""
    fixed = os.environ.get("B2_WEB_URL", "").strip()
    cands = [fixed] if fixed else ["http://127.0.0.1:%d/" % p for p in range(web.PORT, web.PORT + 5)]
    for url in cands:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/bootstrap", timeout=1.5) as r:
                b = json.loads(r.read())
        except Exception:  # noqa: BLE001
            continue
        same = os.path.normcase(os.path.realpath(b.get("workdir") or "")) == \
            os.path.normcase(os.path.realpath(s.workdir))
        if b.get("version") and same:
            return url
    return None


def connect(s):
    url = find_server(s)
    return Remote(url) if url else Local(s)


def _wait(api, task: dict, quiet: bool) -> dict:
    t, last = task, None
    while t and t.get("state") in ("queued", "running"):
        line = "%s  %s%s" % (t.get("state"), t.get("step") or t.get("title"),
                             "  %d/%d" % (t["done"], t["total"]) if t.get("total") else "")
        if not quiet and line != last:
            print(line, flush=True)
            last = line
        time.sleep(0.5)
        t = api.task(task["id"]) or t
    return t


def _h(x) -> str:
    from ..history import hours
    return hours(x)


def run(args, s) -> None:
    api = connect(s)
    out = None
    try:
        c = args.neu_cmd
        if c == "projects":
            out = api.projects()
            text = lambda d: [print("%-4s %-28s %8s  %5d Lights  %s" % (  # noqa: E731
                p["id"], p["name"][:28], _h(p["hours"]), p["lights"],
                ", ".join("%s %s" % (f["filter"], _h(f["hours"])) for f in p["filters"]))) for p in d]
        elif c == "project":
            out = api.project(args.project)
            plan = out.get("plan") or {}

            def text(d):
                print("%s (ID %s)%s" % (d["name"], d["id"], " – " + d["description"] if d.get("description") else ""))
                for f in d.get("folders") or []:
                    print("  Ordner: %s%s" % (f["path"], "" if f["online"] else "  (offline)"))
                for r in plan.get("runs") or []:
                    print("  Lauf %-10s %s  %4d Lights  %s%s" % (r["name"], "an " if r["enabled"] else "aus",
                                                              r["lights"], _h(r["hours"]),
                                                              "" if r["enabled"] else "  – " + (r.get("why_off") or "")))
                print("  Bereit zum Stacken" if plan.get("ready") else "  Noch nicht bereit")
                for st in (d.get("stacks") or [])[:5]:
                    print("  Stack %s  %s  %s" % (st["id"], st["name"], st["status"]))
        elif c == "refresh":
            t = api.refresh(args.project)
            out = t if args.no_wait else _wait(api, t, args.json)
            text = lambda d: print("Ergebnis: %s %s" % (d.get("state"), d.get("error") or ""))  # noqa: E731
        elif c == "frames":
            out = api.frames(args.project, args.type, args.status, args.limit)
            text = lambda d: [print("%-6s %-6s %-30s %-8s FWHM %-5s %s" % (  # noqa: E731
                f["id"], f["status"], f["name"][:30], f.get("filter") or "", f.get("fwhm") or "–", f.get("reason") or ""))
                for f in d["frames"]] + [print("%d von %d" % (len(d["frames"]), d["total"]))]
        elif c == "stack":
            t = api.stack(args.project, args.runs)
            out = t if args.no_wait else _wait(api, t, args.json)
            text = lambda d: print("Ergebnis: %s %s" % (d.get("state"), d.get("error") or ""))  # noqa: E731
        elif c == "stacks":
            out = api.stacks(args.project)

            def text(d):
                for st in d:
                    q = st.get("quality") or {}
                    print("%-4s %-28s %-9s %5s Lights %8s  FWHM %s  Sterne %s  %s" % (
                        st["id"], st["name"][:28], st["status"], st.get("frames") or 0, _h(st.get("hours")),
                        q.get("fwhm", "–"), q.get("stars", "–"), st.get("note") or ""))
        elif c == "history":
            out = api.history(args.project, args.limit)
            text = lambda d: [print("%s  %-20s %s%s" % (i["at"][:16].replace("T", " "), (i.get("project") or "")[:20],  # noqa: E731
                                                       i["title"], "  (" + i["detail"] + ")" if i.get("detail") else ""))
                              for i in d]
        else:
            raise ValueError("unbekannter Befehl")
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        else:
            text(out)
    finally:
        api.close()
    if isinstance(out, dict) and out.get("state") == "failed":
        sys.exit(1)


def add_parser(sub) -> None:
    p = sub.add_parser("neu", help="Neubau: Projekte, Prüfen, Frames, Stacken, Verlauf (über den neuen Server)")
    ns = p.add_subparsers(dest="neu_cmd", required=True)
    ns.add_parser("projects", help="alle Projekte")
    x = ns.add_parser("project", help="Überblick über ein Projekt")
    x.add_argument("project")
    x = ns.add_parser("refresh", help="jetzt auf neue oder geänderte Dateien prüfen")
    x.add_argument("project")
    x.add_argument("--no-wait", action="store_true")
    x = ns.add_parser("frames", help="Frames mit Status, Grund und Messwerten")
    x.add_argument("project")
    x.add_argument("--type", default="light", choices=["light", "flat", "bias", "dark", "darkflat", "all"])
    x.add_argument("--status", choices=["use", "skip", "delete"])
    x.add_argument("--limit", type=int, default=200)
    x = ns.add_parser("stack", help="mit PixInsight stacken")
    x.add_argument("project")
    x.add_argument("--runs", nargs="*", help="nur diese Lauf-IDs")
    x.add_argument("--no-wait", action="store_true")
    x = ns.add_parser("stacks", help="Stacks mit Qualitätswerten")
    x.add_argument("project")
    x = ns.add_parser("history", help="Verlauf in Klartext")
    x.add_argument("project", nargs="?")
    x.add_argument("--limit", type=int, default=30)
