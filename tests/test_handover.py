"""Übergabe an WBPP (Weg B): Auftrag, Skripte, Start, Fortschritt, Ergebnis – mit nachgebautem PixInsight."""
import json
import os
import subprocess
import sys
import threading

import pytest

from astrostack import flow, handover, library
from astrostack.flow import plan as fp

from .test_flow import _calib_tree, env  # noqa: F401 – Fixture

FAKE = r'''
import json, os, sys, time
r = [a for a in sys.argv if a.startswith("-r=")][0][3:]
script, arg = r.split(",", 1)
assert arg.startswith("job=")
first = open(script, encoding="utf-8").readline().strip()
if first != "#engine v8":
    sys.exit(0)
code = open(script, encoding="utf-8").read()
inc = [l for l in code.splitlines() if l.startswith("#include")][0]
job = json.load(open(arg[4:], encoding="utf-8"))
out = dict(ok=False, phase="files", added=len(job["files"]), notAdded=[], wbpp="3.2.0", include=inc)
sess = {}
for f in job["files"]:
    sess.setdefault(f["type"], set()).add(f.get("session"))
out["sessions"] = {k: sorted(x or "" for x in v) for k, v in sess.items()}
out["params"] = job["params"]
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "crash":
    sys.exit(1)
st = job["status"]
for i in range(3):
    json.dump(dict(total=3, done=i, name=["Kalibrieren", "Registrieren", "Integrieren"][i]), open(st, "w"))
    time.sleep(float(os.environ.get("FAKE_STEP_S", "0.05")))
os.makedirs(os.path.join(job["outputDirectory"], "master"), exist_ok=True)
open(os.path.join(job["outputDirectory"], "master", "masterLight_HaO3.xisf"), "w").write("x")
json.dump(dict(total=3, done=3, name="fertig"), open(st, "w"))
out.update(ok=True, phase="done")
json.dump(out, open(job["result"], "w"))
'''


@pytest.fixture
def stacked(env, tmp_path):  # noqa: F811
    ws, db, data = env
    _calib_tree(data)
    pid = library.create_project(db, ws, "vdB 131", folders=[data])
    flow.run_project(db, ws, pid, workers=1)
    plan = fp.update_plan(db, pid, ws.root)
    fake = tmp_path / "fakepi.py"
    fake.write_text(FAKE, encoding="utf-8")

    def popen(cmd, **kw):
        kw.pop("creationflags", None)
        return subprocess.Popen([sys.executable, str(fake)] + cmd[1:], **kw)
    return ws, db, plan, popen, tmp_path


def _ha(plan):
    return next(r for r in plan["runs"] if r["filter"] == "HaO3")


def test_build_job_sessions_params_and_paths(stacked):
    ws, db, plan, _, _ = stacked
    run = _ha(plan)
    out = os.path.join(ws.temp, "run1")
    job = handover.build_job(db, run, out)
    types = {}
    for f in job["files"]:
        types.setdefault(f["type"], []).append(f)
        assert "\\" not in f["path"] and os.path.exists(f["path"])
    assert len(types["light"]) == 3 and len(types["flat"]) == 5 and len(types["bias"]) == 4
    assert {f["session"] for f in types["light"]} == {f["session"] for f in types["flat"]} == {"S1"}
    assert "dark" not in types
    assert job["params"]["autoIntegrationMode"] is False and job["params"]["groupingKeywordsEnabled"] is True
    assert job["indexed"]["rejection"] == [None, None, None, 0]           # 3 Lights → Percentile
    assert job["sessionKeyword"] == handover.SESSION_KEYWORD
    assert any("Rohdaten, NAS" in f["path"] for f in job["files"]), "Komma im Pfad bleibt erhalten"
    assert "drizzle" not in job
    drz = dict(run, settings=[dict(s, value=True) if s["key"] == "drizzle" else s for s in run["settings"]])
    assert handover.build_job(db, drz, out)["drizzle"]["scale"] == 2


