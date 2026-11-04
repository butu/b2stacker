"""Siril als Stacking-Engine: Erkennung der siril-cli (Linux, Windows, macOS, Flatpak).

Detection, version check and the Siril run (.ssf script).
"""
from __future__ import annotations

import datetime
import glob
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

from .. import logbook, stackqa, staging, steps, system, wbpplog
from ..fitsmeta import BIAS, DARK, FLAT
from ..system import _decode
from . import pixinsight as pi
from .pixinsight import Cancelled, Status, StackError

MIN_VERSION = (1, 4, 0)        # die Skripte nutzen „requires 1.4.0“ und -weight=wfwhm
FLATPAK_ID = "org.siril.Siril"
FLATPAK_LABEL = "flatpak:" + FLATPAK_ID
FLATPAK_DIRS = ("/var/lib/flatpak", os.path.expanduser("~/.local/share/flatpak"))
_NO_WINDOW = 0x08000000
_VERSION_CACHE: dict[tuple, str] = {}       # nur erfolgreiche Abfragen; Fehlschläge werden erneut geprüft

if sys.platform == "win32":
    SEARCH_PATHS = [r"C:\Program Files\Siril\bin\siril-cli.exe"]
elif sys.platform == "darwin":
    SEARCH_PATHS = ["/Applications/Siril.app/Contents/MacOS/siril-cli"]
else:
    SEARCH_PATHS = ["/usr/bin/siril-cli", "/usr/local/bin/siril-cli"]


@dataclass(frozen=True)
class Install:
    cli: tuple            # Aufruf-Präfix für die Kommandozeile (Flatpak = mehrere Wörter)
    gui: tuple | None     # Aufruf zum Öffnen von Ergebnissen in der Siril-Oberfläche
    label: str            # Anzeige und Wert für die Einstellung
    flatpak: bool = False

    def command(self, dirs, args: list[str]) -> list[str]:
        """Vollständiger Aufruf. Flatpak sieht nur freigegebene Ordner → jeder Arbeitsordner einzeln."""
        if not self.flatpak:
            return list(self.cli) + list(args)
        return ["flatpak", "run", "--env=LC_ALL=C", "--env=LANGUAGE=en"] + _grants(dirs) + \
            ["--command=siril-cli", FLATPAK_ID] + list(args)

    def gui_command(self, dirs, files: list[str]) -> list[str] | None:
        """Aufruf der Siril-Oberfläche mit Dateien. None = keine Oberfläche gefunden (nur siril-cli)."""
        if self.gui is None:
            return None
        if not self.flatpak:
            return list(self.gui) + list(files)
        return ["flatpak", "run"] + _grants(dirs) + [FLATPAK_ID] + list(files)


def _grants(dirs) -> list[str]:
    return ["--filesystem=" + d for d in sorted({os.path.abspath(d) for d in dirs if d})]


def _flatpak_install() -> Install | None:
    if not sys.platform.startswith("linux") or shutil.which("flatpak") is None:
        return None
    if not any(os.path.isdir(os.path.join(d, "app", FLATPAK_ID)) for d in FLATPAK_DIRS):
        return None
    return Install(cli=("flatpak", "run", "--command=siril-cli", FLATPAK_ID),
                   gui=("flatpak", "run", FLATPAK_ID), label=FLATPAK_LABEL, flatpak=True)


def _from_binary(path: str) -> Install | None:
    """Pfad zu siril-cli – oder zum Siril-Programm, dann liegt die CLI daneben."""
    base = os.path.basename(path).lower()
    d = os.path.dirname(path)
    if base.startswith("siril") and not base.startswith("siril-cli"):
        cli = os.path.join(d, "siril-cli" + (".exe" if base.endswith(".exe") else ""))
        if not os.path.isfile(cli):
            return None
        return Install(cli=(cli,), gui=(path,), label=cli)
    if not os.path.isfile(path):
        return None
    gui = os.path.join(d, base.replace("siril-cli", "siril", 1))
    return Install(cli=(path,), gui=(gui,) if os.path.isfile(gui) else None, label=path)


def locate(setting: str = "") -> Install | None:
    """Einstellung auswerten; leer = automatische Erkennung. None = nicht gefunden."""
    v = (setting or "").strip().strip('"')
    if v == FLATPAK_LABEL:
        return _flatpak_install()
    if not v:
        found = shutil.which("siril-cli")
        if found:
            return _from_binary(found)
        for p in SEARCH_PATHS:
            if os.path.isfile(p):
                return _from_binary(p)
        return _flatpak_install()
    if os.path.isdir(v):
        for cand in (os.path.join(v, "siril-cli"), os.path.join(v, "bin", "siril-cli"),
                     os.path.join(v, "siril-cli.exe"), os.path.join(v, "bin", "siril-cli.exe")):
            if os.path.isfile(cand):
                return _from_binary(cand)
        return None
    return _from_binary(v)


