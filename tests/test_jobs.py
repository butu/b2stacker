"""Auftrags-Warteschlange: nacheinander abarbeiten, wartende abbrechen, Protokoll nie leer."""
import threading
import time

import pytest

from astrostack.jobs import JobManager


def _wait(cond, t=5):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_jobs_queue_run_in_order_and_queued_can_be_cancelled(tmp_path):
    m, order, gate = JobManager(), [], threading.Event()

    def make(n, wait=False):
        def fn(job):
            if wait:
                gate.wait(5)
            order.append(n)
            return n
        return fn
    a = m.submit("analyze", "A", str(tmp_path), make("A", True), project="p1")
    b = m.submit("stack", "B", str(tmp_path), make("B"), project="p2")
    c = m.submit("stack", "C", str(tmp_path), make("C"), project="p3")
    assert _wait(lambda: a.state == "running")
    assert [j.state for j in (b, c)] == ["queued", "queued"]
    assert m.active() is a and m.queued() == [b, c]
    with pytest.raises(RuntimeError):
        m.submit("stack", "C", str(tmp_path), make("C2"), project="p3")      # Doppelklick reiht nicht doppelt ein
    m.cancel(b)
    assert b.state == "cancelled"
    gate.set()
    assert _wait(lambda: c.state == "done")
    assert order == ["A", "C"] and a.state == "done"
    assert [j.id for j in m.overview()][:2] in ([c.id, b.id], [c.id, a.id])


def test_running_job_cancel_and_protocol_is_never_empty(tmp_path):
    m = JobManager()

    def fn(job):
        job.set_progress(stage="Fotos vermessen", step="measure", done=0, total=4)
        for i in range(1, 5):
            job.set_progress(stage="Fotos vermessen", step="measure", done=i, total=4)
            if job.cancel.wait(0.05):
                raise RuntimeError("abgebrochen")
    j = m.submit("analyze", "Analyse X", str(tmp_path), fn, project="X")
    assert _wait(lambda: j.state in ("done", "cancelled", "failed"))
    text = "\n".join(j.log)
    assert "Fotos vermessen" in text and "4 von 4" in text and "Fertig." in text
    assert j.as_dict()["project"] == "X"

    gate = threading.Event()
    k = m.submit("stack", "Stack Y", str(tmp_path), lambda job: (gate.set(), job.cancel.wait(5), None)[-1], project="Y")
    assert gate.wait(5)
    m.cancel(k)
    assert _wait(lambda: k.state == "cancelled")