def test_scripts_and_command(stacked, tmp_path):
    ws, db, plan, _, _ = stacked
    job = handover.build_job(db, _ha(plan), os.path.join(ws.temp, "run1"))
    exe = str(tmp_path / "PixInsight" / "bin" / "PixInsight.exe")
    launcher, job_path = handover.write_scripts(job, ws.temp, exe)
    code = open(launcher, encoding="utf-8").read()
    assert code.startswith("#engine v8\n")
    assert '#include "%s"' % handover.pi_path(str(tmp_path / "PixInsight" / "src" / "scripts" / "BatchPreprocessing"
                                                           / "BPP-main.js")) in code
    ev = open(os.path.join(ws.temp, "b2events.js"), encoding="utf-8").read()
    assert not ev.startswith("#engine") and job["status"] in ev
    assert json.load(open(job_path, encoding="utf-8"))["eventScript"].endswith("b2events.js")
    cmd = handover.command(exe, launcher, job_path)
    assert cmd[1:3] == ["-n", "--automation-mode"] and cmd[-1] == "--force-exit"
    assert cmd[3].count(",") == 1                                     # nur Skript und Auftrag
    with pytest.raises(handover.HandoverError, match="Komma"):
        handover.command(exe, os.path.join(str(tmp_path), "a,b", "x.js"), job_path)


def test_run_reports_progress_and_masters(stacked, tmp_path, monkeypatch):
    ws, db, plan, popen, _ = stacked
    job = handover.build_job(db, _ha(plan), os.path.join(ws.temp, "run1"))
    launcher, job_path = handover.write_scripts(job, ws.temp, str(tmp_path / "pi" / "bin" / "PixInsight.exe"))
    seen = []
    monkeypatch.setenv("FAKE_STEP_S", "0.3")
    res = handover.run("PixInsight.exe", launcher, job_path, progress=lambda **kw: seen.append(kw), popen=popen,
                       poll_s=0.05)
    assert res["ok"] and res["exit_code"] == 0 and res["phase"] == "done"
    assert res["masters"] and res["masters"][0].endswith("masterLight_HaO3.xisf")
    assert res["sessions"]["light"] == ["S1"] and res["include"].endswith('BPP-main.js"')
    assert any(s["message"] == "Registrieren" for s in seen) and seen[0]["step"] == "Stacken mit PixInsight"


def test_run_explains_silent_exit(stacked, tmp_path, monkeypatch):
    ws, db, plan, popen, _ = stacked
    job = handover.build_job(db, _ha(plan), os.path.join(ws.temp, "run1"))
    launcher, job_path = handover.write_scripts(job, ws.temp, str(tmp_path / "pi" / "bin" / "PixInsight.exe"))
    with open(launcher, encoding="utf-8") as fh:
        body = fh.read().split("\n", 1)[1]
    with open(launcher, "w", encoding="utf-8") as fh:                  # wie ein Skript ohne #engine v8
        fh.write(body)
    res = handover.run("PixInsight.exe", launcher, job_path, popen=popen, poll_s=0.05)
    assert not res.get("ok") and "nicht ausgeführt" in res["error"]
    monkeypatch.setenv("FAKE_MODE", "crash")
    launcher, job_path = handover.write_scripts(job, ws.temp, str(tmp_path / "pi" / "bin" / "PixInsight.exe"))
    res = handover.run("PixInsight.exe", launcher, job_path, popen=popen, poll_s=0.05)
    assert res["exit_code"] == 1 and res["error"]


def test_run_cancel(stacked, tmp_path, monkeypatch):
    ws, db, plan, popen, _ = stacked
    job = handover.build_job(db, _ha(plan), os.path.join(ws.temp, "run1"))
    launcher, job_path = handover.write_scripts(job, ws.temp, str(tmp_path / "pi" / "bin" / "PixInsight.exe"))
    monkeypatch.setenv("FAKE_STEP_S", "5")
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    res = handover.run("PixInsight.exe", launcher, job_path, cancel=ev, popen=popen, poll_s=0.05)
    assert res["cancelled"] and not res.get("ok")


