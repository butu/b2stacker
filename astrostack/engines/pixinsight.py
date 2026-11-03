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
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from .. import hotpix, logbook, stackqa, staging, steps, system, wbpplog
from ..fitsmeta import BIAS, DARK, FLAT

IMAGE_EXT = (".fits", ".fit", ".fts", ".xisf")
STAGES = [("calibrated", "calibrate"), ("cosmetized", "cosmetic"), ("debayered", "debayer"),
          ("registered", "register")]
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
    step: str = ""                    # Schlüssel aus steps.STACK
    step_started: float = 0.0
    eta_s: float | None = None
    run_index: int = 0
    runs_total: int = 0
    run_steps: list = field(default_factory=list)
    steps_done: list = field(default_factory=list)

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
    """Kalibrierframes so bereitstellen, dass WBPP sie richtig erkennt – WBPP richtet sich nur nach dem Header:
    * falscher IMAGETYP (z. B. NINA-Snapshot-Bias mit IMAGETYP=LIGHT) → korrigierte Kopie
    * fertige Masters ohne „Master“ im IMAGETYP (z. B. masterBias.fits mit IMAGETYP=BIAS) → „Master Bias“,
      sonst würde WBPP sie als einzelnes Rohbild behandeln."""
    import hashlib
    from astropy.io import fits
    want = {BIAS: "Bias Frame", DARK: "Dark Frame", FLAT: "Flat Frame"}[ftype]
    want_master = {BIAS: "Master Bias", DARK: "Master Dark", FLAT: "Master Flat"}[ftype]
    # eigener Ordner je Satz (Hash über die Pfade), damit dir= nie fremde Dateien mitnimmt
    cache_dir = os.path.join(cache_dir, ftype + "_" + hashlib.sha1(
        "|".join(sorted(os.path.abspath(p) for p in paths)).encode()).hexdigest()[:12])
    out = []
    for p in paths:
        low = p.lower()
        is_xisf = low.endswith(".xisf")
        try:
            hdr = _header(p)
        except Exception:  # noqa: BLE001
            out.append(p)
            continue
        cur = str(hdr.get("IMAGETYP", hdr.get("FRAME", ""))).lower()
        master = "master" in cur or os.path.basename(low).startswith("master")
        target = want_master if master else want
        if ftype.lower() in cur and (not master or "master" in cur):
            out.append(p)
            continue
        os.makedirs(cache_dir, exist_ok=True)
        dst = os.path.join(cache_dir, hashlib.sha1(os.path.abspath(p).encode()).hexdigest()[:8] + "_" + os.path.basename(p))
        if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(p):
            if is_xisf:
                from .. import stackqa, xisf
                img = xisf.read_image(p, 0)
                kw = stackqa.keywords_of(p)
                kw["IMAGETYP"] = target
                xisf.write_image(dst, img.astype("float32") if img.dtype.kind == "f" else img.astype("float32") / 65535.0,
                                 kw, color=img.shape[0] == 3)
            else:
                with fits.open(p) as hd:
                    hd[0].header["IMAGETYP"] = target
                    hd.writeto(dst, overwrite=True)
        out.append(dst)
    return out


def _header(p: str) -> dict:
    if p.lower().endswith(".xisf"):
        from .. import xisf
        return xisf.read_header(p)
    from astropy.io import fits
    return dict(fits.getheader(p))


def find_master(outdir: str) -> str | None:
    ms = sorted(glob.glob(os.path.join(outdir, "master", "masterLight*.xisf")), key=os.path.getmtime)
    if not ms:
        return None
    combined = [m for m in ms if not any(m.endswith(s) for s in ("_R.xisf", "_G.xisf", "_B.xisf"))]
    # Drizzle- / Autocrop-Varianten bevorzugt nicht nehmen: die "normale" Master-Datei
    plain = [m for m in combined if "drizzle" not in os.path.basename(m).lower() and "autocrop" not in os.path.basename(m).lower()]
    return (plain or combined or ms)[-1]


def stage_progress(outdir: str, n_lights: int) -> tuple[str, int, int]:
    """Fortschritt aus den Zwischenordnern von WBPP ablesen → (Schritt-Schlüssel, erledigt, gesamt)."""
    if glob.glob(os.path.join(outdir, "master", "masterLight*.xisf")):
        return "integrate", 1, 1
    last = ("calibrate", 0, n_lights)
    for sub, key in STAGES:
        d = os.path.join(outdir, sub)
        if os.path.isdir(d):
            n = sum(1 for _r, _d, fs in os.walk(d) for f in fs if f.lower().endswith(".xisf"))
            last = (key, min(n, n_lights), n_lights)
    if last[0] == "register" and last[1] >= n_lights * 0.9:
        return "integrate", 0, 0        # Registrieren fertig → Local Normalization + Integration
    return last


