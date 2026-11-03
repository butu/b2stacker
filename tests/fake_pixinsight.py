"""Nachgebautes PixInsight/WBPP für Ende-zu-Ende-Tests – als **eigener Prozess** mit der echten Kommandozeile.

Was nachgebildet wird (so wie es PixInsight 1.9.6 im Automationsmodus tut):

* ``-r="script,arg,arg,…"`` wird **am Komma** getrennt – ein Komma im Pfad zerlegt das Argument.
* Windows-Grenze der Kommandozeile (~32 767 Zeichen): zu lang → der Start scheitert wie bei CreateProcess.
* Bildtyp kommt **nur aus dem FITS-Header** (IMAGETYP), wie bei WBPP.
* Grouping Keyword ``ASFLAT_<n>`` im Pfad: jede Light-Gruppe braucht Flats mit demselben Kennwort.
* Nur 1–2 Roh-Bias/-Darks/-Flats ohne Master → WBPP bricht die Integration ab, kein Master entsteht.
* Ausgabepfade über 260 Zeichen ohne LongPaths → Fehler beim Schreiben.

Ergebnis: WBPP-artiges Protokoll in ``<out>/logs``, ``registered``/``master`` wie WBPP, und ``fakepi-input.json`` mit
allem, was angekommen ist – damit Tests die Invarianten prüfen können (welche Dateien, welche Gruppen, welche Pfade).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class ProcRunner:
    """Runner-Ersatz für pixinsight.run_plan: startet diesen Fake als Prozess mit der echten Kommandozeile."""
    last = None

    def __init__(self, long_paths: bool = True):
        self.proc = None
        self.long_paths = long_paths
        ProcRunner.last = self

    def start(self, script, items):
        from astrostack.engines import pixinsight as pi
        cmd = pi.build_command("PixInsight.exe", script, items)      # dieselbe Prüfung wie im echten Lauf
        args = [sys.executable, os.path.abspath(__file__), "--cmd", cmd]
        if not self.long_paths:
            args.append("--no-long-paths")
        env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        if len(subprocess.list2cmdline(args)) > 32767:                # wie CreateProcess (WinError 206)
            raise OSError(206, "Der Dateiname oder die Erweiterung ist zu lang (Kommandozeile)")
        self.proc = subprocess.Popen(args, env=env)
        return cmd

    def poll(self):
        return self.proc.poll() if self.proc else 0

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


# ---------------------------------------------------------------- der "PixInsight"-Prozess

def _imagetyp(path: str) -> str:
    try:
        if path.lower().endswith(".xisf"):
            from astrostack import xisf
            h = xisf.read_header(path)
        else:
            from astropy.io import fits
            h = dict(fits.getheader(path))
        return str(h.get("IMAGETYP", h.get("FRAME", "LIGHT"))).strip().lower()
    except Exception as e:  # noqa: BLE001
        return "unreadable: %s" % e


def _kind(t: str) -> str:
    master = "master" in t
    for k in ("darkflat", "flatdark", "bias", "dark", "flat"):
        if k in t.replace(" ", "").replace("-", ""):
            k = {"flatdark": "darkflat"}.get(k, k)
            return ("master_" if master else "") + k
    return "light"


def _group(path: str) -> str | None:
    m = re.search(r"[\\/]ASFLAT_([^\\/]+)[\\/]", path)
    return m.group(1) if m else None


def main(argv):
    cmd = argv[argv.index("--cmd") + 1]
    long_paths = "--no-long-paths" not in argv
    m = re.search(r'-r="([^"]*)"', cmd)
    errors, warnings, lines = [], [], []
    now = lambda: datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")   # noqa: E731
    parts = m.group(1).split(",") if m else []
    script, args = (parts[0], parts[1:]) if parts else ("", [])
    opts, files = {}, []
    for a in args:
        k, sep, v = a.partition("=")
        if not sep:
            errors.append("*** Error: Invalid argument: '%s' (Komma im Pfad?)" % a)
            continue
        if k == "file":
            files.append(v)
        elif k == "dir":
            if not os.path.isdir(v):
                errors.append("*** Error: Directory not found: " + v)
                continue
            for r, _d, fs in os.walk(v):
                files += [os.path.join(r, f) for f in sorted(fs) if f.lower().endswith((".fits", ".fit", ".fts", ".xisf"))]
        else:
            opts[k] = v
    out = opts.get("outputDirectory")
    if not out:
        print("kein outputDirectory", file=sys.stderr)
        return 2
    os.makedirs(os.path.join(out, "logs"), exist_ok=True)
    by = {}
    missing = []
    for f in files:
        if not os.path.exists(f):
            missing.append(f)
            continue
        by.setdefault(_kind(_imagetyp(f)), []).append(f)
    for f in missing:
        errors.append("*** Error: File not found: " + f)
    grouping = opts.get("groupingKeywordsEnabled") == "true"
    groups = {}
    if grouping:
        for k in ("light", "flat", "master_flat"):
            for f in by.get(k, []):
                groups.setdefault(_group(f) or "-", {}).setdefault("flat" if "flat" in k else "light", []).append(f)
        for g, d in sorted(groups.items()):
            if d.get("light") and not d.get("flat"):
                warnings.append("** Warning: no master flat for light frames group ASFLAT_%s" % g)
    # Integration der Kalibrierframes
    master_ok = {}
    for k in ("bias", "dark", "flat", "darkflat"):
        n = len(by.get(k, []))
        master_ok[k] = bool(by.get("master_" + k)) or n >= 3
        if 0 < n < 3 and not by.get("master_" + k):
            errors.append("** Canceled: Integration [%s frames … frames = %d (%d active)]: only %d active frames" % (
                k.capitalize(), n, n, n))
    if by.get("flat") and not master_ok["bias"] and not by.get("darkflat"):
        errors.append("*** Error: Flat frames not calibrated: the master bias required … could not be generated")
    # Ausgabepfade (WBPP hängt lange Namen an)
    sample = os.path.join(out, "calibrated", "light", "BIN-1_4144x2822_EXPOSURE-180.00s_FILTER-NoFilter_Mono_0001_c_cc_d.xisf")
    if not long_paths and len(sample) > 256:
        errors.append("*** Error: Cannot create files with a path longer than 256 characters: " + sample)
    lights = by.get("light", [])
    if not lights:
        errors.append("No frames to be measured")
    ok = not [e for e in errors if e.startswith(("*** Error", "No frames", "** Canceled: Integration [Bias"))]
    lines.append("%s WBPP fake for b2stacker tests" % now())
    lines.append("%s * Processing %d light frames" % (now(), len(lights)))
    lines += ["%s %s" % (now(), e) for e in warnings + errors]
    report = dict(script=script, options=opts, files={k: sorted(v) for k, v in by.items()}, missing=missing,
                  grouping=grouping, groups={g: {k: len(v) for k, v in d.items()} for g, d in groups.items()},
                  errors=errors, warnings=warnings, cmd_len=len(cmd), ok=ok)
    json.dump(report, open(os.path.join(out, "fakepi-input.json"), "w", encoding="utf-8"), indent=1)
    if ok:
        os.makedirs(os.path.join(out, "registered"), exist_ok=True)
        for i, _f in enumerate(lights):
            open(os.path.join(out, "registered", "L%04d_r.xisf" % i), "wb").close()
        os.makedirs(os.path.join(out, "master"), exist_ok=True)
        sys.path.insert(0, ROOT)
        from astrostack import xisf
        from tests.test_stackqa import synth_master
        xisf.write_image(os.path.join(out, "master", "masterLight_BIN-1_FILTER-RGB_RGB.xisf"),
                         synth_master(shape=(300, 450)), {"OBJECT": "fake"})
        lines.append("%s * End of integration: %d frames" % (now(), len(lights)))
    else:
        lines.append("%s %d frame(s) excluded" % (now(), len(lights)))
    with open(os.path.join(out, "logs", datetime.datetime.now().strftime("%Y%m%d%H%M%S") + ".log"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
