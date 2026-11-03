"""Siril als Stacking-Engine: Erkennung der siril-cli (Linux, Windows, macOS, Flatpak).

Phase 1: nur Erkennung und Prüfung. Der Lauf selbst (.ssf-Skript) folgt in Phase 2.
"""
from __future__ import annotations

import datetime
import glob
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

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
        fs = ["--filesystem=" + d for d in sorted({os.path.abspath(d) for d in dirs if d})]
        return ["flatpak", "run", "--env=LC_ALL=C", "--env=LANGUAGE=en"] + fs + ["--command=siril-cli", FLATPAK_ID] + list(args)


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


def build_script(spec: dict) -> str:
    """Skript für einen Lauf. spec: outdir, lights (Ordner), light_ext, osc, bias/dark/darkflat/flat →
    dict(raw=Ordner mit Präfix-Sequenz|None, prefix, master=Pfad|None), cosmetic (bool), wcs=..."""
    out = spec["outdir"]
    L = ["requires 1.4.0", "setext fit"]
    masters = {}

    def master_of(kind: str, s: dict, parent: str | None) -> str | None:
        if s.get("master"):
            return _p(s["master"])
        if not s.get("raw"):
            return None
        seq = os.path.join(out, "seq_" + kind)
        L.extend(["cd " + _quote(s["raw"]), "convert %s %s" % (s["prefix"], _kv("-out", seq)), "cd " + _quote(seq)])
        # Siril hängt an konvertierte Sequenzen „_“ an: Sequenz „bias“ → Befehle nutzen „bias_“
        if kind == "flat":
            opt = (" " + _kv("-dark" if parent[0] == "dark" else "-bias", parent[1])) if parent else ""
            L.append("calibrate %s_%s -prefix=pp_" % (s["prefix"], opt))
            L.append("stack pp_%s_ rej w 3 3 -norm=mul -output_norm -32b -out=master_%s" % (s["prefix"], kind))
        else:
            L.append("stack %s_ rej w 3 3 -nonorm -32b -out=master_%s" % (s["prefix"], kind))
        return _p(os.path.join(seq, "master_%s.fit" % kind))

    for kind in ("bias", "dark", "darkflat"):
        if spec.get(kind):
            masters[kind] = master_of(kind, spec[kind], None)
    if masters.get("darkflat"):
        flat_dark = ("dark", masters["darkflat"])          # Dark-Flats bevorzugt, sonst Bias
    elif masters.get("bias"):
        flat_dark = ("bias", masters["bias"])
    else:
        flat_dark = None
    if spec.get("flat"):
        if spec["flat"].get("master"):
            masters["flat"] = _p(spec["flat"]["master"])
        else:
            masters["flat"] = master_of("flat", spec["flat"], flat_dark)
    # Lights (Original-Dateien werden verknüpft/kopiert, nie verändert)
    seq_light = os.path.join(out, "seq_light")
    L.extend(["cd " + _quote(spec["lights"]), "convert light %s" % _kv("-out", seq_light), "cd " + _quote(seq_light)])
    cal = []
    if masters.get("bias"):
        cal.append(_kv("-bias", masters["bias"]))
    if masters.get("dark"):
        cal.append(_kv("-dark", masters["dark"]))
    if masters.get("flat"):
        cal.append(_kv("-flat", masters["flat"]))
    if masters.get("dark") and spec.get("cosmetic", True):
        cal.append("-cc=dark %.1f %.1f" % (pi.CC_COLD_SIGMA, pi.CC_HOT_SIGMA))
    if spec.get("osc"):
        cal += ["-cfa", "-equalize_cfa", "-debayer"]
    L.append("calibrate light_ %s -prefix=pp_" % " ".join(cal))
    if spec.get("sigma"):
        L.append("setfindstar -sigma=%.2f" % spec["sigma"])
    L.append("register pp_light_ -2pass -minpairs=10 -transf=homography -maxstars=2000")
    L.append("seqapplyreg pp_light_ -interp=lanczos4 -prefix=r_")
    L.append("stack r_pp_light_ rej w 3 3 -norm=addscale -weight=wfwhm -output_norm -32b%s -out=result"
             % (" -rgb_equal" if spec.get("osc") else ""))
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ Lauf

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
        self.proc = subprocess.Popen(self.args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
                                     errors="replace", creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        return " ".join(self.args)

    def _pump(self):
        for line in self.proc.stdout:
            with self._lock:
                self._out.append(line.rstrip("\r\n"))

    def drain(self) -> list[str]:
        with self._lock:
            out, self._out = self._out, []
        return out

    def poll(self):
        return self.proc.poll() if self.proc else None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

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


def _causes(lines: list[str]) -> list[dict]:
    """Verständliche Ursachen aus der Siril-Ausgabe (englische Meldungen, siehe docs/ERKENNTNISSE.md)."""
    text = "\n".join(lines).lower()
    out = []
    rules = [
        ("not enough stars", "Zu wenige Sternpaare zum Ausrichten – Bilder zu dunkel, unscharf oder Wolken."),
        ("not enough star pairs", "Zu wenige Sternpaare zum Ausrichten – Bilder zu dunkel, unscharf oder Wolken."),
        ("star matching", "Sternzuordnung gescheitert – Sterne zu schwach oder Sternerkennung zu streng (Empfindlichkeit prüfen)."),
        ("no such file", "Eine Datei fehlt oder ist nicht lesbar (Pfad, Netzlaufwerk, Flatpak-Freigabe)."),
        ("not found", "Eine Datei fehlt oder ist nicht lesbar (Pfad, Netzlaufwerk, Flatpak-Freigabe)."),
        ("dimension", "Bildgrößen sind unterschiedlich – Lights und Kalibrierbilder müssen gleich groß sein."),
        ("out of memory", "Zu wenig Arbeitsspeicher – andere Programme schließen."),
        ("memory", "Zu wenig Arbeitsspeicher – andere Programme schließen."),
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
    """Siril-Sequenz nach „register“ → Kennzahlen je Light (gleiche Form wie wbpplog.registrations).

    Fehlgeschlagen: R0-Zeile mit 0 Sternen bzw. I-Zeile mit Flag 0. Die Zeilen folgen der Reihenfolge der Lights."""
    ref, sel, rows = None, {}, []
    for ln in text.splitlines():
        f = ln.split()
        if not f:
            continue
        if f[0] == "S" and len(f) > 6:
            ref = int(f[6])
        elif f[0] == "I" and len(f) > 2:
            sel[int(f[1])] = f[2] != "0"
        elif f[0] == "R0" and len(f) >= 17:
            rows.append(f)
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


def _check_registration(run: dict, outdir: str, notes: list, log) -> dict | None:
    """Registrierung aus der Siril-Sequenz prüfen. Wie PixInsight: Lauf ohne falsche Frames wiederholen (einmal)
    bzw. bei „am Sensor ausgerichtet“ mit Hotpixel-Entfernung und strengerer Sternerkennung neu rechnen."""
    seq = os.path.join(outdir, "seq_light", "r_pp_light_.seq")
    try:
        regs = parse_registration(open(seq, encoding="utf-8", errors="replace").read(), run["lights"])
        bad = wbpplog.judge(regs, wbpplog.light_info(run["lights"]))
    except Exception as e:  # noqa: BLE001 – Prüfung darf das Ergebnis nicht gefährden
        log("  Hinweis: Registrierung nicht geprüft (%s)" % e)
        return None
    frames = [r for r in regs if not r.get("reference")]
    lock = [b for b in bad if b.get("lock")]
    logbook.event("registration_check", engine="siril", run=run["name"], frames=len(frames),
                  retry=bool(run.get("_retry")), lock_retry=bool(run.get("_lock_retry")), locked=len(lock),
                  bad=[dict(file=os.path.basename(b["file"]), why=b["why"], scale=b.get("scale"), lock=b.get("lock"),
                            tx=b.get("tx"), ty=b.get("ty"), rotation=b.get("rotation")) for b in bad],
                  stats=[dict(file=os.path.basename(r["file"]), fwhm=r.get("fwhm"), nstars=r.get("nstars"),
                              roundness=r.get("roundness"), bglvl=r.get("bglvl")) for r in frames if "file" in r])
    if not bad:
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


def _drop_intermediates(outdir: str) -> None:
    for old in [outdir] + glob.glob(glob.escape(outdir) + ".versuch-*"):
        for d in ("raw", "seq_light", "seq_bias", "seq_dark", "seq_darkflat", "seq_flat"):
            shutil.rmtree(os.path.join(old, d), ignore_errors=True)


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
    if run.get("flat_groups"):
        raise StackError("Flat-Gruppen (Kamera-Stellung je Nacht) sind für Siril noch nicht umgesetzt.")
    if run.get("reference") not in (None, "", "auto"):
        log("  WARNUNG: Referenz „%s“ wird mit Siril noch nicht übernommen – Siril wählt die Referenz selbst. "
            "Masters mehrerer Filter sind dadurch nicht zwingend deckungsgleich." % run.get("reference"))

    # Ohne Darks: Hotpixel vor dem Stacken mit unserer Korrektur entfernen (Siril-Äquivalent zur PixInsight-Stufe).
    # Mit Darks übernimmt -cc=dark die Hotpixel; _force_cosmetic (Wiederholung) erzwingt die Vorstufe immer.
    if (not calib["darks"] or run.get("_force_cosmetic")) and plan.get("options", {}).get("hot_pixels", True) is not False:
        run = pi._cosmetic(plan, dict(run, _force_cosmetic=True), settings, jobdir, calib, st, log, save, cancel)

    # Bereitstellen: Lights und Rohkalibrierung als Sequenzen (Original-Dateien bleiben unverändert)
    raw = os.path.join(outdir, "raw")
    stg = staging.Stager(jobdir, os.path.join(settings.workdir, "cache"), log)
    lights = []
    for i, p in enumerate(run["lights"], 1):
        dst = os.path.join(raw, "light", "light_%05d.fit" % i)          # setext fit: Siril liest nur diese Endung
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        lights.append(stg.place(p, dst))
    if len(lights) != n:
        raise StackError("Interner Fehler: %d Lights bereitgestellt, %d gewählt." % (len(lights), n))
    spec = dict(outdir=outdir, lights=os.path.join(raw, "light"), osc=bool(run.get("is_osc")) and
                run.get("mode") in ("debayer", "extract"), cosmetic=bool(calib["darks"]))
    spec["lights_prefix"] = "light"
    for kind, key, prefix in (("bias", "bias", "bias"), ("dark", "darks", "dark"),
                              ("darkflat", "darkflats", "darkflat"), ("flat", "flats", "flat")):
        cs = _calib_spec(calib[key], prefix, os.path.join(raw, kind), log)
        if cs and cs.get("paths"):
            d = os.path.join(raw, kind)
            os.makedirs(d, exist_ok=True)
            for i, p in enumerate(cs.pop("paths"), 1):
                stg.place(p, os.path.join(d, "%s_%05d.fit" % (prefix, i)))
        spec[kind] = cs
    sd = run.get("star_detection") or {}
    params = dict(sd.get("params") or {})
    if "sensitivity" in params:                    # Näherung: höhere WBPP-Empfindlichkeit → kleineres sigma
        spec["sigma"] = min(3.0, max(0.3, round(2.0 - 1.5 * float(params.pop("sensitivity")), 2)))
        log("  Sternerkennung → setfindstar -sigma=%.2f (Näherung aus WBPP-sensitivity)" % spec["sigma"])
    if params:
        log("  Hinweis: Sternerkennung-Parameter ohne Siril-Entsprechung nicht übernommen: %s" % ", ".join(sorted(params)))
    if run.get("_lock_retry"):                       # strengere Sternerkennung: höhere Schwelle
        spec["sigma"] = min(5.0, (spec.get("sigma") or 1.0) + 1.0)
        log("  Sternerkennung strenger → setfindstar -sigma=%.2f" % spec["sigma"])
    script_text = build_script(spec)
    script = os.path.join(outdir, "siril.ssf")
    open(script, "w", encoding="utf-8").write(script_text)
    log("  Skript: %s (%d Zeilen)" % (script, script_text.count("\n")))
    logbook.event("staging", run=name, grouping=False, lights=len(lights), **stg.stats)
    dirs = [outdir, settings.workdir] + [os.path.dirname(p) for p in run["lights"]] + \
        [os.path.dirname(p) for k in calib for p in (calib[k] or [])]
    inst = locate(settings.siril)
    if inst is None:
        raise StackError(check(settings.siril)["message"])
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
    logf = open(os.path.join(logdir, "siril.log"), "a", encoding="utf-8")
    st.alert, st.hints, st.substeps = "", [], []

    state = dict(key="check", pct=None)

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
                log("    Siril: " + ln)

    code = None
    while True:
        code = runner.poll()
        read_lines()
        key = state["key"]
        if state["pct"] is not None:
            st.done = int(n * state["pct"] / 100)
        st.total = n
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
    st.logdir = logdir
    save()
    result = os.path.join(outdir, "seq_light", "result.fit")
    ok = code == 0 and os.path.exists(result)
    logbook.event("siril_exit", run=name, code=code, duration_s=round(time.time() - t0, 1), command=cmd[:3000],
                  logdir=logdir, master=result if ok else None)
    if not ok:
        causes = _causes(seen)
        st.causes = causes
        why = causes[0]["text"] if causes else (
            "Siril hat keinen Stack erzeugt (Exit-Code %s)." % code if code else "Siril lief durch, hat aber keinen Stack erzeugt.")
        logbook.event("stack_diagnosis", run=name, causes=causes)
        raise StackError("Stacking fehlgeschlagen beim %s: %s" % (steps.BY_KEY.get(key, {}).get("title", "Stacken"), why))
    notes = list(orig_run.get("_notes") or [])
    retry = _check_registration(orig_run, outdir, notes, log)
    if retry is not None:
        return _one_run(plan, retry, settings, jobdir, masters, finaldir, st, log, save, cancel, runner_factory)
    log("  Stack: %s (%s)" % (os.path.basename(result), pi._elapsed(time.time() - t0)))
    master_keep = os.path.join(outdir, "master.fit")          # bleibt nach der Bereinigung erhalten
    shutil.copy2(result, master_keep)
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
        if inst.gui:
            subprocess.Popen(list(inst.gui) + list(finals))

    return pi.run_frame(plan, settings, one_run=_one_run, runner_factory=runner_factory, preflight=preflight,
                        opener=opener, log=log, on_status=on_status, cancel=cancel, status_file=status_file,
                        open_results=open_results)