def crash_text(code: int) -> str:
    c = code & 0xFFFFFFFF
    if c in CRASH_CODES:
        return "PixInsight ist abgestürzt (0x%08X: %s)." % (c, CRASH_CODES[c])
    return "PixInsight beendet mit Code %d." % code


def safe_file(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "Ziel"


def archive_existing(path: str):  # (ältere Versionen; neue Dateinamen sind eindeutig)
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
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=0x08000000)
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


# ---------------------------------------------------------------- Flats je Nachtgruppe (Grouping Keyword)

GROUP_KEYWORD = "ASFLAT"


def _is_link(p: str) -> bool:
    if os.path.islink(p):
        return True
    isj = getattr(os.path, "isjunction", None)
    if isj and isj(p):
        return True
    if sys.platform == "win32":
        try:
            return bool(os.lstat(p).st_file_attributes & 0x400)       # FILE_ATTRIBUTE_REPARSE_POINT
        except (OSError, AttributeError):
            return False
    return False


def is_remote_path(path: str) -> bool:
    """UNC-Freigabe (\\\\server\\share) oder verbundenes Netzlaufwerk – dorthin darf keine Junction zeigen."""
    if str(path).startswith(("\\\\", "//")):
        return True
    p = os.path.abspath(path)
    if sys.platform == "win32":
        drive = os.path.splitdrive(p)[0]
        if drive and len(drive) == 2:
            try:
                import ctypes
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4     # DRIVE_REMOTE
            except Exception:  # noqa: BLE001
                return False
    return False


def _usable_dir_link(link: str) -> bool:
    try:
        os.listdir(link)
        return True
    except OSError:
        return False


def _drop_link(link: str) -> None:
    try:
        if _is_link(link) or os.path.isdir(link):
            (os.rmdir if sys.platform == "win32" or not os.path.islink(link) else os.unlink)(link)
        elif os.path.lexists(link):
            os.unlink(link)
    except OSError:
        pass


def link_dir(target: str, link: str) -> bool:
    """Verzeichnis-Verknüpfung ohne Kopie. True = verknüpft, False = nicht möglich (Aufrufer legt Kopien an).

    Windows: Junction nur für lokale Laufwerke – auf Netzwerkziele legt CreateJunction sonst eine kaputte
    Junction an (WinError 123 beim Zugriff). Für Netzwerkziele wird ein Symlink versucht (geht mit
    Entwicklermodus oder Adminrechten). Jede Verknüpfung wird vor der Verwendung geprüft."""
    os.makedirs(os.path.dirname(link), exist_ok=True)
    target = os.path.abspath(target)
    if sys.platform == "win32" and not is_remote_path(target):
        try:
            import _winapi
            _winapi.CreateJunction(target, os.path.abspath(link))
            if _usable_dir_link(link):
                return True
        except OSError:
            pass
        _drop_link(link)
    try:
        os.symlink(target, link, target_is_directory=True)
        if _usable_dir_link(link):
            return True
    except (OSError, NotImplementedError):
        pass
    _drop_link(link)
    return False


def place_files(paths: list[str], folder: str, log=None) -> None:
    """Fallback ohne Verknüpfung: nur die gewählten Dateien in einen eigenen Ordner legen (Hardlink, sonst Kopie).
    Der Ordner gehört b2stacker – remove_link_tree löscht dort nur diese Kopien, nie Originale."""
    os.makedirs(folder, exist_ok=True)
    copied = 0
    for p in paths:
        t = os.path.join(folder, os.path.basename(p))
        if os.path.exists(t):
            continue
        try:
            os.link(p, t)
        except OSError:
            try:
                shutil.copy2(p, t)
            except OSError as e:
                raise StackError("Datei konnte nicht in den Arbeitsordner kopiert werden: %s (%s). Liegt sie auf einem "
                                 "Netzlaufwerk, das gerade nicht erreichbar ist? Notlösung: Lights und Flats auf eine "
                                 "lokale Platte kopieren." % (p, e)) from e
            copied += 1
    if copied and log:
        log("  %d Dateien aus %s kopiert (Netzlaufwerk – Verknüpfung nicht möglich)" % (copied, os.path.dirname(paths[0])))


