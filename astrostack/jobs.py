"""Hintergrund-Aufträge (Analyse, Stacking) in einer Warteschlange – einer läuft, weitere warten.

Status landet zusätzlich in <workdir>/jobs/<id>.json, damit CLI und KI ihn lesen können.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import traceback
import uuid

from . import logbook


class Job:
    def __init__(self, kind: str, title: str, workdir: str):
        self.id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.kind, self.title = kind, title
        self.state = "queued"           # queued | running | done | failed | cancelled
        self.progress = None            # dict(stage, done, total, message)
        self.log: list[str] = []
        self.result = None
        self.error = ""
        self.created = datetime.datetime.now().isoformat(timespec="seconds")
        self.finished = ""
        self.cancel = threading.Event()
        self.project = None
        self._log_step = None
        self._log_pct = -1
        self._file = os.path.join(workdir, "jobs", self.id + ".json")
        self._lock = threading.Lock()

    def write(self, msg: str):
        logbook.text("[%s] %s" % (self.id, msg))
        with self._lock:
            self.log.append("%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))
            if len(self.log) > 2000:
                self.log = self.log[-1500:]
        self.save()

    def set_progress(self, **kw):
        import time
        now = time.time()
        prev = self.progress or {}
        step = kw.get("step") or kw.get("stage")
        if step != (prev.get("step") or prev.get("stage")):
            self._step_t0 = now
            self._last_save = 0
        t0 = getattr(self, "_step_t0", now)
        kw["step_elapsed_s"] = round(now - t0)
        if "eta_s" not in kw and kw.get("total") and kw.get("done", 0) >= 2 and now - t0 > 5:
            kw["eta_s"] = round((kw["total"] - kw["done"]) * (now - t0) / kw["done"])
        kw.setdefault("started", self.created)
        self.progress = kw
        self._auto_log(kw, step)
        if now - getattr(self, "_last_save", 0) > 1.0 or kw.get("done") == kw.get("total"):
            self._last_save = now
            self.save()

    def _auto_log(self, kw: dict, step):
        """Protokoll nie leer lassen: jeder neue Schritt und jedes Viertel des Fortschritts wird notiert."""
        label = kw.get("stage") or step
        total, done = kw.get("total") or 0, kw.get("done") or 0
        if step != self._log_step:
            self._log_step, self._log_pct = step, -1
            if label:
                self.write("→ %s%s" % (label, " (%d)" % total if total else ""))
        if total:
            pct = int(100 * done / total) // 25 * 25
            if pct > self._log_pct and 0 < pct:
                self._log_pct = pct
                self.write("  %s: %d von %d (%d %%)" % (label or "Fortschritt", done, total, int(100 * done / total)))

    def as_dict(self, log_tail: int | None = 200):
        return dict(id=self.id, kind=self.kind, title=self.title, state=self.state, progress=self.progress,
                    project=self.project,
                    log=self.log[-log_tail:] if log_tail else self.log, result=self.result, error=self.error,
                    created=self.created, finished=self.finished)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.as_dict(None), fh, ensure_ascii=False, default=str)
            os.replace(tmp, self._file)
        except OSError:
            pass


class JobManager:
    """Aufträge laufen nacheinander in einem Arbeiter-Thread; neue Aufträge werden eingereiht."""

    def __init__(self):
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker: threading.Thread | None = None

    def running(self) -> Job | None:
        return next((j for j in self.jobs if j.state == "running"), None)

    def queued(self) -> list[Job]:
        return [j for j in self.jobs if j.state == "queued"]

    def active(self) -> Job | None:
        """Laufender Auftrag, sonst der nächste wartende (None = nichts zu tun)."""
        return self.running() or next(iter(self.queued()), None)

    def get(self, jid: str) -> Job | None:
        return next((j for j in self.jobs if j.id == jid), None)

    def overview(self, recent: int = 5) -> list[Job]:
        """Laufend + wartend + die letzten fertigen – für die Auftragsliste der Oberfläche."""
        busy = [j for j in self.jobs if j.state in ("running", "queued")]
        done = [j for j in self.jobs if j.state not in ("running", "queued")][-recent:]
        return busy + list(reversed(done))

    def submit(self, kind: str, title: str, workdir: str, fn, project: str | None = None) -> Job:
        """fn(job) läuft im Hintergrund, sobald alle vorherigen Aufträge fertig sind; Rückgabewert wird job.result."""
        with self._lock:
            dup = next((j for j in self.jobs if j.state in ("queued", "running") and j.kind == kind
                        and j.title == title and j.project == project), None)
            if dup:
                raise RuntimeError("Dieser Auftrag ist schon %s: %s" % (
                    "in Arbeit" if dup.state == "running" else "in der Warteschlange", dup.title))
            job = Job(kind, title, workdir)
            job.project = project
            job._fn = fn
            self.jobs.append(job)
            if len(self.jobs) > 200:                    # alte, fertige Aufträge vergessen
                keep = [j for j in self.jobs if j.state in ("queued", "running")]
                self.jobs = [j for j in self.jobs if j not in keep][-100:] + keep
            job.save()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True, name="jobs")
                self._worker.start()
            self._wake.notify_all()
        return job

    def cancel(self, job: Job) -> None:
        """Wartende Aufträge fallen sofort weg, laufende werden zum Abbruch aufgefordert."""
        with self._lock:
            if job.state == "queued":
                job.state = "cancelled"
                job.cancel.set()
                job.finished = datetime.datetime.now().isoformat(timespec="seconds")
                job.log.append("%s  Aus der Warteschlange entfernt" % datetime.datetime.now().strftime("%H:%M:%S"))
                job.save()
                logbook.event("job_dequeued", job=job.id, job_kind=job.kind, title=job.title, project=job.project)
                return
        if job.state == "running":
            job.cancel.set()
            job.write("Abbruch angefordert …")

    def _loop(self):
        while True:
            with self._lock:
                job = next((j for j in self.jobs if j.state == "queued"), None)
                if job is None:
                    self._worker = None
                    return
                job.state = "running"
            self._run(job)

    def _run(self, job: Job):
        import time
        t0 = time.time()
        kind, title = job.kind, job.title
        job.save()
        logbook.event("job_start", job=job.id, job_kind=kind, title=title, project=job.project)
        job.write("Auftrag gestartet: %s" % title)
        try:
            job.result = job._fn(job)
            if job.cancel.is_set():
                job.state = "cancelled"
            elif isinstance(job.result, dict) and job.result.get("state") in ("failed", "cancelled"):
                job.state = job.result["state"]
                job.error = job.result.get("error", "")
            else:
                job.state = "done"
        except Exception as e:  # noqa: BLE001
            job.state, job.error = ("cancelled", "") if job.cancel.is_set() else ("failed", "%s: %s" % (type(e).__name__, e))
            if job.state == "failed":
                logbook.error("Auftrag fehlgeschlagen: %s" % title, job=job.id, project=job.project)
                job.write("FEHLER: " + job.error)
                job.write(traceback.format_exc()[-1500:])
        # Endzustand eindeutig: kein "laufender" Schritt bleibt stehen (Bericht 0.8 §6/§9)
        prog = dict(job.progress or {})
        if job.state == "failed":
            prog["failed_step"] = prog.get("failed_step") or prog.get("step")
        prog.update(final=True, eta_s=None, stage={"done": "Fertig", "cancelled": "Abgebrochen",
                                                    "failed": "Fehlgeschlagen"}.get(job.state, job.state))
        if isinstance(job.result, dict) and job.result.get("causes"):
            prog["causes"] = job.result["causes"]
        job.progress = prog
        job.write({"done": "Fertig.", "cancelled": "Abgebrochen.", "failed": "Fehlgeschlagen."}.get(job.state, job.state))
        job.finished = datetime.datetime.now().isoformat(timespec="seconds")
        job._fn = None
        job.save()
        logbook.event("job_end", job=job.id, job_kind=kind, title=title, project=job.project,
                      state=job.state, error=job.error or None, duration_s=round(time.time() - t0, 1))