def _parse(version: str) -> tuple:
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def version_of(inst: Install) -> str | None:
    """`siril-cli --version` → „1.4.4“. Timeout, damit eine hängende Installation die Oberfläche nicht blockiert."""
    key = inst.cli
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    try:
        out = subprocess.run(list(inst.cli) + ["--version"], capture_output=True, timeout=20,
                             creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+(?:\.\d+)?", _decode(out.stdout + out.stderr))
    if not m:
        return None
    _VERSION_CACHE[key] = m.group(0)
    return m.group(0)


def open_command(setting: str, files: list[str]) -> list[str]:
    """Befehl, um Ergebnisse in der Siril-Oberfläche zu öffnen. Fehlt sie, ist das eine klare Meldung."""
    inst = locate(setting)
    if inst is None:
        raise FileNotFoundError("Siril nicht gefunden – Pfad in den Einstellungen prüfen.")
    cmd = inst.gui_command([os.path.dirname(os.path.abspath(f)) for f in files], files)
    if cmd is None:
        raise FileNotFoundError("Siril-Oberfläche nicht gefunden – im Einstellungsfeld das Siril-Programm "
                                "(nicht nur siril-cli) eintragen.")
    return cmd


def check(setting: str = "") -> dict:
    """Ist eine passende Siril-Installation da? → Felder für Oberfläche und Blocker-Meldung."""
    v = (setting or "").strip()
    inst = locate(v)
    if inst is None:
        msg = ("Pfad nicht gefunden – siril-cli prüfen oder „flatpak:%s“ eintragen." % FLATPAK_ID) if v else \
            "Siril nicht gefunden – siril-cli im PATH oder im Standardordner nötig (Flatpak: %s)." % FLATPAK_ID
        return dict(path=v, ok=False, found=False, version=None, cmd=None, message=msg)
    ver = version_of(inst)
    if ver is None:
        return dict(path=v, ok=False, found=True, version=None, cmd=inst.label,
                    message="Siril gefunden, aber „siril-cli --version“ antwortet nicht – Installation prüfen.")
    if _parse(ver) < MIN_VERSION:
        return dict(path=v, ok=False, found=True, version=ver, cmd=inst.label,
                    message="Siril %s ist zu alt – mindestens %s nötig." % (ver, ".".join(map(str, MIN_VERSION))))
    return dict(path=v, ok=True, found=True, version=ver, cmd=inst.label, message="Siril %s gefunden." % ver)


# ------------------------------------------------------------------ Skript

_RUNNING = re.compile(r"Running command: (\w+)")
# Siril-Meldungen, die bei erfolgreichen Läufen auftreten (Ursache: Aufräumen von Temp-Dateien/Sequenz-Scan)
NOISE = ("g_unlink", "removed gpid", "reading sequence failed", "could not load first image", "cgroups",
         "memory controller", "venv", "found python executable", "color management", "jpeg icc")
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _p(path: str) -> str:
    """Pfad für das Skript: absolut, Vorwärtsschrägstriche (auch unter Windows von Siril akzeptiert)."""
    return os.path.abspath(path).replace("\\", "/")


def _quote(path: str) -> str:
    if '"' in path:
        raise StackError("Pfad enthält Anführungszeichen – Siril kann ihn nicht übergeben: " + path)
    return '"%s"' % _p(path)


def _kv(key: str, path: str) -> str:
    """Option mit Pfad als ein quotiertes Token („-out=…“). Quotes INNERHALB des Werts übernimmt Siril nicht."""
    token = "%s=%s" % (key, _p(path))
    if '"' in token:
        raise StackError("Pfad enthält Anführungszeichen – Siril kann ihn nicht übergeben: " + token)
    return '"%s"' % token


def _stack_seq(n_groups: int) -> str:
    """Name der Sequenz, die registriert und gestapelt wird (ohne Merge: Lichter der einzigen Gruppe)."""
    return "lights_" if n_groups > 1 else "pp_light_"


# Siril-Befehlsbausteine; planview.py zeigt dieselben Werte an.
REJECTION = "rej w 3 3"              # winsorisiert, 3 σ / 3 σ
REG_TRANSFORM = "-transf=homography"
INTERP = "lanczos4"


def build_script(spec: dict) -> str:
    """Skript für einen Lauf. spec: outdir, lights (Ordner), osc, bias/dark/darkflat/flat →
    dict(raw=Ordner mit Präfix-Sequenz|None, prefix, master=Pfad|None), cosmetic (bool), sigma (float),
    groups (optional): [dict(sub, lights=Ordner, flat=…)] – je Gruppe eigener Master-Flat, dann merge."""
    out = spec["outdir"]
    L = ["requires 1.4.0", "setext fit"]
    if spec.get("osc") and spec.get("bayer"):              # Header fehlt/widerspricht → Muster festlegen
        L += ["set debayer.use_bayer_header=false", "set debayer.pattern=%d" % BAYER_INDEX[spec["bayer"]]]
    masters = {}

    def master_of(kind: str, s: dict, parent: str | None, sub: str = "") -> str | None:
        if s.get("master"):
            return _p(s["master"])
        if not s.get("raw"):
            return None
        seq = os.path.join(out, sub, "seq_" + kind)
        L.extend(["cd " + _quote(s["raw"]), "convert %s %s" % (s["prefix"], _kv("-out", seq)), "cd " + _quote(seq)])
        # Siril hängt an konvertierte Sequenzen „_“ an: Sequenz „bias“ → Befehle nutzen „bias_“
        if kind == "flat":
            opt = (" " + _kv("-dark" if parent[0] == "dark" else "-bias", parent[1])) if parent else ""
            L.append("calibrate %s_%s -prefix=pp_" % (s["prefix"], opt))
            L.append("stack pp_%s_ %s -norm=mul -output_norm -32b -out=master_%s" % (s["prefix"], REJECTION, kind))
        else:
            L.append("stack %s_ %s -nonorm -32b -out=master_%s" % (s["prefix"], REJECTION, kind))
        return _p(os.path.join(seq, "master_%s.fit" % kind))

    for kind in ("bias", "dark", "darkflat"):
        if spec.get(kind):
            masters[kind] = master_of(kind, spec[kind], None)
    if masters.get("darkflat"):
        flat_dark = ("dark", masters["darkflat"])          # prefer dark-flats, else bias
    elif masters.get("bias"):
        flat_dark = ("bias", masters["bias"])
    else:
        flat_dark = None
    common = []
    if masters.get("bias"):
        common.append(_kv("-bias", masters["bias"]))
    if masters.get("dark"):
        common.append(_kv("-dark", masters["dark"]))
    if masters.get("dark") and spec.get("cosmetic", True):
        common.append("-cc=dark %.1f %.1f" % (pi.CC_COLD_SIGMA, pi.CC_HOT_SIGMA))
    if spec.get("osc"):
        common += ["-cfa", "-equalize_cfa", "-debayer"]
    groups = spec.get("groups") or [dict(sub="", lights=spec["lights"], flat=spec.get("flat"))]
    seqs = []
    for g in groups:
        sub = g.get("sub", "")
        cal = list(common)
        if g.get("flat"):
            cal.append(_kv("-flat", master_of("flat", g["flat"], flat_dark, sub)))
        seq_light = os.path.join(out, sub, "seq_light")
        # originals are linked or copied, never modified
        L.extend(["cd " + _quote(g["lights"]), "convert light %s" % _kv("-out", seq_light), "cd " + _quote(seq_light)])
        L.append("calibrate light_ %s -prefix=pp_" % " ".join(cal))
        seqs.append(_p(os.path.join(seq_light, "pp_light_")))
    findstar = dict(spec.get("findstar") or {})
    if spec.get("sigma"):
        findstar.setdefault("sigma", spec["sigma"])
    sigma_line = "setfindstar " + " ".join(
        "-%s=%s" % (k, ("%.2f" % v) if k == "sigma" else v) for k, v in sorted(findstar.items())) if findstar else ""
    ref = spec.get("reference")
    if ref:                                                # Referenzframes als eigene Sequenz (nicht kalibriert)
        L.extend(["cd " + _quote(ref), "convert ref %s" % _kv("-out", os.path.join(out, "seq_ref")), ])
        seqs.append(_p(os.path.join(out, "seq_ref", "ref_")))
    seq = _stack_seq(len(seqs))
    framing = " -framing=min" if spec.get("framing") == "min" else ""     # autocrop: common area only
    if len(seqs) > 1:                                      # Gruppen (+ Referenz) → eine Sequenz in dieser Reihenfolge
        L.append("cd " + _quote(os.path.join(out, "seq_light")))
        L.append("merge %s lights" % " ".join(_quote(s) for s in seqs))
    if sigma_line:
        L.append(sigma_line)
    if ref:
        n = sum(g.get("n", 0) for g in groups)
        # Reference = first reference frame; no -2pass, which would let Siril pick its own reference
        L.append("setref %s %d" % (seq, n + 1))
        L.append("register %s -minpairs=10 %s -maxstars=2000" % (seq, REG_TRANSFORM))
        L.append("seqapplyreg %s -interp=%s%s -prefix=r_" % (seq, INTERP, framing))
        L.append("unselect r_%s %d %d" % (seq, n + 1, n + 2))
    else:
        L.append("register %s -2pass -minpairs=10 %s -maxstars=2000" % (seq, REG_TRANSFORM))
        L.append("seqapplyreg %s -interp=%s%s -prefix=r_" % (seq, INTERP, framing))
    L.append("stack r_%s %s -norm=addscale -weight=%s -output_norm -32b%s%s%s -out=result"
             % (seq, REJECTION, spec.get("weight", "wfwhm"), " -rgb_equal" if spec.get("osc") else "",
                " -filter-included" if ref else "", " -rejmaps" if spec.get("rejmaps") else ""))
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ Lauf

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def clean_line(raw: str) -> str:
    """Eine Ausgabezeile von Terminal-Steuerzeichen befreien. Fortschritt überschreibt sich mit „\\r“ bzw. ANSI-Codes
    (z. B. Zeilen-Löschen + Cursor hoch) → nur der letzte sichtbare Stand bleibt; „Tprogress:“ wird zu „progress:“."""
    parts = [p for p in raw.rstrip("\r\n").split("\r") if p.strip()]
    line = _ANSI.sub("", parts[-1] if parts else "").replace("\x1b", "")
    line = re.sub(r"^(?:\[\d*[AKJ])+", "", line)            # ESC verloren gegangen: „[A[2K“ am Zeilenanfang
    i = line.find("progress:")
    if i > 0 and not line[:i].strip(" [T\t"):              # Fortschritt mit Steuerrest davor (z. B. „[A[2KT“)
        line = line[i:]
    return line.strip()


class SirilRunner:
    """Startet siril-cli mit dem Skript und sammelt die Ausgabe (Fehlerausgabe in derselben Leitung)."""

    def __init__(self, inst: Install, script: str, workdir: str, dirs):
        # Siril löst den Skriptpfad relativ zum (mit -d gewechselten) Arbeitsordner auf → absolut übergeben
        self.args = inst.command(dirs, ["-d", os.path.abspath(workdir), "-s", os.path.abspath(script)])
        self.proc = None
        self.thread = None
        self._out: list[str] = []
        self._lock = threading.Lock()

    def start(self) -> str:
        env = dict(os.environ, LC_ALL="C", LANG="C", LANGUAGE="en")       # Siril-Meldungen auf Englisch
        # Eigene Prozessgruppe: Abbruch beendet auch Kindprozesse (Flatpak-Sandbox, siril-cli)
        self.proc = subprocess.Popen(self.args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
                                     errors="replace", creationflags=_NO_WINDOW if sys.platform == "win32" else 0,
                                     start_new_session=sys.platform != "win32")
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        return " ".join(self.args)

    def _pump(self):
        for line in self.proc.stdout:
            with self._lock:
                self._out.append(clean_line(line))

    def drain(self) -> list[str]:
        with self._lock:
            out, self._out = self._out, []
        return out

    def poll(self):
        return self.proc.poll() if self.proc else None

    def kill(self):
        if not self.proc or self.proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=_NO_WINDOW)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def finish(self):
        if self.thread:
            self.thread.join(timeout=10)


