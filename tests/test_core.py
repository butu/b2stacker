"""Dienst-Schicht des Neubaus: Projekte, automatischer Ablauf als Auftrag, Plan, Stacken, History."""
import os
import subprocess
import sys

import pytest

from astrostack import core, handover

from .test_flow import _calib_tree, env  # noqa: F401
from .test_handover import FAKE


@pytest.fixture
def c(env, tmp_path, monkeypatch):  # noqa: F811
    ws, db, data = env
    _calib_tree(data)
    exe = tmp_path / "pi" / "bin" / "PixInsight.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    fake = tmp_path / "fakepi.py"
    fake.write_text(FAKE, encoding="utf-8")
    def run(*a, **kw):
        return handover.run(*a, popen=lambda cmd, **k: subprocess.Popen(
            [sys.executable, str(fake)] + cmd[1:], **{x: y for x, y in k.items() if x != "creationflags"}),
            poll_s=0.05, **kw)
    co = core.Core(ws.root, pixinsight=str(exe), workers=1, stack_runner=run)
    yield co, data
    co.close()


def test_project_lifecycle(c):
    co, data = c
    r = co.create_project("vdB 131", [data], "Iris")
    assert co.tasks.wait_idle(60)
    t = co.tasks.get(r["task"]["id"])
    assert t.state == "done", t.error + t.message
    assert t.result["plan_ready"] and t.result["discover"]["new"] > 0
    tiles = co.projects()
    assert tiles[0]["name"] == "vdB 131" and tiles[0]["lights"] == 6 and tiles[0]["months"] == ["2026-07", "2026-10"]
    assert {f["filter"] for f in tiles[0]["filters"]} == {"HaO3", "RGB"} and tiles[0]["hours"] == 1.0
    p = co.project("VDB 131")
    assert p["counts"]["light"] == 7 and p["plan"]["ready"] and p["folders"][0]["online"] == 1
    fr = co.frames("vdB 131", status="skip")
    assert fr["total"] == 1 and fr["frames"][0]["reason"]
    co.rename("vdB 131", "Iris-Nebel")
    assert co.projects()[0]["name"] == "Iris-Nebel"
    with pytest.raises(ValueError):
        co.project("gibt es nicht")


def test_refresh_is_coalesced_and_open_prefers(c):
    co, data = c
    co.create_project("A", [data])
    t1 = co.refresh("A")
    t2 = co.refresh("A")
    assert t1 is t2 or t2.state in ("queued", "running")
    co.open_project("A")
    assert co.tasks.wait_idle(60)


def test_decide_and_plan_edit_and_stack(c):
    co, data = c
    co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    fr = co.frames("vdB 131", status="use")
    first = fr["frames"][0]["id"]
    assert co.decide("vdB 131", [first], "skip", "Flugzeug")["changed"] == 1
    assert co.frames("vdB 131", status="skip")["total"] == 2
    plan = co.edit_plan("vdB 131", options={"local_normalization": True})
    assert any(s["edited"] for r in plan["runs"] for s in r["settings"])
    t = co.stack("vdB 131")
    assert co.tasks.wait_idle(60)
    task = co.tasks.get(t["id"])
    assert task.state == "done", task.error + task.message
    st = co.stacks("vdB 131")
    assert st[0]["status"] == "done" and st[0]["files"]
    co.note("vdB 131", st[0]["id"], "gut")
    assert co.stacks("vdB 131")[0]["note"] == "gut"
    kinds = [h["kind"] for h in co.history("vdB 131")]
    assert kinds[0] == "stack_done" and "frames_decided" in kinds and "project_created" in kinds
    assert co.history()[0]["project"] == "vdB 131"
    assert co.backup().endswith(".db")


def test_stack_refused_without_pixinsight(c):
    co, data = c
    co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    co.pixinsight = os.path.join(data, "gibt-es-nicht.exe")
    with pytest.raises(ValueError, match="PixInsight"):
        co.stack("vdB 131")


def test_events_are_published(c):
    co, data = c
    co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    items, _ = co.tasks.hub.since(0)
    kinds = {k for _, k, _ in items}
    assert {"task", "projects", "project"} <= kinds
