"""Hintergrund-Aufträge mit Live-Rückmeldung (Konzept Abschnitt 6).

Alles, was länger als etwa 0,5 s dauern kann, läuft als Auftrag (``Task``) und meldet laufend, was passiert,
wie weit es ist und wie lange es noch dauert. Jede Änderung geht als Ereignis an den ``EventHub``; die
Oberfläche bekommt sie per Server-Sent Events sofort, CLI und MCP lesen denselben Stand.

Spuren (``lanes``) laufen unabhängig voneinander, innerhalb einer Spur immer nur ein Auftrag:

- ``stack``: Stacken mit PixInsight – nie zwei PixInsight gleichzeitig.
- ``analysis``: automatischer Ablauf (Entdecken, Header, Vermessen, Zuordnen, Plan).
- ``misc``: alles andere (Vorschauen, Aufräumen, Sicherung).

Automatische Aufträge mit gleichem ``key`` werden nicht doppelt eingereiht. Aufträge des geöffneten
Projekts kommen in ihrer Spur zuerst dran (``prefer_project``).
"""
from __future__ import annotations

import collections
import itertools
import threading
import time
import traceback
import uuid
from typing import Any, Callable

LANES = ("stack", "analysis", "misc")
ACTIVE = ("queued", "running")
FINAL = ("done", "failed", "cancelled")


class Cancelled(Exception):
    """Wird aus ``Task.check_cancel()`` geworfen, wenn der Auftrag abgebrochen werden soll."""


def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ---------------------------------------------------------------- Ereignisse

class EventHub:
    """Verteilt Ereignisse an Abonnenten; die letzten ``keep`` werden für Wiederverbindungen aufgehoben."""

    def __init__(self, keep: int = 500):
        self._cond = threading.Condition()
        self._seq = 0
        self._buf: collections.deque = collections.deque(maxlen=keep)

    @property
    def last_id(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, kind: str, data: dict) -> int:
        with self._cond:
            self._seq += 1
            self._buf.append((self._seq, kind, data))
            self._cond.notify_all()
            return self._seq

    def since(self, last_id: int) -> tuple[list[tuple[int, str, dict]], bool]:
        """Ereignisse nach ``last_id`` → (Ereignisse, lückenlos?). Nicht lückenlos = Stand neu laden."""
        with self._cond:
            items = [e for e in self._buf if e[0] > last_id]
            complete = last_id >= self._seq or (bool(self._buf) and self._buf[0][0] <= last_id + 1)
            return items, complete

    def wait(self, last_id: int, timeout: float) -> tuple[list[tuple[int, str, dict]], bool]:
        """Wie ``since``, wartet aber höchstens ``timeout`` Sekunden auf das nächste Ereignis."""
        with self._cond:
            if self._seq <= last_id:
                self._cond.wait(timeout)
        return self.since(last_id)


def sse_format(seq: int, kind: str, data: str) -> bytes:
    """Ein Ereignis im Server-Sent-Events-Format (``data`` ist bereits JSON)."""
    lines = ["id: %d" % seq, "event: %s" % kind]
    lines += ["data: " + part for part in data.split("\n")]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


# ---------------------------------------------------------------- Aufträge

class Task:
    """Ein Auftrag. Die Arbeitsfunktion bekommt ihn als einziges Argument und meldet darüber Fortschritt."""

    _ids = itertools.count(1)

    def __init__(self, manager: "TaskManager", kind: str, title: str, fn: Callable[["Task"], Any], *,
                 lane: str = "misc", project_id: int | None = None, key: str | None = None,
                 origin: str = "ui"):
        if lane not in LANES:
            raise ValueError("unbekannte Spur: %r" % lane)
        self._m = manager
        self.id = "%s-%d-%s" % (kind, next(Task._ids), uuid.uuid4().hex[:4])
        self.kind, self.title, self.fn = kind, title, fn
        self.lane, self.project_id, self.key, self.origin = lane, project_id, key, origin
        self.state = "queued"
        self.step = ""                  # was gerade passiert, in Klartext
        self.done = 0
        self.total = 0
        self.message = ""
        self.eta_s: int | None = None
        self.result: Any = None
        self.error = ""
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.cancel_event = threading.Event()
        self._rate: float | None = None     # geglättete Einheiten je Sekunde im aktuellen Schritt
        self._step_t = 0.0
        self._step_done = 0
        self._last_pub = 0.0

    # ------------------------------------------------------------ für die Arbeitsfunktion
    def progress(self, step: str | None = None, done: int | None = None, total: int | None = None,
                 message: str | None = None) -> None:
        """Fortschritt melden. Ein neuer ``step`` setzt Zähler und Restzeit-Schätzung zurück."""
        now = time.monotonic()
        if step is not None and step != self.step:
            self.step, self.done, self.total = step, 0, 0
            self._rate, self._step_t, self._step_done = None, now, 0
            self.eta_s = None
        if total is not None:
            self.total = max(0, int(total))
        if done is not None:
            self._update_eta(int(done), now)
            self.done = int(done)
        if message is not None:
            self.message = message
        # Höchstens etwa 5 Meldungen je Sekunde; Schrittwechsel und Ende immer sofort.
        if step is not None or self.done >= self.total or now - self._last_pub >= 0.2:
            self._last_pub = now
            self._m._publish(self)

    def _update_eta(self, done: int, now: float) -> None:
        dt = now - self._step_t
        if dt <= 0 or done <= self._step_done:
            return
        rate = (done - self._step_done) / dt
        if dt >= 0.5 or self._rate is None:
            self._rate = rate if self._rate is None else 0.7 * self._rate + 0.3 * rate
            self._step_t, self._step_done = now, done
        if self._rate and self.total and done < self.total:
            self.eta_s = int(round((self.total - done) / self._rate))
        elif self.total and done >= self.total:
            self.eta_s = 0

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    # ------------------------------------------------------------ Darstellung
    def as_dict(self) -> dict:
        return dict(id=self.id, kind=self.kind, title=self.title, lane=self.lane, project_id=self.project_id,
                    key=self.key, origin=self.origin, state=self.state, step=self.step, done=self.done,
                    total=self.total, message=self.message, eta_s=self.eta_s, error=self.error,
                    created=_iso(self.created), started=_iso(self.started), finished=_iso(self.finished))