def _is_master(p: str) -> bool:
    from .. import fitsmeta
    try:
        return bool(fitsmeta.from_header(pi._header(p), p).is_master)
    except Exception:  # noqa: BLE001
        return False


def _calib_spec(paths: list[str], prefix: str, raw_dir: str, log) -> dict | None:
    """Fertiger Master (ein Bild) → direkt verwenden; sonst Rohbilder in eine Sequenz legen."""
    if not paths:
        return None
    masters = [p for p in paths if _is_master(p)]
    if masters:
        return dict(master=masters[0])
    return dict(raw=raw_dir, prefix=prefix, paths=paths)


def _seq_name(folder: str, prefix: str, i: int, src: str) -> str:
    """Zielname für die Sequenz „prefix_NNNNN“. XISF behält seine Endung (Siril liest es direkt), FITS wird .fit."""
    ext = ".xisf" if src.lower().endswith(".xisf") else ".fit"
    return os.path.join(folder, "%s_%05d%s" % (prefix, i, ext))


def _stage_set(stg, cs: dict | None, folder: str, prefix: str) -> dict | None:
    """Rohbilder einer Kalibrierung als Sequenz „prefix_NNNNN“ bereitstellen (Master: nur Pfad)."""
    if cs and cs.get("paths"):
        for i, p in enumerate(cs.pop("paths"), 1):
            stg.place(p, _seq_name(folder, prefix, i, p))
    return cs


BAYER_INDEX = {"RGGB": 0, "BGGR": 1, "GBRG": 2, "GRBG": 3}     # Siril: debayer.pattern

# WBPP-Stellschrauben → Siril. Näherungen sind markiert; was Siril nicht umsetzt, steht im Lauf-Log.
WEIGHT_MAP = {"psf_signal": "wfwhm", "psf_snr": "noise", "snr": "noise"}   # Näherung: Siril hat keine PSF-Gewichtung (Siril 1.4: wfwhm|nbstars|noise)
FINDSTAR_MAP = {                    # WBPP-Sternerkennung → setfindstar
    "maxStarDistortion": ("roundness", "Näherung: Siril misst Rundheit statt Fläche/Quadrat"),
    "minStructureSize": ("minA", "Näherung: kleinste Sternfläche statt Strukturgröße"),
}
NOT_MAPPED = {                      # keine Entsprechung in Siril → im Log nennen, nicht still weglassen
    "localNormalization": "Lokale Normierung (Siril normiert global)",
    "distortionCorrection": "Verzeichnungskorrektur (Siril registriert nur über Homografie)",
    "lightsLargeScaleRejectionHigh": "Filter für Satelliten-/Flugzeugspuren",
    "structureLayers": "Strukturebenen der Sternerkennung",
    "peakResponse": "Peak-Antwort der Sternerkennung",
    "noiseReductionFilterRadius": "Rauschfilter vor der Sternerkennung",
    "drizzle": "Drizzle nicht automatisiert",
}
HANDLED = {"hotPixelFilterRadius": "Hotpixel entfernt b2stacker selbst (Cosmetic/CC)",
           "generateRejectionMaps": "→ -rejmaps",
           "subframeWeightingEnabled": "Gewichtung ist immer aktiv (-weight=)",
           "imageRegistration": "Registrierung ist immer aktiv",
           "integrate": "Stapeln ist immer aktiv"}


def map_wbpp(plan: dict, run: dict, lock_retry: bool = False) -> dict:
    """WBPP-Optionen und Sternerkennung → Siril-Befehle. → dict(findstar, weight, rejmaps, notes)."""
    opt = plan.get("options", {})
    wb = dict(run.get("wbpp") or {})
    params = dict((run.get("star_detection") or {}).get("params") or {})
    notes, findstar = [], {}
    if "sensitivity" in params:          # höhere WBPP-Empfindlichkeit → kleineres sigma (Näherung)
        findstar["sigma"] = min(3.0, max(0.3, round(2.0 - 1.5 * float(params.pop("sensitivity")), 2)))
    if lock_retry:                       # strengere Sternerkennung nach Fehlregistrierung
        findstar["sigma"] = min(5.0, findstar.get("sigma", 1.0) + 1.0)
    for k, (siril_key, why) in FINDSTAR_MAP.items():
        if k in params:
            findstar[siril_key] = params.pop(k)
            notes.append("Sternerkennung %s → setfindstar -%s (%s)" % (k, siril_key, why))
    for k in sorted(params):
        if k not in HANDLED:
            notes.append("Sternerkennung %s: %s" % (k, NOT_MAPPED.get(k, "keine Entsprechung in Siril")))
    for k in sorted(wb):
        if k not in HANDLED:
            notes.append("WBPP-Option %s=%s: %s" % (k, wb[k], NOT_MAPPED.get(k, "keine Entsprechung in Siril")))
    if opt.get("local_normalization"):
        notes.append("Local Normalization: " + NOT_MAPPED["localNormalization"])
    if opt.get("distortion_correction"):
        notes.append("Verzeichnungskorrektur: " + NOT_MAPPED["distortionCorrection"])
    if opt.get("large_scale_rejection"):
        notes.append("Satellitenspuren-Filter: " + NOT_MAPPED["lightsLargeScaleRejectionHigh"])
    if opt.get("drizzle"):
        notes.append("Drizzle: " + NOT_MAPPED.get("drizzle", "in Siril nicht automatisiert (nur im Stacking-Programm von Hand)"))
    weight = WEIGHT_MAP.get(opt.get("weighting", "psf_signal"), "wfwhm")
    rejmaps = str(wb.get("generateRejectionMaps", "true")).lower() != "false"
    framing = "min" if opt.get("autocrop") else "max"
    return dict(findstar=findstar, weight=weight, rejmaps=rejmaps, framing=framing, notes=notes)


