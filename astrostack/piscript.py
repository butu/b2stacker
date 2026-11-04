"""Ein eigenes PixInsight-Skript (PJSR) im Automatik-Modus ausführen – für Diagnose und Entwicklung.

Wird über die App als normaler Auftrag gestartet (sichtbar, abbrechbar, in derselben Warteschlange wie das
Stacken, also nie parallel zu einem WBPP-Lauf). Das Skript muss im Arbeitsverzeichnis liegen; Rohdaten und
PixInsight-Installation sind tabu. Die Ausgabe von PixInsight (stdout/stderr) landet zeilenweise im Protokoll
des Auftrags und vollständig in ``<workdir>/logs/pixinsight-scripts/``.

Wichtig (PixInsight 1.9.6): Ein Skript ohne ``#engine v8`` in der ersten Zeile läuft nicht (die alte
SpiderMonkey-Engine ist veraltet) – PixInsight beendet sich dann ohne Meldung mit Code 0.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import threading
import time

from .engines.pixinsight import StackError, build_command

EXTENSIONS = (".js", ".scp")
DEFAULT_TIMEOUT_S = 30 * 60
MAX_TIMEOUT_S = 6 * 3600
TAIL_LINES = 200


class ScriptError(ValueError):
    pass


_popen = subprocess.Popen       # in Tests ersetzbar


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def check(script: str, workdir: str, args: list[str] | None = None) -> tuple[str, list[str], list[str]]:
    """Skript und Argumente prüfen. → (absoluter Pfad, Argumente, Hinweise)"""
    script = (script or "").strip().strip('"')
    if not script:
        raise ScriptError("Kein Skript angegeben.")
    if not os.path.isabs(script):
        script = os.path.join(workdir, script)
    script = os.path.abspath(script)
    if not _inside(script, workdir):
        raise ScriptError("Das Skript muss im Arbeitsverzeichnis liegen (%s): %s" % (workdir, script))
    if os.path.splitext(script)[1].lower() not in EXTENSIONS:
        raise ScriptError("Nur PixInsight-Skripte (%s) können ausgeführt werden: %s" % (", ".join(EXTENSIONS), script))
    if not os.path.isfile(script):
        raise ScriptError("Skript nicht gefunden: " + script)
    args = [str(a) for a in (args or [])]
    for a in args:
        if "," in a or '"' in a:
            raise ScriptError("Argumente dürfen kein Komma und kein Anführungszeichen enthalten "
                              "(PixInsight trennt am Komma): " + a)
    notes = []
    if script.lower().endswith(".js"):
        try:
            with open(script, encoding="utf-8", errors="replace") as fh:
                first = fh.readline().strip()
        except OSError:
            first = ""
        if first != "#engine v8":
            notes.append("Erste Zeile ist nicht „#engine v8“ – PixInsight 1.9.6 führt das Skript dann "
                         "vermutlich nicht aus.")
    return script, args, notes


def run(exe: str, script: str, args: list[str], workdir: str, log=print, cancel: threading.Event | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S, popen=None) -> dict:
    """PixInsight mit dem Skript starten und auf das Ende warten. → Ergebnis mit Exit-Code und Ausgabe."""
    if not exe or not os.path.isfile(exe):
        raise ScriptError("PixInsight nicht gefunden: %s" % (exe or "–"))
    timeout_s = max(10.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    try:
        cmd = build_command(exe, script, args)
    except StackError as e:
        raise ScriptError(str(e)) from e
    logdir = os.path.join(workdir, "logs", "pixinsight-scripts")
    os.makedirs(logdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = os.path.join(logdir, "%s_%s.log" % (stamp, os.path.splitext(os.path.basename(script))[0]))
    log("Starte PixInsight: %s" % os.path.basename(script))
    log("Kommandozeile: %s" % cmd)

    popen = popen or _popen
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000                       # CREATE_NO_WINDOW
        proc = popen(cmd, **kw)
    else:
        import shlex
        proc = popen(shlex.split(cmd), **kw)

    tail: list[str] = []
    lines_total = 0

    def reader():
        nonlocal lines_total
        with open(logfile, "w", encoding="utf-8") as out:
            out.write("# %s\n" % cmd)
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                out.write(line + "\n")
                out.flush()
                lines_total += 1
                tail.append(line)
                if len(tail) > TAIL_LINES:
                    del tail[0]
                if line.strip():
                    log("  PI: " + line)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t0 = time.time()
    state = "done"
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            state = "cancelled"
        elif time.time() - t0 > timeout_s:
            state = "timeout"
        if state != "done":
            log("PixInsight wird beendet (%s)." % ("abgebrochen" if state == "cancelled" else "Zeitlimit erreicht"))
            _kill(proc)
            break
        time.sleep(0.2)
    proc.wait()
    th.join(5)
    code = proc.returncode
    log("PixInsight beendet nach %.1f s, Code %s." % (time.time() - t0, code))
    return dict(script=script, args=args, exit_code=code, state=state, duration_s=round(time.time() - t0, 1),
                output_lines=lines_total, output_tail=tail[-60:], logfile=logfile, command=cmd)


def _kill(proc) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=0x08000000)
    else:
        proc.kill()
