"""Stacking mit PixInsight WBPP im Automationsmodus.

Erprobter Ablauf (siehe docs/ERKENNTNISSE.md):
- PixInsight.exe -n --automation-mode -r="WBPP.js,automationMode=true,param=wert,…,dir=…,file=…" --force-exit
- WBPP lädt im Automationsmodus KEINE gespeicherten GUI-Einstellungen – alles kommt über Parameter.
- Dualband an Farbkamera: "separated" debayern ist in WBPP 2.7.8 (Automation) defekt → Farbbild stacken,
  danach Kanäle per PixelMath aufteilen (Ha = R, OIII = (G+B)/2).
- Nur eine PixInsight-Instanz gleichzeitig (alte Instanzen fressen RAM → Absturz 0xC000041D).
- Energiesparmodus während des Laufs verhindern (sonst stundenlange Pausen).
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from .. import system
from ..fitsmeta import BIAS, DARK, FLAT

IMAGE_EXT = (".fits", ".fit", ".fts", ".xisf")
STAGES = [("calibrated", "Kalibrieren"), ("cosmetized", "Hotpixel"), ("debayered", "Debayern"),
          ("registered", "Ausrichten"), ("master", "Integrieren")]
MAX_CMDLINE = 30000

CRASH_CODES = {
    0xC000041D: "unbehandelte Ausnahme – meist zu wenig Arbeitsspeicher",
    0xC00000FD: "Stapelüberlauf – meist zu wenig Arbeitsspeicher",
    0xC0000017: "kein Arbeitsspeicher mehr",
    0xC0000005: "Speicherzugriffsfehler",
    0xC0000409: "Programmabbruch",
}

WBPP_DEFAULTS = {
    "imageRegistration": "true",
    "subframeWeightingEnabled": "true",
    "integrate": "true",
    "platesolve": "false",
    "generateRejectionMaps": "true",
    "saveFrameGroups": "false",
}


class StackError(Exception):
    pass


class Cancelled(StackError):
    pass


@dataclass
class Status:
    state: str = "pending"            # pending | running | done | failed | cancelled
    run: str = ""
    stage: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    started: str = ""
    finished: str = ""
    finals: list = field(default_factory=list)
    runs: dict = field(default_factory=dict)
    error: str = ""

    def as_dict(self):
        return dict(self.__dict__)


# ---------------------------------------------------------------- Hilfsfunktionen

def pi_path(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/")


def check_item(item: str):
    key, _, val = item.partition("=")
    if "," in item:
        raise StackError("Pfad enthält ein Komma – das erlaubt PixInsight im Automationsmodus nicht: " + val)
    if "=" in val:
        raise StackError("Pfad enthält ein Gleichheitszeichen – das erlaubt WBPP nicht: " + val)
    if '"' in item:
        raise StackError("Pfad enthält Anführungszeichen: " + val)


def build_command(exe: str, script: str, items: list[str]) -> str:
    for it in items:
        check_item(it)
    r = '-r="' + pi_path(script) + "," + ",".join(items) + '"'
    return '"%s" -n --automation-mode %s --force-exit' % (exe, r)


def _images_below(d: str) -> set[str]:
    res = set()
    for root, _dirs, files in os.walk(d):
        for f in files:
            if f.lower().endswith(IMAGE_EXT):
                res.add(os.path.normcase(os.path.abspath(os.path.join(root, f))))
    return res


def compress_paths(paths: list[str]) -> list[str]:
    """Pfade → WBPP-Argumente. Ganzer Ordner als dir=, wenn genau alle Bilder darin (rekursiv) gewählt sind."""
    by_dir: dict[str, list[str]] = {}
    for p in paths:
        by_dir.setdefault(os.path.dirname(os.path.abspath(p)), []).append(p)
    items = []
    for d, ps in sorted(by_dir.items()):
        chosen = {os.path.normcase(os.path.abspath(p)) for p in ps}
        if ".." not in d and _images_below(d) == chosen:
            items.append("dir=" + pi_path(d))
        else:
            items += ["file=" + pi_path(p) for p in sorted(ps)]
    return items


def fix_calibration(paths: list[str], ftype: str, cache_dir: str) -> list[str]:
    """Kalibrierframes, deren IMAGETYP nicht stimmt (z. B. NINA-Snapshot-Bias mit IMAGETYP=LIGHT),
    als korrigierte Kopie bereitstellen – WBPP richtet sich nur nach dem Header."""
    from astropy.io import fits
    import hashlib
    want = {BIAS: "Bias Frame", DARK: "Dark Frame", FLAT: "Flat Frame"}[ftype]
    # eigener Ordner je Satz (Hash über die Pfade), damit dir= nie fremde Dateien mitnimmt
    cache_dir = os.path.join(cache_dir, ftype + "_" + hashlib.sha1(
        "|".join(sorted(os.path.abspath(p) for p in paths)).encode()).hexdigest()[:12])
    out = []
    for p in paths:
        if not p.lower().endswith((".fits", ".fit", ".fts")):
            out.append(p)
            continue
        try:
            hdr = fits.getheader(p)
        except Exception:
            out.append(p)
            continue
        cur = str(hdr.get("IMAGETYP", hdr.get("FRAME", ""))).lower()
        if ftype.lower() in cur:
            out.append(p)
            continue
        os.makedirs(cache_dir, exist_ok=True)
        dst = os.path.join(cache_dir, hashlib.sha1(os.path.abspath(p).encode()).hexdigest()[:8] + "_" + os.path.basename(p))
        if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(p):
            with fits.open(p) as hd:
                hd[0].header["IMAGETYP"] = want
                hd.writeto(dst, overwrite=True)
        out.append(dst)
    return out


def find_master(outdir: str) -> str | None:
    ms = sorted(glob.glob(os.path.join(outdir, "master", "masterLight*.xisf")), key=os.path.getmtime)
    if not ms:
        return None
    combined = [m for m in ms if not any(m.endswith(s) for s in ("_R.xisf", "_G.xisf", "_B.xisf"))]
    # Drizzle- / Autocrop-Varianten bevorzugt nicht nehmen: die "normale" Master-Datei
    plain = [m for m in combined if "drizzle" not in os.path.basename(m).lower() and "autocrop" not in os.path.basename(m).lower()]
    return (plain or combined or ms)[-1]


def stage_progress(outdir: str, n_lights: int) -> tuple[str, int, int]:
    """Fortschritt aus den Zwischenordnern ablesen → (Stufe, erledigt, gesamt)."""
    if glob.glob(os.path.join(outdir, "master", "masterLight*.xisf")):
        return "Fertig integriert", 1, 1
    last = ("Vorbereiten", 0, n_lights)
    for sub, label in STAGES[:-1]:
        d = os.path.join(outdir, sub)
        if os.path.isdir(d):
            n = sum(1 for _r, _d, fs in os.walk(d) for f in fs if f.lower().endswith(".xisf"))
            last = (label, min(n, n_lights), n_lights)
    if last[0] == "Ausrichten" and last[1] >= n_lights * 0.9:
        # Registrieren fertig → Local Normalization / Integration laufen
        lnf = sum(1 for _r, _d, fs in os.walk(os.path.join(outdir, "registered")) for f in fs if f.lower().endswith(".xnml"))
        if lnf:
            return "Normalisieren / Integrieren", lnf, n_lights
    return last


def crash_text(code: int) -> str:
    c = code & 0xFFFFFFFF
    if c in CRASH_CODES:
        return "PixInsight ist abgestürzt (0x%08X: %s)." % (c, CRASH_CODES[c])
    return "PixInsight beendet mit Code %d." % code


def safe_file(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "Ziel"


def archive_existing(path: str):
    """Vorhandenes Ergebnis nicht überschreiben, sondern nach final/_alt verschieben."""
    if os.path.exists(path):
        alt = os.path.join(os.path.dirname(path), "_alt")
        os.makedirs(alt, exist_ok=True)
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d-%H%M")
        base, ext = os.path.splitext(os.path.basename(path))
        shutil.move(path, os.path.join(alt, "%s_%s%s" % (base, stamp, ext)))


# ---------------------------------------------------------------- Ausführung

class Runner:
    """Startet PixInsight und wartet – austauschbar für Tests."""

    def __init__(self, exe: str):
        self.exe = exe
        self.proc = None

    def start(self, script: str, items: list[str]):
        cmd = build_command(self.exe, script, items)
        if sys.platform == "win32":
            self.proc = subprocess.Popen(cmd, creationflags=0x08000000)  # CREATE_NO_WINDOW
        else:
            self.proc = subprocess.Popen(shlex.split(cmd))
        return cmd

    def poll(self):
        return self.proc.poll() if self.proc else 0

    def kill(self):
        if not self.proc or self.proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
        else:
            self.proc.kill()


def wbpp_items(plan: dict, run: dict, outdir: str, reference: str | None, calib: dict) -> list[str]:
    opt = plan.get("options", {})
    items = ["automationMode=true", "outputDirectory=" + pi_path(outdir)]
    p = dict(WBPP_DEFAULTS)
    p.update({
        "distortionCorrection": str(bool(opt.get("distortion_correction", True))).lower(),
        "localNormalization": str(bool(opt.get("local_normalization", True))).lower(),
        "autocrop": str(bool(opt.get("autocrop", False))).lower(),
        "lightsLargeScaleRejectionHigh": str(bool(opt.get("large_scale_rejection", True))).lower(),
        "subframesWeightsMethod": {"psf_signal": "0", "psf_snr": "1", "snr": "2"}.get(opt.get("weighting", "psf_signal"), "0"),
    })
    if run["mode"] in ("debayer", "extract"):
        p["debayerOutputMethod"] = "0"      # combined (separated ist in Automation defekt)
    if reference:
        p["bestFrameRefernceMethod"] = "0"  # (sic) Schreibweise wie in WBPP
        p["referenceImage"] = pi_path(reference)
    else:
        p["bestFrameRefernceMethod"] = "1"
    p.update({k: str(v) for k, v in (run.get("wbpp") or {}).items()})   # Experten-Überschreibungen
    items += ["%s=%s" % kv for kv in p.items()]
    for key in ("bias", "darks", "darkflats", "flats"):
        items += compress_paths(calib.get(key, []))
    items += compress_paths(run["lights"])
    return items


def stage_lights(paths: list[str], dst: str, log) -> list[str]:
    """Bei sehr vielen Einzeldateien: Lights in einen Ordner verlinken (Hardlink) bzw. kopieren."""
    os.makedirs(dst, exist_ok=True)
    out, copied = [], 0
    for i, p in enumerate(paths):
        t = os.path.join(dst, "%05d_%s" % (i, os.path.basename(p)))
        if not os.path.exists(t):
            try:
                os.link(p, t)
            except OSError:
                shutil.copy2(p, t)
                copied += 1
        out.append(t)
    if copied:
        log("  %d Lights in den Arbeitsordner kopiert (Hardlink nicht möglich)" % copied)
    return out


def run_plan(plan: dict, settings, log=print, on_status=None, cancel: threading.Event | None = None,
             runner_factory=None, status_file: str | None = None, open_results: bool | None = None) -> Status:
    """Alle aktivierten Läufe eines Plans nacheinander ausführen."""
    cancel = cancel or threading.Event()
    real_pi = runner_factory is None
    runner_factory = runner_factory or (lambda: Runner(settings.pixinsight))
    st = Status(state="running", started=datetime.datetime.now().isoformat(timespec="seconds"))
    pdir = plan["workdir"]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    jobdir = os.path.join(pdir, "runs", stamp)
    finaldir = os.path.join(pdir, "final")
    os.makedirs(jobdir, exist_ok=True)
    os.makedirs(finaldir, exist_ok=True)
    status_file = status_file or os.path.join(jobdir, "status.json")
    logfile = open(os.path.join(jobdir, "astrostack.log"), "a", encoding="utf-8")

    def _log(msg):
        line = "%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
        logfile.write(line + "\n")
        logfile.flush()
        log(msg)

    def _save():
        tmp = status_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st.as_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, status_file)
        if on_status:
            on_status(st)

    json.dump(plan, open(os.path.join(jobdir, "plan.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    masters: dict[str, str] = {}
    keep = system.KeepAwake() if settings.keep_awake else None
    try:
        if keep:
            keep.__enter__()
        if real_pi and not os.path.exists(settings.pixinsight):
            raise StackError("PixInsight nicht gefunden: %s – Pfad in den Einstellungen prüfen." % settings.pixinsight)
        runs = [r for r in plan["runs"] if r.get("enabled", True)]
        if not runs:
            raise StackError("Kein Lauf aktiviert.")
        for run in runs:
            if cancel.is_set():
                raise Cancelled("abgebrochen")
            st.run, st.stage, st.done, st.total = run["name"], "Prüfen", 0, len(run["lights"])
            _save()
            masters[run["name"]] = _one_run(plan, run, settings, jobdir, masters, finaldir, st, _log, _save, cancel, runner_factory)
        st.state = "done"
        _log("Fertig: " + ", ".join(os.path.basename(f) for f in st.finals))
        if (settings.open_results if open_results is None else open_results) and st.finals and real_pi:
            subprocess.Popen([settings.pixinsight] + st.finals)
    except Cancelled:
        st.state, st.error = "cancelled", "vom Benutzer abgebrochen"
        _log("Abgebrochen.")
    except StackError as e:
        st.state, st.error = "failed", str(e)
        _log("FEHLER: " + str(e))
    except Exception as e:  # noqa: BLE001 – alles melden statt still sterben
        st.state, st.error = "failed", "%s: %s" % (type(e).__name__, e)
        _log("FEHLER: %s: %s" % (type(e).__name__, e))
    finally:
        if keep:
            keep.__exit__(None, None, None)
        st.finished = datetime.datetime.now().isoformat(timespec="seconds")
        _save()
        logfile.close()
    return st


def _one_run(plan, run, settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory) -> str:
    name = run["name"]
    outdir = os.path.join(jobdir, safe_file(name))
    os.makedirs(outdir, exist_ok=True)
    n = len(run["lights"])
    log("=== Lauf %s: %d Lights ===" % (name, n))
    missing = [p for p in run["lights"] if not os.path.exists(p)]
    if missing:
        raise StackError("%d Lights fehlen, z. B. %s" % (len(missing), missing[0]))
    # Platz + RAM + andere PixInsight-Instanzen
    free = system.free_disk_gb(outdir)
    need = run.get("gb") or 0
    if free is not None and free < need:
        raise StackError("Zu wenig Platz im Arbeitsverzeichnis: %.0f GB nötig, %.0f GB frei." % (need, free))
    others = system.pixinsight_processes()
    if others:
        raise StackError("PixInsight läuft bereits (%d Instanz(en), %s MB). Bitte schließen – zwei Instanzen "
                         "teilen sich den Arbeitsspeicher und stürzen ab." % (
                             len(others), "/".join("%.0f" % (o["mem_mb"] or 0) for o in others)))
    fr, tot = system.ram_gb()
    want = min(run.get("ram_gb") or settings.min_free_ram_gb, settings.min_free_ram_gb)
    if fr is not None and fr < want:
        raise StackError("Nur %.1f GB Arbeitsspeicher frei (mindestens %.0f GB nötig). Andere Programme schließen." % (fr, want))
    log("  Platz: %s GB frei / ~%.0f GB nötig · RAM: %s GB frei" % (
        "%.0f" % free if free is not None else "?", need, "%.1f" % fr if fr is not None else "?"))
    # Referenz
    ref = None
    r = run.get("reference") or "auto"
    if r.startswith("master:"):
        src = r[7:]
        ref = masters.get(src)
        if not ref:
            cand = os.path.join(finaldir, "%s_%s.xisf" % (safe_file(plan["target"]), safe_file(src)))
            ref = cand if os.path.exists(cand) else None
        if not ref:
            log("  Referenz-Master %s nicht vorhanden – WBPP wählt selbst." % src)
    elif r.startswith("file:"):
        ref = r[5:]
        if not os.path.exists(ref):
            raise StackError("Referenzbild nicht gefunden: " + ref)
    # Kalibrierung (Header-Typ ggf. korrigieren)
    cache = os.path.join(settings.workdir, "cache", "calib_fixed")
    calib = dict(bias=fix_calibration(run.get("bias", []), BIAS, cache),
                 darks=fix_calibration(run.get("darks", []), DARK, cache),
                 darkflats=fix_calibration(run.get("darkflats", []), DARK, cache),
                 flats=fix_calibration(run.get("flats", []), FLAT, cache))
    if not calib["flats"]:
        log("  WARNUNG: keine Flats – Vignettierung und Staub bleiben im Bild.")
    script = settings.wbpp_script
    items = wbpp_items(plan, run, outdir, ref, calib)
    if len(",".join(items)) > MAX_CMDLINE:
        log("  Sehr viele Einzeldateien – verlinke Lights in einen Arbeitsordner …")
        staged = stage_lights(run["lights"], os.path.join(jobdir, "_lights", safe_file(name)), log)
        items = wbpp_items(plan, dict(run, lights=staged), outdir, ref, calib)
    runner = runner_factory()
    cmd = runner.start(script, items)
    log("  PixInsight gestartet")
    open(os.path.join(outdir, "command.txt"), "w", encoding="utf-8").write(cmd)
    t0 = time.time()
    last = None
    while True:
        code = runner.poll()
        stage, d, total = stage_progress(outdir, n)
        if (stage, d) != last:
            st.stage, st.done, st.total = stage, d, total
            st.message = "%s %d/%d · %s" % (stage, d, total, _elapsed(time.time() - t0))
            save()
            last = (stage, d)
        if code is not None:
            break
        if cancel.wait(3):
            runner.kill()
            raise Cancelled("abgebrochen")
    master = find_master(outdir)
    if not master:
        raise StackError("%s Kein Master entstanden – Details in %s" % (
            crash_text(code) if code else "WBPP lief durch, aber", os.path.join(outdir, "logs")))
    log("  Master: %s (%s)" % (os.path.basename(master), _elapsed(time.time() - t0)))
    finals = _finalize(plan, run, master, finaldir, settings, runner_factory, log)
    st.finals += finals
    st.runs[name] = dict(master=master, finals=finals, minutes=round((time.time() - t0) / 60, 1))
    if settings.cleanup_intermediate:
        for d in os.listdir(outdir):
            full = os.path.join(outdir, d)
            if os.path.isdir(full) and d not in ("master", "logs"):
                shutil.rmtree(full, ignore_errors=True)
        shutil.rmtree(os.path.join(jobdir, "_lights", safe_file(name)), ignore_errors=True)
        log("  Zwischendateien gelöscht")
    save()
    return master


def _finalize(plan, run, master, finaldir, settings, runner_factory, log) -> list[str]:
    tgt = safe_file(plan["target"])
    if run["mode"] != "extract":
        dst = os.path.join(finaldir, "%s_%s.xisf" % (tgt, safe_file(run["name"])))
        archive_existing(dst)
        shutil.copy2(master, dst)
        return [dst]
    items = ["in=" + pi_path(master)]
    outs = []
    for i, ch in enumerate(run["outputs"], 1):
        dst = os.path.join(finaldir, "%s_%s.xisf" % (tgt, safe_file(ch)))
        archive_existing(dst)
        items += ["out%d=%s" % (i, pi_path(dst)), "ch%d=%s" % (i, run["channel_map"][ch])]
        outs.append(dst)
    log("  Teile in %s auf …" % " + ".join(run["outputs"]))
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pjsr", "split.js")
    r = runner_factory()
    r.start(script, items)
    while r.poll() is None:
        time.sleep(1)
    if not all(os.path.exists(o) for o in outs):
        raise StackError("Aufteilen fehlgeschlagen – der Farb-Master liegt in " + master)
    return outs


def _elapsed(sec: float) -> str:
    m = int(sec // 60)
    return "%d:%02d h" % (m // 60, m % 60) if m >= 60 else "%d min" % m
