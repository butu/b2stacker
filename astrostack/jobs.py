"""Hintergrund-Aufträge (Analyse, Stacking) – immer nur einer gleichzeitig.

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
        if now - getattr(self, "_last_save", 0) > 1.0 or kw.get("done") == kw.get("total"):
            self._last_save = now
            self.save()

    def as_dict(self, log_tail: int | None = 200):
        return dict(id=self.id, kind=self.kind, title=self.title, state=self.state, progress=self.progress,
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
    def __init__(self):
        self.jobs: list[Job] = []
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        return next((j for j in self.jobs if j.state in ("queued", "running")), None)

    def get(self, jid: str) -> Job | None:
        return next((j for j in self.jobs if j.id == jid), None)

    def submit(self, kind: str, title: str, workdir: str, fn, project: str | None = None) -> Job:
        """fn(job) läuft im Hintergrund; Rückgabewert wird job.result."""
        with self._lock:
            if self.active():
                raise RuntimeError("Es läuft bereits ein Auftrag: " + self.active().title)
            job = Job(kind, title, workdir)
            job.project = project
            self.jobs.append(job)
        def _run():
            import time
            t0 = time.time()
            job.state = "running"
            job.save()
            logbook.event("job_start", job=job.id, job_kind=kind, title=title, project=getattr(job, "project", None))
            try:
                job.result = fn(job)
                if job.cancel.is_set():
                    job.state = "cancelled"
                elif isinstance(job.result, dict) and job.result.get("state") in ("failed", "cancelled"):
                    job.state = job.result["state"]
                    job.error = job.result.get("error", "")
                else:
                    job.state = "done"
            except Exception as e:  # noqa: BLE001
                job.state, job.error = "failed", "%s: %s" % (type(e).__name__, e)
                logbook.error("Auftrag fehlgeschlagen: %s" % title, job=job.id, project=getattr(job, "project", None))
                job.write("FEHLER: " + job.error)
                job.write(traceback.format_exc()[-1500:])
            job.finished = datetime.datetime.now().isoformat(timespec="seconds")
            job.save()
            logbook.event("job_end", job=job.id, job_kind=kind, title=title, project=getattr(job, "project", None),
                          state=job.state, error=job.error or None, duration_s=round(time.time() - t0, 1))
        threading.Thread(target=_run, daemon=True, name="job-" + job.id).start()
        return job
