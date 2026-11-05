"""Selbsttest der neuen App: kleiner synthetischer Datensatz → automatischer Ablauf → Stacken mit dem echten
PixInsight über die Übergabe (Weg B).

Prüft auf dem echten Rechner, was Linux-Tests nicht sehen: PixInsight- und WBPP-Version, Komma im Ordnernamen,
Netzlaufwerk, Flats je Nacht, Wiederverwendung der Master (zweiter Durchlauf). Testdaten werden nur neu angelegt
(``b2stacker-selbsttest, mit Komma``); das Testprojekt liegt in einem eigenen Arbeitsverzeichnis
(``<arbeitsverzeichnis>/selbsttest/neu``) und erscheint nicht in der Projektliste.
"""
from __future__ import annotations

import datetime
import json
import os
import time

from . import flow, handover, library, stacking, system
from .flow import plan as fplan
from .selftest import make_dataset
from .workspace import Workspace


def environment(pixinsight: str, workdir: str, folder: str) -> dict:
    from .engines.pixinsight import is_remote_path
    pi_ok = bool(pixinsight) and os.path.isfile(pixinsight)
    main = handover.wbpp_main(pixinsight) if pixinsight else ""
    version = None
    if pi_ok:
        pi_dir = os.path.dirname(os.path.dirname(pixinsight))
        for cand in (os.path.join(pi_dir, "doc", "PixInsight.txt"), os.path.join(pi_dir, "bin", "version.txt")):
            try:
                with open(cand, encoding="utf-8", errors="replace") as fh:
                    version = fh.readline().strip()[:80]
                break
            except OSError:
                continue
    try:
        free = system.free_disk_gb(workdir)
    except Exception:  # noqa: BLE001
        free = None
    return dict(pixinsight=pixinsight, pixinsight_ok=pi_ok, wbpp_ok=bool(main) and os.path.isfile(main),
                wbpp_main=main, pixinsight_version=version, test_folder=folder,
                test_folder_remote=bool(is_remote_path(folder)), workdir=workdir, workdir_free_gb=free)


def run(pixinsight: str, workdir: str, folder: str, progress=None, cancel=None, runner=handover.run,
        with_pixinsight: bool = True) -> dict:
    """Selbsttest ausführen. → Bericht (auch als JSON in ``<workdir>/selbsttest/bericht-neu-*.json``)"""
    t0 = time.time()
    res = dict(started=datetime.datetime.now().isoformat(timespec="seconds"), steps=[], kind="neu")

    def step(name, ok, detail="", optional=False):
        res["steps"].append(dict(name=name, ok=bool(ok), detail=detail, optional=optional))

    def prog(text, i):
        if progress:
            progress(step=text, done=i, total=5)

    env = environment(pixinsight, workdir, folder)
    res["environment"] = env
    step("PixInsight gefunden", env["pixinsight_ok"], env["pixinsight"] or "nicht eingestellt")
    step("WBPP gefunden", env["wbpp_ok"], env["wbpp_main"])
    step("Testordner auf Netzlaufwerk", True, "ja" if env["test_folder_remote"] else "nein (lokal)", optional=True)
    prog("Testdaten anlegen", 0)
    root = make_dataset(folder, log=lambda *_: None)
    step("Testdaten", os.path.isdir(root), root)
    ws = Workspace(os.path.join(workdir, "selbsttest", "neu")).ensure()
    db = ws.open_db()
    try:
        prog("Testdaten einlesen und vermessen", 1)
        pid = db.value("SELECT id FROM projects WHERE name = ?", ("Selbsttest",))
        if pid is None:
            pid = library.create_project(db, ws, "Selbsttest", folders=[root])
        cancel_fn = (lambda: cancel.is_set()) if cancel is not None else None
        flow.run_project(db, ws, pid, workers=2, cancel=cancel_fn)
        plan = fplan.update_plan(db, pid, ws.root)
        runs = [r for r in plan["runs"] if r["enabled"]]
        lights = sum(r["lights"] for r in runs)
        flats = sum(len(r.get("flats") or []) for r in runs)
        step("Ablauf (Entdecken bis Plan)", bool(runs) and lights >= 8,
             "%d Läufe, %d Lights, %d Flat-Sätze" % (len(runs), lights, flats))
        if with_pixinsight and env["pixinsight_ok"] and env["wbpp_ok"]:
            for i, label in ((2, "erster Durchlauf"), (3, "zweiter Durchlauf (Master wiederverwenden)")):
                if cancel is not None and cancel.is_set():
                    break
                prog("Stacken mit PixInsight – " + label, i)
                out = stacking.stack_project(db, ws, pid, pixinsight, progress=None, cancel=cancel, runner=runner,
                                             origin="selftest")
                ok = bool(out) and all(r["status"] == "done" for r in out)
                det = "; ".join("%s: %s" % (r["run"], r["error"] or ", ".join(os.path.basename(f) for f in r["files"]))
                                for r in out)
                step("Stacken – " + label, ok, det)
                res.setdefault("stacks", []).append(out)
                if i == 3 and ok:
                    reused = [m for r in out for m in r.get("masters") or [] if m["reused"]]
                    step("Master wiederverwendet", bool(reused), "%d Master" % len(reused))
                if not ok:
                    break
        else:
            step("Stacken mit PixInsight", False, "übersprungen (PixInsight/WBPP nicht gefunden)")
    finally:
        db.close()
    prog("Bericht schreiben", 4)
    res["ok"] = all(s["ok"] for s in res["steps"] if not s["optional"])
    res["duration_s"] = round(time.time() - t0, 1)
    os.makedirs(os.path.join(workdir, "selbsttest"), exist_ok=True)
    out = os.path.join(workdir, "selbsttest", "bericht-neu-%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2, ensure_ascii=False, default=str)
    res["report"] = out
    return res