def remove_link_tree(root: str):
    """Ordnerbaum mit Verknüpfungen entfernen, OHNE je in die Ziele hineinzulöschen (Originaldaten!)."""
    if not os.path.isdir(root) and not _is_link(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for d in list(dirnames):
            p = os.path.join(dirpath, d)
            if _is_link(p):
                (os.rmdir if sys.platform == "win32" else os.unlink)(p)
                dirnames.remove(d)
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for f in filenames:                      # sollte leer sein – nur eigene Dateien
            os.unlink(os.path.join(dirpath, f))
        os.rmdir(dirpath)


def build_group_links(groups: list[dict], base: str, fix=lambda paths: paths, log=None) -> tuple[list[str], list[str]]:
    """Lights/Flats je Gruppe über …/ASFLAT_<n>/L<i>/datei erreichbar machen. → (lights, flats) neue Pfade.
    Lässt sich ein Ordner nicht verknüpfen (Netzlaufwerk ohne Symlink-Recht), werden nur die gewählten Dateien kopiert."""
    lights, flats = [], []
    for g in groups:
        gdir = os.path.join(base, "%s_%s" % (GROUP_KEYWORD, g["key"]))
        for kind, paths, out in (("L", g["lights"], lights), ("F", fix(g["flats"]), flats)):
            dirs = sorted({os.path.dirname(os.path.abspath(p)) for p in paths})
            for i, d in enumerate(dirs):
                link = os.path.join(gdir, "%s%d" % (kind, i))
                mine = [p for p in paths if os.path.dirname(os.path.abspath(p)) == d]
                if not os.path.exists(link) and not link_dir(d, link):
                    place_files(mine, link, log)
                out += [os.path.join(link, os.path.basename(p)) for p in paths if os.path.dirname(os.path.abspath(p)) == d]
    return lights, flats


class WbppTail:
    """Neue Zeilen aus dem WBPP-Protokoll mitlesen, damit das b2stacker-Protokoll während PixInsight nicht stillsteht.
    Nur aussagekräftige Zeilen, gedrosselt (PixInsight schreibt sehr viel)."""
    KEEP = ("error", "warning", "fehler", "**", "calibrat", "register", "integrat", "debayer", "normaliz", "measur",
            "weight", "master", "cosmetic", "drizzle", "* ", "frames", "completed", "processing")

    def __init__(self, logdir: str, limit: int = 8):
        self.logdir, self.limit, self.file, self.pos = logdir, limit, None, 0

    def new_lines(self) -> list[str]:
        try:
            f = wbpplog.newest_log(self.logdir)
        except Exception:  # noqa: BLE001
            return []
        if not f:
            return []
        if f != self.file:
            self.file, self.pos = f, 0
        try:
            with open(f, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read(256 * 1024)
                self.pos = fh.tell()
        except OSError:
            return []
        out = []
        for raw in chunk.decode("utf-8", "replace").splitlines():
            line = " ".join(raw.split())
            if len(line) < 4 or set(line) <= set("*=-_. "):
                continue
            if any(k in line.lower() for k in self.KEEP):
                out.append(line[:200])
        return out[-self.limit:]


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
                try:
                    shutil.copy2(p, t)
                except OSError as e:
                    raise StackError("Light konnte nicht in den Arbeitsordner kopiert werden: %s (%s). Notlösung: "
                                     "Lights auf eine lokale Platte kopieren und den Ordner neu analysieren." % (p, e)) from e
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
    base, k = stamp, 2
    while os.path.exists(os.path.join(pdir, "runs", stamp)):      # eindeutig, auch bei zwei Läufen pro Sekunde
        stamp, k = "%s-%d" % (base, k), k + 1
    jobdir = os.path.join(pdir, "runs", stamp)
    finaldir = os.path.join(pdir, "final")
    os.makedirs(jobdir)
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
    logbook.setup(settings.workdir)
    project_name = os.path.basename(pdir)
    t_job = time.time()
    logbook.event("stack_start", project=project_name, run_id=stamp, jobdir=jobdir,
                  runs=[dict(name=r["name"], mode=r["mode"], lights=r["lightCount"], hours=r.get("hours"), flats=len(r["flats"]),
                             bias=len(r["bias"]), darks=len(r["darks"]), reference=r.get("reference"),
                             flat_groups=len(r.get("flat_groups") or []) or None, calib_manual=r.get("calib_manual"))
                        for r in plan["runs"] if r.get("enabled", True)], options=plan.get("options"))
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
        st.runs_total = len(runs)
        for i, run in enumerate(runs):
            if cancel.is_set():
                raise Cancelled("abgebrochen")
            st.run, st.run_index = run["name"], i + 1
            st.run_steps = steps.stack_steps(run["mode"], run.get("is_osc", True))
            st.steps_done = []
            _set_step(st, "check", 0, len(run["lights"]))
            _save()
            masters[run["name"]] = _one_run(plan, run, settings, jobdir, masters, finaldir, st, _log, _save, cancel, runner_factory)
        st.state = "done"
        _record_stacks(plan, st, stamp, _log)
        _log("Fertig: " + ", ".join(os.path.basename(f) for f in st.finals))
        if (settings.open_results if open_results is None else open_results) and st.finals and real_pi:
            subprocess.Popen([settings.pixinsight] + st.finals)
    except Cancelled:
        st.state, st.error = "cancelled", "vom Benutzer abgebrochen"
        _log("Abgebrochen.")
    except (StackError, staging.StagingError) as e:
        st.state, st.error = "failed", str(e)
        _log("FEHLER: " + str(e))
        logbook.event("error", project=project_name, run_id=stamp, run=st.run, step=st.step, message=str(e))
    except Exception as e:  # noqa: BLE001 – alles melden statt still sterben
        st.state, st.error = "failed", "%s: %s" % (type(e).__name__, e)
        _log("FEHLER: %s: %s" % (type(e).__name__, e))
        logbook.error("Stacking-Fehler", project=project_name, run_id=stamp, run=st.run, step=st.step)
    finally:
        try:
            remove_link_tree(os.path.join(jobdir, "_grp"))       # nur Verknüpfungen, nie Originale
        except OSError as e:
            _log("Hinweis: Verknüpfungen nicht entfernt (%s)" % e)
        logbook.event("stack_end", project=project_name, run_id=stamp, state=st.state, error=st.error or None,
                      duration_s=round(time.time() - t_job, 1), finals=[os.path.basename(f) for f in st.finals])
        if keep:
            keep.__exit__(None, None, None)
        st.finished = datetime.datetime.now().isoformat(timespec="seconds")
        _save()
        logfile.close()
    return st


def _one_run(plan, run, settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory) -> str:
    name = run["name"]
    orig_run = run
    outdir = os.path.join(jobdir, safe_file(name))
    os.makedirs(outdir, exist_ok=True)
    n = len(run["lights"])
    if run.get("_retry") or run.get("_lock_retry"):
        log("=== Lauf %s wird wiederholt: %d Lights ===" % (name, n))
    else:
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
            from .. import project as _project
            ref = _project.latest_output(plan["workdir"], src)
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
    run = _cosmetic(plan, run, settings, jobdir, calib, st, log, save, cancel)
    script = settings.wbpp_script
    # Bereitstellen (Auswahl, Flat-Gruppen, Sonderzeichen, Länge) – zentral in staging.prepare.
    # Reste eines vorherigen Versuchs (Wiederholung ohne fehlregistrierte Frames) zuerst entfernen.
    remove_link_tree(os.path.join(jobdir, "_grp"))
    fg = [dict(g, flats=fix_calibration(g.get("flats", []), FLAT, cache)) for g in (run.get("flat_groups") or [])]
    lay = staging.prepare(dict(run, flat_groups=fg), calib, ref, jobdir, os.path.join(settings.workdir, "cache"),
                          MAX_CMDLINE, log=log,
                          cmd_len=lambda L: len(",".join(wbpp_items(plan, dict(run, lights=L.lights), outdir, L.reference, L.calib))))
    if lay.grouping:
        log("  Kamera-Stellung wechselt zwischen den Nächten → %d Gruppen mit eigenen Flats:" % len(lay.groups))
        for g in lay.groups:
            log("    Gruppe %s: Nächte %s · %d Lights · Flats %s (%d)" % (g.key, ", ".join(g.nights), len(g.lights),
                                                                       g.flat_set or "keine", len(g.flats)))
        run = dict(run, wbpp=dict(run.get("wbpp") or {}, groupingKeywordsEnabled="true", keywords="%s pre" % GROUP_KEYWORD))
        logbook.event("flat_groups", run=name, groups=[dict(key=g.key, nights=g.nights, lights=len(g.lights),
                                                             flats=len(g.flats), flat_set=g.flat_set) for g in lay.groups])
    for note in lay.notes:
        log("  " + note)
    log("  " + lay.summary())
    logbook.event("staging", run=name, grouping=lay.grouping, lights=len(lay.lights), **lay.stats)
    if len(lay.lights) != n:                       # Invariante: genau die gewählten Lights
        raise StackError("Interner Fehler: %d Lights bereitgestellt, %d gewählt." % (len(lay.lights), n))
    run = dict(run, lights=lay.lights)
    calib, ref = lay.calib, lay.reference
    items = wbpp_items(plan, run, outdir, ref, calib)
    runner = runner_factory()
    cmd = runner.start(script, items)
    log("  PixInsight gestartet")
    open(os.path.join(outdir, "command.txt"), "w", encoding="utf-8").write(cmd)
    t0 = time.time()
    timing = steps.load_timing(settings.workdir)
    last = None
    tail = WbppTail(os.path.join(outdir, "logs"))
    while True:
        code = runner.poll()
        key, d, total = stage_progress(outdir, n)
        if key != st.step:
            _finish_step(st, settings.workdir, n)
            _set_step(st, key, d, total)
            log("  → %s" % steps.BY_KEY[key]["title"])
        for line in tail.new_lines():
            log("    PI: " + line)
        st.done, st.total = d, total
        st.eta_s = _eta(st, n, timing)
        st.stage = steps.BY_KEY[key]["title"]
        st.message = "%s · Lauf läuft seit %s" % (st.stage, _elapsed(time.time() - t0))
        save()                                  # alle 3 s: Fortschritt + Restzeit aktuell halten
        last = (key, d)
        if code is not None:
            break
        if cancel.wait(3):
            runner.kill()
            raise Cancelled("abgebrochen")
    _finish_step(st, settings.workdir, n)
    logbook.event("pixinsight_exit", run=name, code=code, code_hex="0x%08X" % (code & 0xFFFFFFFF) if code else "0",
                  duration_s=round(time.time() - t0, 1), command=cmd[:3000], wbpp_logs=os.path.join(outdir, "logs"),
                  master=find_master(outdir))
    master = find_master(outdir)
    if not master:
        raise StackError("%s Kein Master entstanden – Details in %s" % (
            crash_text(code) if code else "WBPP lief durch, aber", os.path.join(outdir, "logs")))
    log("  Master: %s (%s)" % (os.path.basename(master), _elapsed(time.time() - t0)))
    notes = list(orig_run.get("_notes") or [])
    if run.get("_cc_pixinsight"):
        cc = cc_check(outdir)
        logbook.event("cosmetic", run=name, engine="pixinsight", **{k: v for k, v in cc.items() if k != "log"})
        if cc["ok"]:
            log("  Hotpixel-Korrektur (PixInsight CosmeticCorrection) ausgeführt")
        else:
            log("  WARNUNG: PixInsight hat die Hotpixel-Korrektur nicht ausgeführt (%s) – Stack wird mit "
                "b2stacker-eigener Hotpixel-Entfernung neu gerechnet." % (
                    "Parameter nicht übernommen" if cc["applied"] else "Pipeline-Script nicht aktiv"))
            _discard(master)
            remove_link_tree(os.path.join(jobdir, "_grp", safe_file(name)))
            return _one_run(plan, dict(orig_run, _force_cosmetic=True,
                                       _notes=notes + ["PixInsight-Hotpixel-Korrektur nicht verfügbar – b2stacker-Ersatz verwendet."]),
                            settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory)
    retry = _check_registration(orig_run, outdir, master, notes, log)
    if retry is not None:
        remove_link_tree(os.path.join(jobdir, "_grp", safe_file(name)))
        return _one_run(plan, retry, settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory)
    _set_step(st, "finalize", 0, 0)
    save()
    previewdir = os.path.join(plan["workdir"], "stacks")
    outputs = stackqa.process_master(
        master, run, plan["target"], finaldir, previewdir, datetime.date.today().isoformat(),
        pixscale=plan.get("pixscale"), fix_fringes=plan.get("options", {}).get("fix_fringes", True), log=log)
    st.steps_done.append("finalize")
    _set_step(st, "quality", 0, 0)
    save()
    for o in outputs:
        k = o["kpi"]
        log("  Qualität %s: Rauschen %.2f ADU · FWHM %s · %d Sterne · Exz. %s · Verlauf %.1f %%" % (
            o["name"], k["noise"], ('%.2f"' % k["fwhm_as"]) if k.get("fwhm_as") else ("%.2f px" % k["fwhm_px"] if k.get("fwhm_px") else "–"),
            k["stars"], k["ecc"] if k["ecc"] is not None else "–", k["gradient_pct"]))
    st.steps_done.append("quality")
    finals = [o["file"] for o in outputs]
    st.finals += finals
    logbook.event("stack_run", project=os.path.basename(plan["workdir"]), run=name, lights=n, hours=run.get("hours"),
                  minutes=round((time.time() - t0) / 60, 1),
                  outputs=[dict(name=o["name"], file=os.path.basename(o["file"]), kpi=o["kpi"]) for o in outputs])
    st.runs[name] = dict(master=master, finals=finals, outputs=outputs, minutes=round((time.time() - t0) / 60, 1),
                         lights=n, hours=run.get("hours"), nights=run.get("nights"), filter=run.get("filter"),
                         mode=run.get("mode"), flats=len(run.get("flats", [])), bias=len(run.get("bias", [])),
                         darks=len(run.get("darks", [])), reference=run.get("reference"), notes=notes,
                         excluded=orig_run.get("_excluded") or [])
    if settings.cleanup_intermediate:
        for d in os.listdir(outdir):
            full = os.path.join(outdir, d)
            if os.path.isdir(full) and d not in ("master", "logs"):
                shutil.rmtree(full, ignore_errors=True)
        shutil.rmtree(os.path.join(jobdir, "_lights", safe_file(name)), ignore_errors=True)
        shutil.rmtree(os.path.join(jobdir, "_cosmetic", safe_file(name)), ignore_errors=True)
        log("  Zwischendateien gelöscht")
    remove_link_tree(os.path.join(jobdir, "_grp", safe_file(name)))
    save()
    return master


CC_ICON = "b2stacker_CC"
CC_HOT_SIGMA = 3.0
CC_COLD_SIGMA = 3.0


def cc_builder_script(workdir: str, hot: float = CC_HOT_SIGMA, cold: float = CC_COLD_SIGMA) -> str:
    """WBPP-Pipeline-Builder-Script (PixInsight CosmeticCorrection, Auto Detect) in den Cache schreiben → Pfad."""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pjsr", "cc_builder.js")
    code = open(src, encoding="utf-8").read().replace("__HOT__", "%.2f" % hot).replace("__COLD__", "%.2f" % cold)
    d = os.path.join(workdir, "cache", "pjsr")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "cc_builder_%s.js" % hashlib.sha1(code.encode("utf-8")).hexdigest()[:8])
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(code)
    return path


def cc_check(outdir: str) -> dict:
    """Hat WBPP unsere CosmeticCorrection wirklich ausgeführt? (Protokoll des letzten Laufs)"""
    log = wbpplog.newest_log(os.path.join(outdir, "logs"))
    text = ""
    if log:
        try:
            text = open(log, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
    applied = ("applying %s process icon" % CC_ICON) in text
    params_ok = "hotAutoCheck = true" in text and "useAutoDetect = true" in text
    done = "End cosmetic correction of light frames" in text
    return {"log": log, "applied": applied, "params_ok": params_ok, "done": done, "ok": applied and params_ok and done}


def _cosmetic(plan, run, settings, jobdir, calib, st, log, save, cancel) -> dict:
    """Ohne Darks: Hotpixel entfernen.

    Normalfall: PixInsight selbst (CosmeticCorrection, Auto Detect) – per WBPP-Kommandozeile
    usePipelineBuilderScript/pipelineBuilderScriptFile eingeschleust, läuft als WBPP-Schritt nach dem Kalibrieren.
    Notfall (_force_cosmetic, z. B. wenn PixInsight die Bilder trotzdem am Sensor ausgerichtet hat): eigene
    Bereinigung der Rohbilder in Python vor WBPP."""
    if not run.get("_force_cosmetic") and (calib.get("darks") or plan.get("options", {}).get("hot_pixels", True) is False):
        return run
    if not run.get("_force_cosmetic"):
        script = cc_builder_script(settings.workdir)
        st.run_steps = steps.stack_steps(run["mode"], run.get("is_osc", True), cosmetic="after")
        log("  Keine Darks → PixInsight entfernt Hotpixel (CosmeticCorrection, Auto Detect) nach dem Kalibrieren")
        wb = dict(run.get("wbpp") or {}, usePipelineBuilderScript="true", pipelineBuilderScriptFile=pi_path(script))
        wb.setdefault("hotPixelFilterRadius", "2")      # zusätzliche Sicherheit bei der Sternerkennung
        return dict(run, wbpp=wb, _cc_pixinsight=True)
    fits_l = [p for p in run["lights"] if p.lower().endswith((".fits", ".fit", ".fts"))]
    if not fits_l:
        return run
    st.run_steps = steps.stack_steps(run["mode"], run.get("is_osc", True), cosmetic="before")
    _finish_step(st, settings.workdir, len(run["lights"]))
    _set_step(st, "cosmetic", 0, len(fits_l))
    save()
    log("  Hotpixel werden vor dem Stacken von b2stacker entfernt (%d Lights) …" % len(fits_l))
    cache = os.path.join(jobdir, "_cosmetic", safe_file(run["name"]))
    last = [0.0]

    def prog(d, t):
        st.done, st.total = d, t
        if time.time() - last[0] > 2:
            last[0] = time.time()
            st.eta_s = _eta(st, len(fits_l), steps.load_timing(settings.workdir))
            save()

    mapping, stats = hotpix.clean_lights(fits_l, cache, is_cfa=None if run.get("is_osc", True) else False,
                                         workers=max(1, min(4, (os.cpu_count() or 2) // 2)), log=log,
                                         progress=prog, cancel=cancel)
    if cancel.is_set():
        raise Cancelled("abgebrochen")
    if stats:
        px = [s["pixels"] for s in stats]
        log("  Hotpixel ersetzt: im Mittel %d pro Bild (%d–%d)" % (sum(px) / len(px), min(px), max(px)))
        logbook.event("cosmetic", run=run["name"], frames=len(stats), mean_pixels=round(sum(px) / len(px)),
                      min_pixels=min(px), max_pixels=max(px))
    _finish_step(st, settings.workdir, len(fits_l))
    m = lambda ps: [mapping.get(p, p) for p in ps]                     # noqa: E731
    fg = [dict(g, lights=m(g.get("lights", []))) for g in run.get("flat_groups") or []]
    wb = dict(run.get("wbpp") or {})
    wb.setdefault("hotPixelFilterRadius", "2")          # zusätzliche Sicherheit bei der Sternerkennung
    return dict(run, lights=m(run["lights"]), flat_groups=fg or run.get("flat_groups"), wbpp=wb)


def _check_registration(run: dict, outdir: str, master: str, notes: list, log) -> dict | None:
    """Registrierung im WBPP-Protokoll prüfen. Falsch registrierte Frames → Lauf ohne sie wiederholen (einmal).

    Gibt den Lauf für die Wiederholung zurück oder None (alles gut bzw. keine Wiederholung möglich)."""
    try:
        chk = wbpplog.check_registration(outdir, wbpplog.light_info(run["lights"]))
    except Exception as e:  # noqa: BLE001 – Prüfung darf das Ergebnis nicht gefährden
        log("  Hinweis: Registrierung nicht geprüft (%s)" % e)
        return None
    bad = chk["bad"]
    lock = [b for b in bad if b.get("lock")]
    logbook.event("registration_check", run=run["name"], frames=chk["frames"], retry=bool(run.get("_retry")),
                  lock_retry=bool(run.get("_lock_retry")), locked=len(lock), reference=chk.get("reference"),
                  bad=[dict(file=os.path.basename(b["file"]), why=b["why"], scale=b.get("scale"), lock=b.get("lock"),
                            inliers=b.get("inliers"), quality=b.get("quality"), tx=b.get("tx"), ty=b.get("ty"),
                            rotation=b.get("rotation")) for b in bad])
    if not bad:
        if chk["frames"]:
            log("  Registrierung geprüft: alle %d Frames plausibel" % chk["frames"])
        return None
    if lock:
        log("  WARNUNG: %d Bilder aus anderen Nächten/von der anderen Meridianseite liegen „perfekt“ (0,0 px) auf der "
            "Referenz – PixInsight hat sie an Hotpixeln (Sensor) statt an den Sternen ausgerichtet." % len(lock))
        _discard(master)
        if not run.get("_lock_retry"):
            log("  → Neuer Versuch: Hotpixel werden entfernt und die Sternerkennung filtert stärker.")
            wb = dict(run.get("wbpp") or {}, hotPixelFilterRadius="3", noiseReductionFilterRadius="2")
            wb.pop("usePipelineBuilderScript", None)
            wb.pop("pipelineBuilderScriptFile", None)
            return dict(run, _lock_retry=True, _force_cosmetic=True, wbpp=wb,
                        _notes=notes + ["Erster Versuch an Hotpixeln ausgerichtet – mit Hotpixel-Entfernung neu gerechnet."])
        raise StackError("PixInsight hat die Bilder auch im zweiten Versuch an Sensor-Mustern statt an den Sternen "
                         "ausgerichtet (%d Bilder). Der Stack wäre unbrauchbar (Geistersterne). Abhilfe: passende Darks "
                         "aufnehmen und im Plan wählen, beim Aufnehmen dithern. Details: %s" % (len(lock), chk.get("log")))
    for b in bad:
        log("  WARNUNG: %s falsch registriert (%s)" % (os.path.basename(b["file"]), ", ".join(b["why"])))
    drop = wbpplog.map_to_lights(bad, run["lights"])
    keep = [p for p in run["lights"] if p not in drop]
    if run.get("_retry") or not drop or len(keep) < 3:
        notes.append("%d Frame(s) möglicherweise falsch registriert – Stack auf Geistersterne prüfen: %s" % (
            len(bad), ", ".join(os.path.basename(b["file"]) for b in bad)))
        return None
    log("  → Diese %d Frame(s) würden Geistersterne erzeugen. Stack wird ohne sie neu gerechnet." % len(drop))
    _discard(master)
    old_n = len(run["lights"])
    dropped = set(drop)
    fg = []
    for g in run.get("flat_groups") or []:
        fg.append(dict(g, lights=[p for p in g.get("lights", []) if p not in dropped]))
    notes = notes + ["%d Frame(s) ausgelassen, weil PixInsight sie falsch registriert hat: %s" % (
        len(drop), ", ".join(os.path.basename(p) for p in drop))]
    return dict(run, lights=keep, lightCount=len(keep), flat_groups=fg or run.get("flat_groups"),
                hours=round((run.get("hours") or 0) * len(keep) / old_n, 2) if run.get("hours") else run.get("hours"),
                _retry=True, _notes=notes, _excluded=list(run.get("_excluded") or []) + drop)


def _discard(master: str):
    """Verworfenen Master umbenennen (darf nicht als Ergebnis gefunden werden)."""
    try:
        os.replace(master, master + ".verworfen")
    except OSError:
        pass


def _set_step(st: Status, key: str, done: int, total: int):
    st.step, st.done, st.total = key, done, total
    st.step_started = time.time()
    st.stage = steps.BY_KEY.get(key, {}).get("title", key)
    st.eta_s = None


def _finish_step(st: Status, workdir: str, n: int):
    if st.step and st.step not in st.steps_done:
        st.steps_done.append(st.step)
        if st.step_started:
            dur = time.time() - st.step_started
            if st.step != "check":
                steps.learn_timing(workdir, st.step, dur, n)
            logbook.event("stack_step", run=st.run, step=st.step, duration_s=round(dur, 1), frames=n)


def _eta(st: Status, n: int, timing: dict) -> float | None:
    """Restzeit des aktuellen Schritts: aus dem Tempo (gezählte Schritte) oder aus Erfahrungswerten."""
    el = time.time() - st.step_started
    if st.total and st.done >= 2 and el > 20:
        return max(0.0, (st.total - st.done) * el / st.done)
    per = timing.get(st.step)
    if per:
        return max(0.0, per * n - el)
    return None


def _record_stacks(plan: dict, st: Status, stamp: str, log):
    """Fertige Stacks in die Projekt-Historie (stacks.json) eintragen."""
    from .. import project as _project
    for name, r in st.runs.items():
        try:
            _project.add_stack(plan["workdir"], dict(
                id="%s-%s" % (stamp, safe_file(name)), run_id=stamp, date=datetime.datetime.now().isoformat(timespec="seconds"),
                run=name, filter=r.get("filter"), mode=r.get("mode"), lights=r.get("lights"), hours=r.get("hours"),
                nights=r.get("nights"), minutes=r.get("minutes"), flats=r.get("flats"), bias=r.get("bias"),
                darks=r.get("darks"), reference=r.get("reference"), options=plan.get("options", {}),
                outputs=r.get("outputs", []), notes=r.get("notes") or [],
                excluded=[os.path.basename(p) for p in r.get("excluded") or []]))
        except Exception as e:  # noqa: BLE001 – Historie darf das Ergebnis nicht gefährden
            log("  Hinweis: Historie nicht gespeichert (%s)" % e)


def _elapsed(sec: float) -> str:
    m = int(sec // 60)
    return "%d:%02d h" % (m // 60, m % 60) if m >= 60 else "%d min" % m