def test_job_without_lights_refused(stacked):
    ws, db, plan, _, _ = stacked
    with pytest.raises(handover.HandoverError):
        handover.build_job(db, dict(_ha(plan), light_ids=[]), ws.temp)


def test_stack_project_end_to_end(stacked, monkeypatch):
    from astrostack import stacking
    ws, db, plan, popen, tmp = stacked
    pid = db.value("SELECT id FROM projects")
    exe = str(tmp / "pi" / "bin" / "PixInsight.exe")
    runner = lambda *a, **kw: handover.run(*a, popen=popen, poll_s=0.05, **kw)          # noqa: E731
    open(os.path.join(ws.temp, "rest-vom-letzten-mal.xisf"), "w").close()
    res = stacking.stack_project(db, ws, pid, exe, runner=runner)
    assert [r["status"] for r in res] == ["done"] and res[0]["run"] == "HaO3"   # RGB ist aus (keine Flats)
    sdir = os.path.join(ws.project_dir("vdB 131"), res[0]["folder"])
    assert res[0]["folder"].endswith("HaO3 0h30") and os.listdir(sdir) == ["masterLight_HaO3.xisf"]
    assert os.listdir(ws.temp) == []                                          # temp vor und nach dem Lauf geleert
    st = db.one("SELECT * FROM stacks")
    assert st["status"] == "done" and st["frames"] == 3 and st["wbpp_version"] == "3.2.0" and st["size_bytes"] == 1
    assert db.value("SELECT COUNT(*) FROM stack_files WHERE stack_id = ?", (st["id"],)) == 3
    assert db.value("SELECT stacked_at FROM projects") is not None
    # zweiter Stack bekommt einen eigenen Ordner, der erste bleibt
    res2 = stacking.stack_project(db, ws, pid, exe, runner=runner)
    assert res2[0]["folder"] != res[0]["folder"] and len(os.listdir(ws.project_dir("vdB 131"))) == 2
    # Löschen entfernt nur eigene Dateien
    open(os.path.join(sdir, "final_final.tif"), "w").close()
    stacking.set_note(db, pid, st["id"], "erster Versuch")
    removed = stacking.delete_stack(db, ws, pid, st["id"])
    assert len(removed) == 1 and os.listdir(sdir) == ["final_final.tif"]
    assert db.value("SELECT COUNT(*) FROM stacks") == 1
    kinds = [r[0] for r in db.query("SELECT kind FROM events WHERE kind LIKE 'stack%' ORDER BY id")]
    assert kinds == ["stack_done", "stack_done", "stack_deleted"]


def test_stack_failure_is_recorded(stacked, monkeypatch):
    from astrostack import stacking
    ws, db, plan, popen, tmp = stacked
    pid = db.value("SELECT id FROM projects")
    monkeypatch.setenv("FAKE_MODE", "crash")
    runner = lambda *a, **kw: handover.run(*a, popen=popen, poll_s=0.05, **kw)          # noqa: E731
    res = stacking.stack_project(db, ws, pid, str(tmp / "pi" / "bin" / "PixInsight.exe"), runner=runner)
    assert res[0]["status"] == "failed" and res[0]["error"]
    assert db.one("SELECT status, error FROM stacks")["status"] == "failed"
    with pytest.raises(ValueError, match="Kein Lauf"):
        stacking.stack_project(db, ws, pid, "x", run_ids=["gibt-es-nicht"])


def test_stack_name():
    import datetime
    from astrostack import stacking
    assert stacking.stack_name({"name": "Ha", "hours": 12.5}, datetime.date(2026, 11, 4)) == "2026-11-04 Ha 12h30"
