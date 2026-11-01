"""Hintergrund-Protokoll – damit ein Mensch oder ein KI-Agent später nachvollziehen kann, was passiert ist.

Ablage: <Arbeitsverzeichnis>/logs/
  events-JJJJ-MM.jsonl   eine Zeile pro Ereignis (JSON): Zeit, Version, Art, Projekt, Dauer, Fehler, Details
  b2stacker.log          lesbares Textprotokoll inkl. Tracebacks (rotiert, 5 × 5 MB)
  README.md              Erklärung für Agenten (wird automatisch angelegt)

Ereignisarten (kind): app_start, settings, job_start, job_end, analysis, calib_choice, stack_start, stack_step,
stack_run, stack_end, pixinsight_exit, error, http_error, move_projects …
"""
from __future__ import annotations

import datetime
import json
import logging
import logging.handlers
import os
import sys
import threading
import traceback

_lock = threading.Lock()
_state = {"dir": None}
log = logging.getLogger("b2stacker")

README = """# b2stacker – Protokolle (für Menschen und KI-Agenten)

Dieses Verzeichnis wird von b2stacker automatisch geschrieben.

* `events-JJJJ-MM.jsonl` – strukturierte Ereignisse, eine JSON-Zeile pro Ereignis:
  `{"ts": "...", "v": "<Programmversion>", "kind": "...", "project": "...", ...}`
  Wichtige Arten:
  - `app_start`, `settings` – Programmstart, geänderte Einstellungen
  - `job_start` / `job_end` – jeder Auftrag (Analyse, Stacking, Kennzahlen) mit `duration_s`, `state`, `error`
  - `analysis` – Ergebnis einer Analyse: Gruppen, Frames, Flats/Bias-Zuordnung, Warnungen, Dauer je Schritt
  - `stack_start` – Plan-Zusammenfassung; `stack_step` – Dauer je Schritt (calibrate, register, integrate …)
  - `pixinsight_exit` – Exit-Code, Kommandozeile (gekürzt), Pfad zum WBPP-Log
  - `stack_run` – Ergebnis je Lauf inkl. Qualitätskennzahlen; `stack_end` – Gesamtergebnis
  - `error` / `http_error` – Fehler mit Traceback
* `b2stacker.log` – dasselbe als lesbarer Text plus vollständige Tracebacks.
* Je Stacking-Lauf zusätzlich: `projects/<Ziel>/runs/<Zeit>/` mit `astrostack.log`, `status.json`, `plan.json`,
  `<Lauf>/command.txt` (exakter PixInsight-Aufruf) und `<Lauf>/logs/` (Original-Protokolle von WBPP).

Tipps zur Fehlersuche:
- Letzte Fehler:   `astrostack logs --kind error --last 20`
- Ein Projekt:     `astrostack logs --project "vdB 15"`
- Abstürze von PixInsight erkennt man an `pixinsight_exit` mit Code ≠ 0 (z. B. 0xC000041D = meist Speicher).
"""


def setup(workdir: str) -> str | None:
    """Protokoll im Arbeitsverzeichnis einrichten (mehrfacher Aufruf ist unschädlich)."""
    d = os.path.join(workdir, "logs")
    if _state["dir"] == d:
        return d
    try:
        os.makedirs(d, exist_ok=True)
        if not os.path.exists(os.path.join(d, "README.md")):
            with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as fh:
                fh.write(README)
    except OSError:
        return None
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    try:
        h = logging.handlers.RotatingFileHandler(os.path.join(d, "b2stacker.log"), maxBytes=5_000_000, backupCount=5,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s  %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    except OSError:
        pass
    _state["dir"] = d
    _install_hooks()
    return d


def _install_hooks():
    if getattr(_install_hooks, "done", False):
        return
    _install_hooks.done = True
    old = sys.excepthook

    def hook(t, v, tb):
        error("unbehandelter Fehler", exc=(t, v, tb))
        old(t, v, tb)
    sys.excepthook = hook

    def thook(args):
        error("unbehandelter Fehler im Thread %s" % (args.thread.name if args.thread else "?"),
              exc=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thook


def event(kind: str, **data) -> dict:
    """Ein Ereignis protokollieren. Nie Ausnahmen werfen – Protokollieren darf nichts kaputt machen."""
    try:
        from . import __version__
    except ImportError:
        __version__ = "?"
    rec = dict(ts=datetime.datetime.now().isoformat(timespec="seconds"), v=__version__, kind=kind)
    rec.update(data)
    d = _state["dir"]
    if d:
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
            with _lock, open(os.path.join(d, "events-%s.jsonl" % rec["ts"][:7]), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    try:
        short = {k: v for k, v in data.items() if k not in ("trace",)}
        log.info("%s %s", kind, json.dumps(short, ensure_ascii=False, default=str)[:2000])
    except Exception:  # noqa: BLE001
        pass
    return rec


def error(msg: str, exc=None, **data) -> dict:
    tb = "".join(traceback.format_exception(*exc)) if exc else traceback.format_exc()
    if tb.strip() == "NoneType: None":
        tb = ""
    try:
        log.error("%s\n%s", msg, tb)
    except Exception:  # noqa: BLE001
        pass
    return event("error", message=msg, trace=tb[-6000:], **data)


def text(msg: str):
    try:
        log.info(msg)
    except Exception:  # noqa: BLE001
        pass


def read_events(workdir: str, last: int = 50, kind: str | None = None, project: str | None = None) -> list[dict]:
    d = os.path.join(workdir, "logs")
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(x for x in os.listdir(d) if x.startswith("events-") and x.endswith(".jsonl"))[::-1]:
        try:
            lines = open(os.path.join(d, f), encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if kind and r.get("kind") != kind:
                continue
            if project and (r.get("project") or "").lower() != project.lower():
                continue
            out.append(r)
            if len(out) >= last:
                return out[::-1]
    return out[::-1]
