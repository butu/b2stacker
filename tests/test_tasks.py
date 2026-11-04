"""Aufträge: Spuren, Vorrang, Doppel-Schutz, Abbruch, Fehler, Restzeit und Live-Ereignisse."""
import json
import threading
import time

import pytest

from astrostack import tasks as tk


@pytest.fixture
def tm():
    m = tk.TaskManager()
    yield m
    m.shutdown()


def _states(hub, task_id):
    items, _ = hub.since(0)
    return [d["state"] for _, k, d in items if k == "task" and d["id"] == task_id]


def test_task_runs_and_reports(tm):
    def work(t):
        t.progress("Header lesen", 0, 4)
        for i in range(1, 5):
            t.progress(done=i)
        return 42

    task = tm.submit("scan", "Neue Lights einlesen", work, lane="analysis", project_id=1)
    assert tm.wait_idle()
    assert task.state == "done" and task.result == 42 and task.done == 4 and task.eta_s == 0
    states = _states(tm.hub, task.id)
    assert states[0] == "queued" and "running" in states and states[-1] == "done"
    d = task.as_dict()
    assert d["started"] and d["finished"] and d["step"] == "Header lesen"
    json.dumps(d)                                                # für SSE serialisierbar


def test_one_task_per_lane_but_lanes_in_parallel(tm):
    gate = threading.Event()
    seen = []

    def blocker(t):
        seen.append(t.lane)
        gate.wait(5)

    a = tm.submit("stack", "Stack 1", blocker, lane="stack")
    b = tm.submit("stack", "Stack 2", blocker, lane="stack")
    c = tm.submit("scan", "Analyse", blocker, lane="analysis")
    deadline = time.time() + 3
    while len(seen) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert sorted(seen) == ["analysis", "stack"]                 # beide Spuren laufen gleichzeitig
    assert a.state == "running" and b.state == "queued" and c.state == "running"
    gate.set()
    assert tm.wait_idle()
    assert b.state == "done"


def test_same_key_is_not_queued_twice(tm):
    gate = threading.Event()
    t1 = tm.submit("scan", "Projekt 3 prüfen", lambda t: gate.wait(5), lane="analysis", key="scan:3")
    t2 = tm.submit("scan", "Projekt 3 prüfen", lambda t: None, lane="analysis", key="scan:3")
    assert t1 is t2
    gate.set()
    assert tm.wait_idle()
    t3 = tm.submit("scan", "Projekt 3 prüfen", lambda t: None, lane="analysis", key="scan:3")
    assert t3 is not t1                                          # nach dem Ende wieder möglich
    assert tm.wait_idle()


def test_preferred_project_runs_first(tm):
    gate = threading.Event()
    order = []
    tm.submit("scan", "läuft", lambda t: gate.wait(5), lane="analysis")
    for pid in (1, 2, 3):
        tm.submit("scan", "P%d" % pid, lambda t, p=pid: order.append(p), lane="analysis", project_id=pid)
    tm.prefer_project(3)
    gate.set()
    assert tm.wait_idle()
    assert order == [3, 1, 2]


def test_cancel_queued_and_running(tm):
    started = threading.Event()

    def long(t):
        started.set()
        for _ in range(500):
            t.check_cancel()
            time.sleep(0.01)

    running = tm.submit("stack", "lang", long, lane="stack")
    waiting = tm.submit("stack", "wartet", lambda t: None, lane="stack")
    assert started.wait(3)
    tm.cancel(waiting.id)
    assert waiting.state == "cancelled"
    tm.cancel(running.id)
    assert tm.wait_idle()
    assert running.state == "cancelled"
    assert tm.cancel("gibt-es-nicht") is None


def test_failure_is_visible(tm):
    def boom(t):
        t.progress("Rechnen")
        raise ValueError("Datei kaputt")

    task = tm.submit("scan", "x", boom)
    assert tm.wait_idle()
    assert task.state == "failed"
    assert task.error == "ValueError: Datei kaputt"
    assert "Traceback" in task.message
    # Ein Fehler stoppt die Spur nicht
    ok = tm.submit("scan", "y", lambda t: 1)
    assert tm.wait_idle() and ok.state == "done"


def test_eta_estimate():
    m = tk.TaskManager()
    try:
        t = tk.Task(m, "scan", "x", lambda t: None)
        t.progress("Vermessen", 0, 100)
        t._step_t -= 1.0                                         # 10 Frames in einer Sekunde
        t.progress(done=10)
        assert t.eta_s == 9                                      # 90 übrig bei 10 je Sekunde
        t.progress("Nächster Schritt", 0, 5)
        assert t.eta_s is None and t.done == 0 and t.total == 5
    finally:
        m.shutdown()


def test_progress_events_are_throttled(tm):
    def chatty(t):
        t.progress("Viele kleine Schritte", 0, 10000)
        for i in range(1, 10001):
            t.progress(done=i)

    task = tm.submit("scan", "x", chatty)
    assert tm.wait_idle()
    items, _ = tm.hub.since(0)
    mine = [d for _, k, d in items if d.get("id") == task.id]
    assert len(mine) < 50                                        # nicht 10000 Ereignisse
    assert mine[-1]["state"] == "done" and mine[-1]["done"] == 10000


def test_hub_replay_and_gaps():
    hub = tk.EventHub(keep=3)
    for i in range(5):
        hub.publish("task", {"n": i})
    items, complete = hub.since(3)
    assert [d["n"] for _, _, d in items] == [3, 4] and complete
    items, complete = hub.since(0)
    assert not complete                                          # zu alt: Client lädt den Stand neu
    assert hub.since(5) == ([], True)


def test_hub_wait_wakes_on_publish():
    hub = tk.EventHub()
    threading.Timer(0.05, lambda: hub.publish("notice", {"text": "hallo"})).start()
    t0 = time.monotonic()
    items, _ = hub.wait(0, timeout=3)
    assert items and time.monotonic() - t0 < 2


def test_snapshot_and_sse_format(tm):
    task = tm.submit("scan", "x", lambda t: None)
    assert tm.wait_idle()
    snap = tm.snapshot()
    assert snap["finished"][0]["id"] == task.id and snap["last_event"] >= 3
    raw = tk.sse_format(7, "task", json.dumps({"a": "zwei\nZeilen"}))
    assert raw.startswith(b"id: 7\nevent: task\ndata: ") and raw.endswith(b"\n\n")


def test_unknown_lane_rejected(tm):
    with pytest.raises(ValueError):
        tm.submit("x", "x", lambda t: None, lane="turbo")