class TaskManager:
    """Verwaltet die Spuren und ihre Arbeiter-Threads."""

    def __init__(self, hub: EventHub | None = None, keep_finished: int = 50):
        self.hub = hub or EventHub()
        self._lock = threading.Condition()
        self._queues: dict[str, list[Task]] = {lane: [] for lane in LANES}
        self._running: dict[str, Task | None] = {lane: None for lane in LANES}
        self._finished: collections.deque = collections.deque(maxlen=keep_finished)
        self._preferred: int | None = None
        self._stopping = False
        self._threads = [threading.Thread(target=self._worker, args=(lane,), name="tasks-" + lane, daemon=True)
                         for lane in LANES]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------ Einreihen
    def submit(self, kind: str, title: str, fn: Callable[[Task], Any], **kw) -> Task:
        """Auftrag einreihen. Gibt es schon einen wartenden oder laufenden mit gleichem ``key``, wird
        dieser zurückgegeben und nichts Neues eingereiht."""
        with self._lock:
            key = kw.get("key")
            if key:
                for t in self._all_active():
                    if t.key == key and not t.cancelled:
                        return t
            task = Task(self, kind, title, fn, **kw)
            self._queues[task.lane].append(task)
            self._lock.notify_all()
        self._publish(task)
        return task

    def prefer_project(self, project_id: int | None) -> None:
        """Wartende Aufträge dieses Projekts zuerst ausführen (das gerade geöffnete Projekt)."""
        with self._lock:
            self._preferred = project_id

    def cancel(self, task_id: str) -> Task | None:
        with self._lock:
            task = self.get(task_id)
            if task is None or task.state in FINAL:
                return task
            task.cancel_event.set()
            if task.state == "queued":
                self._queues[task.lane].remove(task)
                self._finish(task, "cancelled")
                return task
        self._publish(task)            # läuft: die Arbeitsfunktion beendet sich beim nächsten check_cancel
        return task

    # ------------------------------------------------------------ Abfragen
    def _all_active(self) -> list[Task]:
        out = [t for t in self._running.values() if t is not None]
        for q in self._queues.values():
            out += q
        return out

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            for t in self._all_active():
                if t.id == task_id:
                    return t
            for t in self._finished:
                if t.id == task_id:
                    return t
        return None

    def active(self, project_id: int | None = None) -> list[Task]:
        with self._lock:
            tasks = self._all_active()
        return [t for t in tasks if project_id is None or t.project_id == project_id]

    def snapshot(self) -> dict:
        """Gesamter Stand für einen neu verbundenen Client, mit Ereignisnummer zum Weiterlesen."""
        with self._lock:
            last = self.hub.last_id
            return dict(last_event=last,
                        running=[t.as_dict() for t in self._running.values() if t is not None],
                        queued=[t.as_dict() for lane in LANES for t in self._queues[lane]],
                        finished=[t.as_dict() for t in reversed(self._finished)])

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Warten, bis alle Spuren leer sind (für Tests und CLI)."""
        end = time.monotonic() + timeout
        with self._lock:
            while self._all_active():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._lock.wait(left)
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            for t in self._all_active():
                t.cancel_event.set()
            self._lock.notify_all()
        for th in self._threads:
            th.join(timeout)

    # ------------------------------------------------------------ intern
    def _publish(self, task: Task) -> None:
        self.hub.publish("task", task.as_dict())

    def _next(self, lane: str) -> Task | None:
        q = self._queues[lane]
        if not q:
            return None
        if self._preferred is not None:
            for i, t in enumerate(q):
                if t.project_id == self._preferred:
                    return q.pop(i)
        return q.pop(0)

    def _finish(self, task: Task, state: str) -> None:
        task.state = state
        task.finished = time.time()
        if state == "done" and task.total:
            task.done = task.total
        if state == "done":
            task.eta_s = 0
        self._finished.append(task)
        self._lock.notify_all()
        self._publish(task)

    def _worker(self, lane: str) -> None:
        while True:
            with self._lock:
                while not self._stopping and not self._queues[lane]:
                    self._lock.wait()
                if self._stopping:
                    return
                task = self._next(lane)
                task.state, task.started = "running", time.time()
                self._running[lane] = task
            self._publish(task)
            state = "done"
            try:
                task.result = task.fn(task)
                if task.cancelled:
                    state = "cancelled"
            except Cancelled:
                state = "cancelled"
            except Exception as e:  # noqa: BLE001 – jeder Fehler landet sichtbar im Auftrag
                state = "failed"
                task.error = "%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__
                task.message = traceback.format_exc(limit=8)
            with self._lock:
                self._running[lane] = None
                self._finish(task, state)