def bayer_override(paths: list[str], log=print) -> str | None:
    """Bayer-Muster, das Siril erzwingen muss: Header fehlt oder die Lights widersprechen sich. None = Header passt."""
    from .. import fitsmeta
    pats = []
    for p in paths:
        try:
            pats.append((fitsmeta.read_meta(p).bayer or "").upper() or None)
        except Exception:  # noqa: BLE001 – unlesbarer Header zählt wie fehlend
            pats.append(None)
    known = [x for x in pats if x in BAYER_INDEX]
    if known and len(known) == len(pats) and len(set(known)) == 1:
        return None
    pat = max(set(known), key=known.count) if known else "RGGB"
    if not known:
        log("  WARNUNG: Bayer-Muster fehlt im Header der Lights – %s angenommen (Farben können vertauscht sein)." % pat)
    else:
        log("  Bayer-Muster in den Headern uneinheitlich (%s) – einheitlich %s erzwungen." % (
            ", ".join(sorted(set(known))), pat))
    logbook.event("bayer_override", engine="siril", pattern=pat, headers=sorted({x or "-" for x in pats}))
    return pat


REF_MARK = "<Referenz>"       # Platzhalter für die zwei Referenzframes in der Registrierungsauswertung


def _reference_files(src: str, osc: bool, shape: tuple, folder: str) -> str:
    """Referenzbild als zwei gleiche FITS-Frames („ref_00001/2“) mit Geometrie der Lights.

    Siril legt aus einem Einzelbild keine Sequenz an, daher zwei Frames; der zweite wird ebenfalls ausgeschlossen."""
    from astropy.io import fits
    if src.lower().endswith(".xisf"):
        from .. import xisf
        arr = xisf.read_image(src)
    else:
        arr = fits.getdata(src)
    arr = np.asarray(arr, dtype=np.float32)
    arr = arr[None] if arr.ndim == 2 else arr
    if arr.shape[1:] != tuple(shape):
        raise StackError("Referenzbild hat eine andere Bildgröße (%dx%d) als die Lights (%dx%d) – die Referenz muss "
                         "aus denselben Aufnahmen stammen." % (arr.shape[2], arr.shape[1], shape[1], shape[0]))
    if arr.shape[0] not in (1, 3):
        raise StackError("Referenzbild hat %d Kanäle – erwartet werden 1 (Mono) oder 3 (RGB)." % arr.shape[0])
    if osc and arr.shape[0] == 1:
        arr = np.repeat(arr, 3, axis=0)                  # Mono-Master für RGB-Lauf: Graustufen auf 3 Kanäle
    elif not osc and arr.shape[0] == 3:
        arr = arr.mean(axis=0, keepdims=True)            # RGB-Master für Mono-Lauf: Helligkeit als Mittelwert
    data = arr if osc else arr[0]
    os.makedirs(folder, exist_ok=True)
    for i in (1, 2):
        dst = os.path.join(folder, "ref_%05d.fit" % i)
        fits.PrimaryHDU(data=data.astype(np.float32), header=fits.Header()).writeto(dst, overwrite=True)
    return folder


SIGNAL_CAUSES = {                 # POSIX: negativer Exit-Code = Signalnummer
    -9: "Siril wurde vom Betriebssystem beendet (SIGKILL) – meist fehlt Arbeitsspeicher.",
    -11: "Siril ist abgestürzt (Speicherzugriffsfehler) – meist zu wenig Arbeitsspeicher.",
    -6: "Siril ist abgebrochen (SIGABRT).",
}


def _causes(lines: list[str], code: int | None = None) -> list[dict]:
    """Plain-language causes from Siril output (Siril messages are English)."""
    text = "\n".join(lines).lower()
    out = []
    if code is not None and code < 0 and code in SIGNAL_CAUSES:
        out.append(dict(level="error", text=SIGNAL_CAUSES[code]))
    elif code is not None and (code & 0xFFFFFFFF) in pi.CRASH_CODES:
        out.append(dict(level="error", text="Siril ist abgestürzt: " + pi.CRASH_CODES[code & 0xFFFFFFFF] + "."))
    rules = [
        ("not enough stars", "Zu wenige Sternpaare zum Ausrichten – Bilder zu dunkel, unscharf oder Wolken."),
        ("not enough star pairs", "Zu wenige Sternpaare zum Ausrichten – Bilder zu dunkel, unscharf oder Wolken."),
        ("star matching", "Sternzuordnung gescheitert – Sterne zu schwach oder Sternerkennung zu streng (Empfindlichkeit prüfen)."),
        ("no such file", "Eine Datei fehlt oder ist nicht lesbar (Pfad, Netzlaufwerk, Flatpak-Freigabe)."),
        ("not found", "Eine Datei fehlt oder ist nicht lesbar (Pfad, Netzlaufwerk, Flatpak-Freigabe)."),
        ("dimension", "Bildgrößen sind unterschiedlich – Lights und Kalibrierbilder müssen gleich groß sein."),
        ("out of memory", "Zu wenig Arbeitsspeicher – andere Programme schließen."),
        ("bad_alloc", "Zu wenig Arbeitsspeicher – andere Programme schließen."),
        # „memory“ allein steht in normalen Siril-Zeilen („memory difference: 0 MB“) → kein Stichwort
    ]
    for needle, msg in rules:
        if needle in text:
            out.append(dict(level="error", text=msg))
            break
    for ln in lines:
        if "error in line" in ln.lower() or "script execution failed" in ln.lower():
            out.append(dict(level="error", text=re.sub(r"^\W*(log:\s*)?", "", ln.strip())))
            break
    return out


def parse_registration(text: str, lights: list[str]) -> list[dict]:
    """Siril sequence after „register“ → per-light metrics (same shape as wbpplog.registrations).

    S field 6 = 0-based reference index. One Rk row per frame: star/FWHM values, 3x3 homography in fields 8-16.
    Mono uses R0, OSC registers on the green channel (R1). Failed: 0 stars or I flag 0."""
    ref, sel, rows = None, {}, {}
    for ln in text.splitlines():
        f = ln.split()
        if not f:
            continue
        if f[0] == "S" and len(f) > 6:
            ref = int(f[6])
        elif f[0] == "I" and len(f) > 2:
            sel[int(f[1])] = f[2] != "0"
        elif re.fullmatch(r"R\d+", f[0]) and len(f) >= 17:
            rows.setdefault(f[0], []).append(f)
    if not rows:
        return []
    rows = rows["R1"] if "R1" in rows else rows[sorted(rows)[0]]
    regs = []
    for i, f in enumerate(rows):
        if i >= len(lights):
            break
        if i == ref:
            regs.append({"reference": lights[i]})
            continue
        fwhm, nstars = float(f[1]), float(f[6])
        if fwhm <= 0 or nstars <= 0 or not sel.get(i + 1, True):
            regs.append({"file": lights[i], "failed": True, "fwhm": fwhm, "nstars": nstars})
            continue
        h00, h01, h02, h10, h11, h12 = (float(x) for x in f[8:14])
        regs.append({"file": lights[i], "scale": abs(h00 * h11 - h01 * h10) ** 0.5, "tx": h02, "ty": h12,
                     "rotation": math.degrees(math.atan2(h10, h00)), "fwhm": fwhm, "nstars": nstars,
                     "roundness": float(f[3]), "bglvl": float(f[5])})
    return regs


def _check_registration(run: dict, outdir: str, seq_name: str, ref_frames: int, notes: list, log) -> dict | None:
    """Registrierung aus der Siril-Sequenz prüfen. Wie PixInsight: Lauf ohne falsche Frames wiederholen (einmal)
    bzw. bei „am Sensor ausgerichtet“ mit Hotpixel-Entfernung und strengerer Sternerkennung neu rechnen."""
    # register writes the matrices into the input sequence; seqapplyreg's r_ output has identity matrices only
    seq = os.path.join(outdir, "seq_light", "%s.seq" % seq_name)
    try:
        regs = parse_registration(open(seq, encoding="utf-8", errors="replace").read(),
                                  run["lights"] + [REF_MARK] * ref_frames)
        regs = [r for r in regs if r.get("file") != REF_MARK]          # zweiter Referenzframe: nicht bewerten
        bad = wbpplog.judge(regs, wbpplog.light_info(run["lights"]))
    except Exception as e:  # noqa: BLE001 – Prüfung darf das Ergebnis nicht gefährden
        log("  Hinweis: Registrierung nicht geprüft (%s)" % e)
        return None
    frames = [r for r in regs if "file" in r]
    if not frames:                       # nichts auswertbar – „alle 0 plausibel“ wäre irreführend
        log("  Hinweis: Registrierung nicht geprüft (keine Frames in der Siril-Auswertung)")
        return None
    lock = [b for b in bad if b.get("lock")]
    logbook.event("registration_check", engine="siril", run=run["name"], frames=len(frames),
                  retry=bool(run.get("_retry")), lock_retry=bool(run.get("_lock_retry")), locked=len(lock),
                  bad=[dict(file=os.path.basename(b["file"]), why=b["why"], scale=b.get("scale"), lock=b.get("lock"),
                            tx=b.get("tx"), ty=b.get("ty"), rotation=b.get("rotation")) for b in bad],
                  stats=[dict(file=os.path.basename(r["file"]), fwhm=r.get("fwhm"), nstars=r.get("nstars"),
                              roundness=r.get("roundness"), bglvl=r.get("bglvl")) for r in frames if "file" in r])
    if not bad:
        if ref_frames:      # externe Referenz hat keine Lights-Herkunft → Sensor-Muster nicht prüfbar
            log("  Registrierung geprüft: %d Frames ohne Befund – Sensor-Prüfung nicht prüfbar (externe Referenz)"
                % len(frames))
        else:
            log("  Registrierung geprüft: alle %d Frames plausibel" % len(frames))
        return None
    if lock:
        log("  WARNUNG: %d Bilder aus anderen Nächten/von der anderen Meridianseite liegen „perfekt“ (0,0 px) auf der "
            "Referenz – Siril hat sie an Hotpixeln (Sensor) statt an den Sternen ausgerichtet." % len(lock))
        if not run.get("_lock_retry"):
            log("  → Neuer Versuch: Hotpixel werden entfernt und die Sternerkennung filtert stärker.")
            return dict(run, _lock_retry=True, _force_cosmetic=True,
                        _notes=notes + ["Erster Versuch an Hotpixeln ausgerichtet – mit Hotpixel-Entfernung neu gerechnet."])
        raise StackError("Siril hat die Bilder auch im zweiten Versuch an Sensor-Mustern statt an den Sternen "
                         "ausgerichtet (%d Bilder). Der Stack wäre unbrauchbar (Geistersterne). Abhilfe: passende Darks "
                         "aufnehmen und im Plan wählen, beim Aufnehmen dithern." % len(lock))
    for b in bad:
        log("  WARNUNG: %s falsch registriert (%s)" % (os.path.basename(b["file"]), ", ".join(b["why"])))
    drop = [b["file"] for b in bad]
    if pi.registration_verdict(bad, drop, run) == "suspect":
        notes.append("%d Frame(s) möglicherweise falsch registriert – Stack auf Geistersterne prüfen: %s" % (
            len(bad), ", ".join(os.path.basename(p) for p in drop)))
        return None
    log("  → Diese %d Frame(s) würden Geistersterne erzeugen. Stack wird ohne sie neu gerechnet." % len(drop))
    return pi.drop_frames(run, drop, notes, "Siril")


def _drop_cosmetic(jobdir: str, name: str) -> None:
    cos = os.path.join(jobdir, "_cosmetic")
    shutil.rmtree(os.path.join(cos, pi.safe_file(name)), ignore_errors=True)
    if os.path.isdir(cos) and not os.listdir(cos):
        os.rmdir(cos)


def _drop_intermediates(outdir: str) -> None:
    for old in [outdir] + glob.glob(glob.escape(outdir) + ".versuch-*"):
        for d in os.listdir(old):
            full = os.path.join(old, d)
            if os.path.isdir(full) and d not in ("logs", "master"):      # master/: Rejection-Maps bleiben
                shutil.rmtree(full, ignore_errors=True)


def _reference_source(run: dict, plan: dict, masters: dict, log) -> str | None:
    """Referenzbild aus „file:“ oder „master:<lauf>“ (wie PixInsight). None = Siril wählt selbst."""
    r = run.get("reference") or "auto"
    if r.startswith("master:"):
        src = r[7:]
        ref = masters.get(src)
        if not ref:
            from .. import project as _project
            ref = _project.latest_output(plan["workdir"], src)
        if not ref:
            log("  Referenz-Master %s nicht vorhanden – Siril wählt die Referenz selbst." % src)
        return ref
    if r.startswith("file:"):
        ref = r[5:]
        if not os.path.exists(ref):
            raise StackError("Referenzbild nicht gefunden: " + ref)
        return ref
    return None


def _light_shape(p: str) -> tuple:
    if p.lower().endswith(".xisf"):
        from .. import xisf
        return tuple(xisf.read_image(p).shape[-2:])
    from astropy.io import fits
    h = fits.getheader(p)
    return (int(h["NAXIS2"]), int(h["NAXIS1"]))


def siril_processes() -> list[dict]:
    """Laufende siril-cli-Prozesse (Linux: /proc, auch Flatpak-Sandbox; Windows: tasklist). → [{pid, mem_mb}]"""
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq siril-cli.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, timeout=15, creationflags=_NO_WINDOW).stdout
            return system.parse_tasklist(_decode(out))
        except Exception:  # noqa: BLE001 – Statusabfrage darf nie die Oberfläche blockieren
            return []
    if not sys.platform.startswith("linux"):
        return []
    found, me = [], os.getpid()
    for d in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(d))
            if pid == me:
                continue
            args = open(os.path.join(d, "cmdline"), "rb").read().split(b"\0")
        except (OSError, ValueError):
            continue
        if not args or os.path.basename(args[0]) != b"siril-cli":      # nur die CLI selbst, nicht flatpak/bwrap
            continue
        try:
            rss = int(open(os.path.join(d, "statm")).read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            rss = 0
        found.append(dict(pid=pid, mem_mb=rss / 1024 ** 2))
    return found


def siril_max_path(outdir: str) -> int:
    """Längster Pfad, den Siril im Arbeitsordner anlegt (Gruppen, Sequenzen mit kurzen Namen)."""
    base = outdir if re.match(r"^([A-Za-z]:)?[\\/]", outdir) else os.path.abspath(outdir)
    tails = ["\\seq_light\\r_lights_00001.fit",               # zusammengeführte Sequenz
             "\\g99\\seq_light\\r_pp_light_00001.fit",         # Gruppenordner (Kamera-Stellung je Nacht)
             "\\raw\\g99\\light\\light_00001.fit"]              # bereitgestellte Lights
    return len(base) + max(len(t) for t in tails)


LOG_LINES_PER_COMMAND = 20       # Siril-Ausgabe je Befehl im Lauf-Log; alles Weitere steht in siril.log


def phases_from_script(script_text: str, n: int, n_flats: int) -> list[dict]:
    """Befehlszeilen → Unterschritte (Master bauen, Kalibrieren, Registrieren, Stapeln) in Befehlsreihenfolge."""
    import shlex
    phases = []
    for i, ln in enumerate(l for l in script_text.splitlines() if l.strip()):
        f = shlex.split(ln)
        cmd = f[0]
        key, title, total = None, None, 0
        if cmd == "stack" and "-out=master_" in ln:
            kind = ln.split("-out=master_")[1].split()[0]
            key, title = "master_" + kind, "Master-%s erstellen" % {"darkflat": "Dark-Flat"}.get(kind, kind.capitalize())
        elif cmd == "calibrate":
            key, title, total = ("cal_flat", "Flats kalibrieren", n_flats) if f[1].startswith("flat_") else \
                ("cal_light", "Lights kalibrieren", n)
        elif cmd == "register":
            key, title, total = "register", "Registrieren", n
        elif cmd == "stack" and "-out=result" in ln:
            key, title, total = "integrate", "Stapeln", n
        if key:
            phases.append(dict(key=key, title=title, total=total, cmd=i))
    subs = []
    for p in phases:
        if subs and subs[-1]["key"] == p["key"] and subs[-1]["last"] == p["cmd"] - 1:
            subs[-1]["last"] = p["cmd"]
        else:
            subs.append(dict(key=p["key"], title=p["title"], total=p["total"], first=p["cmd"], last=p["cmd"]))
    return subs


def substep_view(subs: list[dict], cmd_idx: int, pct: float | None) -> list[dict]:
    """Stand der Unterschritte: fertige, aktueller (mit Zähler aus dem Siril-Fortschritt) – Befehlsindex 0-basiert."""
    view = []
    for s in subs:
        if s["first"] > cmd_idx:
            break
        if s["last"] < cmd_idx:
            state, done = "done", s["total"]
        else:
            state = "now"
            done = int(s["total"] * pct / 100) if (pct is not None and s["total"]) else 0
        view.append(dict(key=s["key"], title=s["title"], state=state, done=done, total=s["total"]))
    return view


def _one_run(plan, run, settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory) -> str:
    name = run["name"]
    orig_run = run
    outdir = os.path.join(jobdir, pi.safe_file(name))
    pi._isolate_attempt(outdir, log)                # frühere Versuche bleiben als .versuch-n (Protokolle)
    os.makedirs(outdir, exist_ok=True)
    n = len(run["lights"])
    log("=== Lauf %s (Siril): %d Lights ===" % (name, n))
    missing = [p for p in run["lights"] if not os.path.exists(p)]
    if missing:
        raise StackError("%d Lights fehlen, z. B. %s" % (len(missing), missing[0]))
    others = siril_processes()
    if others:      # ein headless siril-cli eines abgebrochenen Laufs würde Speicher und Dateien belegen
        raise StackError("Siril läuft bereits (%d Instanz(en), %s MB). Bitte beenden – sonst teilen sich die Läufe "
                         "Arbeitsspeicher und Dateien." % (
                             len(others), "/".join("%.0f" % (o["mem_mb"] or 0) for o in others)))
    free = system.free_disk_gb(outdir)
    need = run.get("gb") or 0
    if free is not None and free < need:
        raise StackError("Zu wenig Platz im Arbeitsverzeichnis: %.0f GB nötig, %.0f GB frei." % (need, free))
    fr, _tot = system.ram_gb()
    want = min(run.get("ram_gb") or settings.min_free_ram_gb, settings.min_free_ram_gb)
    if fr is not None and fr < want:
        raise StackError("Nur %.1f GB Arbeitsspeicher frei (mindestens %.0f GB nötig). Andere Programme schließen." % (fr, want))
    log("  Platz: %s GB frei / ~%.0f GB nötig · RAM: %s GB frei" % (
        "%.0f" % free if free is not None else "?", need, "%.1f" % fr if fr is not None else "?"))
    cache = os.path.join(settings.workdir, "cache", "calib_fixed")
    calib = dict(bias=pi.fix_calibration(run.get("bias", []), BIAS, cache),
                 darks=pi.fix_calibration(run.get("darks", []), DARK, cache),
                 darkflats=pi.fix_calibration(run.get("darkflats", []), DARK, cache),
                 flats=pi.fix_calibration(run.get("flats", []), FLAT, cache))
    pi._preflight_calibration(run, calib, log)
    if not calib["flats"]:
        log("  WARNUNG: keine Flats – Vignettierung und Staub bleiben im Bild.")
    ref_src = _reference_source(run, plan, masters, log)

    # Ohne Darks: Hotpixel vor dem Stacken mit unserer Korrektur entfernen (Siril-Äquivalent zur PixInsight-Stufe).
    # Mit Darks übernimmt -cc=dark die Hotpixel; _force_cosmetic (Wiederholung) erzwingt die Vorstufe immer.
    if (not calib["darks"] or run.get("_force_cosmetic")) and plan.get("options", {}).get("hot_pixels", True) is not False:
        run = pi._cosmetic(plan, dict(run, _force_cosmetic=True), settings, jobdir, calib, st, log, save, cancel)

    # Flat-Gruppen (Kamera-Stellung je Nacht): je Gruppe eigene Flats, danach eine gemeinsame Sequenz
    groups = None
    if run.get("flat_groups"):
        fg = [dict(g, flats=pi.fix_calibration(g.get("flats", []), FLAT, cache)) for g in run["flat_groups"]]
        groups = staging.plan_groups(dict(run, flat_groups=fg), run["lights"]) or None
    multi = bool(groups) and len(groups) > 1
    if multi:
        log("  Kamera-Stellung wechselt zwischen den Nächten → %d Gruppen mit eigenen Flats:" % len(groups))
        for g in groups:
            log("    Gruppe %s: Nächte %s · %d Lights · Flats %s (%d)" % (
                g.key, ", ".join(g.nights) or "–", len(g.lights), g.flat_set or "keine", len(g.flats)))
            if not g.flats:
                log("    WARNUNG: Gruppe %s ohne Flats – ihre Lights werden nicht flat-korrigiert." % g.key)
        logbook.event("flat_groups", run=name, engine="siril", groups=[dict(
            key=g.key, nights=g.nights, lights=len(g.lights), flats=len(g.flats), flat_set=g.flat_set) for g in groups])
    elif groups:
        calib["flats"] = list(groups[0].flats)

    # Bereitstellen: Lights und Rohkalibrierung als Sequenzen (Original-Dateien bleiben unverändert)
    raw = os.path.join(outdir, "raw")
    stg = staging.Stager(jobdir, os.path.join(settings.workdir, "cache"), log)
    if multi:
        set_groups = [(g, "g%d" % k) for k, g in enumerate(groups, 1)]
    else:
        set_groups = [(groups[0] if groups else None, "")]
    spec_groups, merged = [], []
    for g, sub in set_groups:
        lights_dir = os.path.join(raw, sub, "light")
        gl = g.lights if g else run["lights"]
        for i, p in enumerate(gl, 1):
            stg.place(p, _seq_name(lights_dir, "light", i, p))
        merged += gl
        gflats = g.flats if g else calib["flats"]
        flat_dir = os.path.join(raw, sub, "flat")
        spec_groups.append(dict(sub=sub, lights=lights_dir, n=len(gl),
                                flat=_stage_set(stg, _calib_spec(gflats, "flat", flat_dir, log), flat_dir, "flat")))
    if len(merged) != n:
        raise StackError("Interner Fehler: %d Lights bereitgestellt, %d gewählt." % (len(merged), n))
    spec = dict(outdir=outdir, osc=bool(run.get("is_osc")) and run.get("mode") in ("debayer", "extract"),
                cosmetic=bool(calib["darks"]) and plan.get("options", {}).get("hot_pixels", True) is not False,
                groups=spec_groups)
    if spec["osc"]:
        spec["bayer"] = bayer_override(merged, log)
    for kind, key, prefix in (("bias", "bias", "bias"), ("dark", "darks", "dark"), ("darkflat", "darkflats", "darkflat")):
        d = os.path.join(raw, kind)
        spec[kind] = _stage_set(stg, _calib_spec(calib[key], prefix, d, log), d, prefix)
    mp = map_wbpp(plan, run, lock_retry=bool(run.get("_lock_retry")))
    spec.update(findstar=mp["findstar"], weight=mp["weight"], rejmaps=mp["rejmaps"], framing=mp["framing"])
    if mp["findstar"]:
        log("  Sternerkennung %s → setfindstar %s" % (
            "strenger" if run.get("_lock_retry") else "angepasst",
            " ".join("-%s=%s" % kv for kv in sorted(mp["findstar"].items()))))
    for note in mp["notes"]:
        log("  Hinweis (WBPP → Siril): nicht übernommen – " + note if "→" not in note else
            "  Hinweis (WBPP → Siril): " + note)
    os.makedirs(os.path.join(outdir, "seq_light"), exist_ok=True)     # Ziel von merge (Siril wechselt dorthin)
    ref_dir = None
    if ref_src:
        ref_dir = _reference_files(ref_src, spec["osc"], _light_shape(merged[0]), os.path.join(raw, "ref"))
        spec["reference"] = ref_dir
        log("  Referenz: %s (Siril-Frames %d und %d, ausgeschlossen)" % (os.path.basename(ref_src), n + 1, n + 2))
    to_orig = dict(zip(run["lights"], orig_run["lights"]))           # Cosmetic-Kopien → Original-Lights
    reg_lights = [to_orig.get(p, p) for p in merged]                  # Registrierung in Siril-Reihenfolge
    script_text = build_script(spec)
    script = os.path.join(outdir, "siril.ssf")
    open(script, "w", encoding="utf-8").write(script_text)
    log("  Skript: %s (%d Zeilen)" % (script, script_text.count("\n")))
    logbook.event("staging", run=name, grouping=multi, lights=len(merged), **stg.stats)
    dirs = [outdir, settings.workdir] + ([os.path.dirname(ref_src)] if ref_src else []) + \
        [os.path.dirname(p) for p in run["lights"]] + \
        [os.path.dirname(p) for k in calib for p in (calib[k] or [])] + \
        [os.path.dirname(p) for g in (run.get("flat_groups") or []) for p in (g.get("flats") or [])]
    inst = locate(settings.siril)
    if inst is None:
        raise StackError(check(settings.siril)["message"])
    max_path = siril_max_path(outdir)
    if staging.long_paths_enabled() is False and max_path > 256:
        raise StackError("Die Zwischendateien von Siril würden Pfade mit ca. %d Zeichen bekommen – Windows erlaubt "
                         "ohne „lange Pfade“ nur 256. Abhilfe: ein kürzeres Arbeitsverzeichnis wählen (z. B. D:\\b2) "
                         "oder lange Pfade in Windows aktivieren (einmalig, Administrator: LongPathsEnabled = 1)."
                         % max_path)
    if staging.long_paths_enabled() is False and max_path > 236:
        log("  WARNUNG: längster Zwischenpfad ca. %d von 256 Zeichen – knapp. Kürzeres Arbeitsverzeichnis empfohlen."
            % max_path)
    make = runner_factory or (lambda s, w, d: SirilRunner(inst, s, w, d))
    runner = make(script, outdir, dirs)
    t0 = time.time()
    cmd = runner.start()
    open(os.path.join(outdir, "command.txt"), "w", encoding="utf-8").write(cmd)
    log("  Siril gestartet")
    timing = steps.load_timing(settings.workdir)
    seen: list[str] = []
    alert = ""
    logdir = os.path.join(outdir, "logs")
    os.makedirs(logdir, exist_ok=True)
    st.logdir = logdir                                # also set when cancelled before the end
    logf = open(os.path.join(logdir, "siril.log"), "a", encoding="utf-8")
    st.alert, st.hints, st.substeps = "", [], []

    state = dict(key="check", pct=None, cmd=-1, shown=0, hidden=0)
    subs = phases_from_script(open(script, encoding="utf-8").read(), n, len(calib.get("flats") or []))

    def set_step(nk: str):
        if nk != state["key"] and nk in steps.BY_KEY:
            pi._finish_step(st, settings.workdir, n)
            state["key"] = nk
            pi._set_step(st, nk, 0, n)
            log("  → %s" % steps.BY_KEY[nk]["title"])

    def read_lines():
        nonlocal alert
        for ln in runner.drain():                        # Zeile für Zeile, in Reihenfolge
            seen.append(ln)
            logf.write(ln + "\n")
            cmd = _RUNNING.search(ln)                     # Siril meldet jeden Befehl → Schritt daraus ableiten
            if cmd:
                c = cmd.group(1)
                state["cmd"] += 1
                state["pct"] = None
                if state["hidden"]:
                    log("    … %d weitere Siril-Ausgaben unterdrückt (siril.log)" % state["hidden"])
                state["shown"], state["hidden"] = 0, 0
                if c in ("convert", "calibrate"):
                    set_step("calibrate")
                elif c in ("register", "seqapplyreg"):
                    set_step("register")
                elif c == "stack":
                    set_step("integrate" if state["key"] == "register" else "calibrate")
                continue
            if ln.startswith("progress"):
                m = _PCT.search(ln)
                if m:
                    state["pct"] = float(m.group(1).replace(",", "."))
                continue
            low = ln.lower()
            if "error in line" in low or "execution failed" in low:
                alert = re.sub(r"^\W*(log:\s*)?", "", ln.strip())
                st.alert, st.alert_raw = alert, ln
                log("  ⚠ Siril: " + ln)
            elif not any(x in low for x in NOISE):           # bei Erfolg harmlos, nur in siril.log
                if state["shown"] < LOG_LINES_PER_COMMAND:    # gedrosselt: Stapel-Ausgaben füllen sonst das Protokoll
                    state["shown"] += 1
                    log("    Siril: " + ln)
                else:
                    state["hidden"] += 1

    code = None
    while True:
        code = runner.poll()
        read_lines()
        key = state["key"]
        if state["pct"] is not None:
            st.done = int(n * state["pct"] / 100)
        st.total = n
        st.substeps = substep_view(subs, state["cmd"], state["pct"])
        st.eta_s = pi._eta(st, n, timing)
        st.stage = steps.BY_KEY.get(key, {}).get("title", key)
        st.message = "%s · Lauf läuft seit %s" % (st.stage, pi._elapsed(time.time() - t0))
        save()
        if code is not None:
            break
        if cancel.wait(3):
            runner.kill()
            logf.close()
            raise Cancelled("abgebrochen")
    runner.finish()
    read_lines()
    logf.close()
    key = state["key"]
    pi._finish_step(st, settings.workdir, n)  # letzter Schritt zählt als erledigt
    st.message = "Siril beendet – werte Ergebnis aus …"
    save()
    result = os.path.join(outdir, "seq_light", "result.fit")
    ok = code == 0 and os.path.exists(result)
    logbook.event("siril_exit", run=name, code=code, duration_s=round(time.time() - t0, 1), command=cmd[:3000],
                  logdir=logdir, master=result if ok else None)
    if not ok:
        causes = _causes(seen, code)
        st.causes = causes
        why = causes[0]["text"] if causes else (
            "Siril hat keinen Stack erzeugt (Exit-Code %s)." % code if code else "Siril lief durch, hat aber keinen Stack erzeugt.")
        logbook.event("stack_diagnosis", run=name, causes=causes)
        raise StackError("Stacking fehlgeschlagen beim %s: %s" % (steps.BY_KEY.get(key, {}).get("title", "Stacken"), why))
    notes = list(orig_run.get("_notes") or [])
    retry = _check_registration(dict(orig_run, lights=reg_lights), outdir,
                                _stack_seq(len(spec_groups) + (1 if ref_dir else 0)), 2 if ref_dir else 0, notes, log)
    if retry is not None:
        return _one_run(plan, retry, settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory)
    log("  Stack: %s (%s)" % (os.path.basename(result), pi._elapsed(time.time() - t0)))
    master_keep = os.path.join(outdir, "master.fit")          # bleibt nach der Bereinigung erhalten
    shutil.copy2(result, master_keep)
    if spec["rejmaps"]:                                     # wie PixInsight: Rejection-Maps neben dem Master (master/)
        for stem in ("low", "high"):
            src = os.path.join(outdir, "seq_light", "result_%s_rejmap.fit" % stem)
            if os.path.exists(src):
                os.makedirs(os.path.join(outdir, "master"), exist_ok=True)
                shutil.move(src, os.path.join(outdir, "master", "rejection_%s.fit" % stem))
    st.steps_done.append("integrate")
    pi._set_step(st, "finalize", 0, 0)
    save()
    previewdir = os.path.join(plan["workdir"], "stacks")
    outputs = stackqa.process_master(result, run, plan["target"], finaldir, previewdir, datetime.date.today().isoformat(),
                                     pixscale=plan.get("pixscale"),
                                     fix_fringes=plan.get("options", {}).get("fix_fringes", True), log=log)
    st.steps_done.append("finalize")
    pi._set_step(st, "quality", 0, 0)
    save()
    for o in outputs:
        k = o["kpi"]
        log("  Qualität %s: Rauschen %.2f ADU · %d Sterne · Verlauf %.1f %%" % (o["name"], k["noise"], k["stars"],
                                                                           k["gradient_pct"]))
    st.steps_done.append("quality")
    bad_fwhm = pi.fwhm_implausible(run.get("star_stats") or orig_run.get("star_stats"), outputs)
    if bad_fwhm:
        log("  WARNUNG: " + bad_fwhm)
        for o in outputs:
            pi._discard_file(o["file"])
        if not orig_run.get("_lock_retry"):
            log("  → Neuer Versuch: Hotpixel werden entfernt und die Sternerkennung filtert stärker.")
            return _one_run(plan, dict(orig_run, _lock_retry=True, _force_cosmetic=True,
                                       _notes=notes + ["Erster Versuch unplausibel (%s) – neu gerechnet." % bad_fwhm]),
                            settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory)
        st.failed_step = "register"
        raise StackError("Der Stack ist unbrauchbar: %s Vermutlich hat Siril an Hotpixeln (Sensor) statt an den Sternen "
                         "ausgerichtet. Abhilfe: passende Darks wählen, beim Aufnehmen dithern." % bad_fwhm)
    finals = [o["file"] for o in outputs]
    st.finals += finals
    logbook.event("stack_run", project=os.path.basename(plan["workdir"]), run=name, lights=n, hours=run.get("hours"),
                  minutes=round((time.time() - t0) / 60, 1),
                  outputs=[dict(name=o["name"], file=os.path.basename(o["file"]), kpi=o["kpi"]) for o in outputs])
    st.runs[name] = dict(master=master_keep, finals=finals, outputs=outputs, minutes=round((time.time() - t0) / 60, 1),
                         lights=n, hours=run.get("hours"), nights=run.get("nights"), filter=run.get("filter"),
                         mode=run.get("mode"), flats=len(run.get("flats", [])), bias=len(run.get("bias", [])),
                         darks=len(run.get("darks", [])), reference=run.get("reference"),
                         notes=notes, excluded=orig_run.get("_excluded") or [])
    if settings.cleanup_intermediate:
        _drop_intermediates(outdir)
        _drop_cosmetic(jobdir, name)
    return master_keep


def run_plan(plan: dict, settings, log=print, on_status=None, cancel=None, runner_factory=None,
             status_file=None, open_results=None, **_kw) -> Status:
    """Alle Läufe eines Plans mit Siril. Gemeinsamer Rahmen mit PixInsight (engines.pixinsight.run_frame)."""
    c = check(settings.siril)
    if not c["ok"]:
        raise StackError(c["message"])
    inst = locate(settings.siril)

    def preflight():
        if not check(settings.siril)["ok"]:
            raise StackError(check(settings.siril)["message"])

    def opener(finals):
        try:
            cmd = open_command(settings.siril, list(finals))
        except FileNotFoundError as e:
            log("  Hinweis: Ergebnis nicht geöffnet – %s" % e)
            return
        subprocess.Popen(cmd)

    return pi.run_frame(plan, settings, one_run=_one_run, runner_factory=runner_factory, preflight=preflight,
                        opener=opener, log=log, on_status=on_status, cancel=cancel, status_file=status_file,
                        open_results=open_results)
