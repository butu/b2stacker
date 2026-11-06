"""Lokale Oberfläche: kleiner HTTP-Server (nur Standardbibliothek) + eine HTML-Seite.

Start: `astrostack gui` – öffnet http://127.0.0.1:8765 im Browser.
Nur von diesem Rechner erreichbar. Schreibende Aufrufe brauchen den Header X-Astrostack
(Schutz davor, dass fremde Webseiten Aufträge auslösen).
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__, assets, checklist, logbook, project, stackqa, steps, system, updater
from ..engines import pixinsight as pi
from ..jobs import JobManager
from ..settings import Settings

HERE = os.path.dirname(__file__)
JOBS = JobManager()
SETTINGS_LOCK = threading.Lock()
STATIC_FILES = {
    ("static", "photoswipe.js"): ("vendor/photoswipe/photoswipe.umd.min.js", "text/javascript; charset=utf-8"),
    ("static", "photoswipe.css"): ("vendor/photoswipe/photoswipe.css", "text/css; charset=utf-8"),
}


def settings() -> Settings:
    return Settings.load()


# ------------------------------------------------------------------ Aktionen

def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 – eine kaputte Einzelabfrage darf die Oberfläche nicht blockieren
        traceback.print_exc()
        return default


def state() -> dict:
    s = settings()
    free_ram, total_ram = _safe(system.ram_gb, (None, None))
    active = JOBS.active()
    last = JOBS.jobs[-1] if JOBS.jobs else None
    return dict(
        version=__version__,
        app_window=_safe(lambda: __import__("astrostack.app.window", fromlist=["STATE"]).STATE.get("window") is not None, False),
        settings={k: v for k, v in asdict(s).items() if k != "mcp_token"},
        install=_safe(lambda: __import__("astrostack.install", fromlist=["status"]).status(), {}),
        settings_file=Settings.path(),
        settings_problems=list(__import__("astrostack.settings", fromlist=["LOAD_PROBLEMS"]).LOAD_PROBLEMS),
        first_run=not os.path.exists(Settings.path()),
        pixinsight_ok=os.path.exists(s.pixinsight),
        wbpp_ok=os.path.exists(s.wbpp_script),
        pixinsight_running=_safe(system.pixinsight_processes, []),
        disk_free_gb=_r(_safe(lambda: system.free_disk_gb(s.workdir), None)),
        ram_free_gb=_r(free_ram), ram_total_gb=_r(total_ram),
        job=(active or last).as_dict(120) if (active or last) else None,
        jobs=_jobs_overview(),
        projects=_safe(lambda: _with_thumb_flags(s, project.list_projects(s)), []),
    )


def _with_thumb_flags(s, projects: list) -> list:
    """Kachel fragt nur nach einem Bild, wenn es lokal eines gibt (kein 404 je leerem Projekt)."""
    for p in projects:
        try:
            p["has_thumb"] = bool(cached_project_thumb(os.path.join(s.workdir, "projects", p["name"])))
        except Exception:  # noqa: BLE001
            p["has_thumb"] = False
    return projects


def sysinfo() -> dict:
    """Leichte Abfrage für die Live-Anzeige (alle paar Sekunden)."""
    s = settings()
    free_ram, total_ram = _safe(system.ram_gb, (None, None))
    du = _safe(lambda: __import__("shutil").disk_usage(_existing(s.workdir)), None)
    j = JOBS.active()
    return dict(ram_free_gb=_r(free_ram), ram_total_gb=_r(total_ram),
                disk_free_gb=_r(du.free / 1024 ** 3) if du else None, disk_total_gb=_r(du.total / 1024 ** 3) if du else None,
                pixinsight_running=_safe(system.pixinsight_processes, []),
                job=j.as_dict(120) if j else None, jobs=_jobs_overview())


def _jobs_overview() -> list[dict]:
    """Warteschlange für die Oberfläche: laufender Auftrag mit Protokoll, die übrigen kompakt."""
    return [j.as_dict(120 if j.state == "running" else 1) for j in JOBS.overview()]


def _existing(p: str) -> str:
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return p or "."


def _r(x, n=1):
    return round(x, n) if x is not None else None


SITE_LIMITS = {"site_lat": (-90, 90, "Breitengrad"), "site_lon": (-180, 180, "Längengrad"),
               "site_elev": (-500, 9000, "Höhe")}


def _site_value(k: str, v):
    """Ersatz-Standort: leer = keiner; Komma als Dezimalzeichen erlaubt; Grenzen prüfen."""
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(str(v).strip().replace(",", "."))
    except ValueError:
        raise ValueError("%s: „%s“ ist keine Zahl" % (SITE_LIMITS[k][2], v)) from None
    lo, hi, label = SITE_LIMITS[k]
    if not lo <= f <= hi:
        raise ValueError("%s muss zwischen %g und %g liegen" % (label, lo, hi))
    return f


SETTING_LIMITS = {"analysis_workers": (1, 32, "Gleichzeitige Analysen"), "min_free_ram_gb": (0, 1024, "Freier RAM"),
                  "watch_interval_s": (10, 3600, "Prüfabstand für neue Dateien (Sekunden)"),
                  "lan_port": (1024, 65535, "Port fürs Heimnetz")}


def _setting_value(k: str, cur, v):
    """Werte aus der Oberfläche prüfen, bevor sie gespeichert werden (sonst scheitert erst die nächste Analyse)."""
    if isinstance(cur, bool):
        if isinstance(v, str):
            if v.strip().lower() not in ("true", "false", "1", "0", "ja", "nein"):
                raise ValueError("%s: „%s“ ist kein Ja/Nein-Wert" % (k, v))
            return v.strip().lower() in ("true", "1", "ja")
        return bool(v)
    if isinstance(cur, (int, float)):
        try:
            num = float(str(v).strip().replace(",", ".")) if isinstance(v, str) else float(v)
        except (TypeError, ValueError):
            raise ValueError("%s: „%s“ ist keine Zahl" % (SETTING_LIMITS.get(k, (0, 0, k))[2], v)) from None
        if num != num:
            raise ValueError("%s: keine gültige Zahl" % k)
        if k in SETTING_LIMITS:
            lo, hi, label = SETTING_LIMITS[k]
            if not lo <= num <= hi:
                raise ValueError("%s muss zwischen %g und %g liegen" % (label, lo, hi))
        return int(num) if isinstance(cur, int) else num
    if isinstance(cur, str):
        if not isinstance(v, str):
            raise ValueError("%s: Text erwartet" % k)
        return v.strip()
    if isinstance(cur, list) and not isinstance(v, list):
        raise ValueError("%s: Liste erwartet" % k)
    if isinstance(cur, dict) and not isinstance(v, dict):
        raise ValueError("%s: Zuordnung erwartet" % k)
    return v


def save_settings(data: dict) -> dict:
    with SETTINGS_LOCK:
        s = settings()
        old_workdir = s.workdir
        for k, v in data.items():
            if not hasattr(s, k):
                raise ValueError("unbekannte Einstellung: " + k)
            cur = getattr(s, k)
            if k in ("site_lat", "site_lon", "site_elev"):
                v = _site_value(k, v)
            else:
                v = _setting_value(k, cur, v)
            setattr(s, k, v)
        if (s.site_lat is None) != (s.site_lon is None):
            raise ValueError("Für den Ersatz-Standort Breiten- und Längengrad angeben (oder beide leer lassen)")
        if s.workdir:
            os.makedirs(s.workdir, exist_ok=True)
        s.save()
    logbook.setup(s.workdir)
    logbook.event("settings", changed={k: v for k, v in data.items()}, workdir=s.workdir)
    st = state()
    if old_workdir and os.path.abspath(old_workdir) != os.path.abspath(s.workdir):
        st["move_offer"] = dict(old=old_workdir, projects=project.foreign_projects(old_workdir, s.workdir))
    return st


def _pick_folder_dialog(initial: str = ""):
    """Läuft in einem eigenen Prozess (tkinter verträgt sich nicht mit Server-Threads)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    p = filedialog.askdirectory(initialdir=initial or None, title="Ordner wählen")
    sys.stdout.write(p or "")
    sys.stdout.flush()


IMAGE_PATTERNS = "*.psd *.tif *.tiff *.jpg *.jpeg *.png *.xisf *.fits *.fit"


def _pick_file_dialog(initial: str = "", kind: str = ""):
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    d = initial if os.path.isdir(initial) else os.path.dirname(initial)
    if kind == "image":
        title, types = "Bild wählen", [("Bilder", IMAGE_PATTERNS), ("Alle Dateien", "*.*")]
    else:
        title, types = "PixInsight.exe wählen", [("Programme", "*.exe"), ("Alle Dateien", "*.*")]
    p = filedialog.askopenfilename(initialdir=d or None, title=title, filetypes=types)
    sys.stdout.write(p or "")
    sys.stdout.flush()


def pick_file(initial: str = "", kind: str = "") -> str | None:
    """Datei wählen (PixInsight.exe oder mit kind="image" ein Bild): im Programmfenster direkt, sonst über einen Hilfsprozess."""
    from . import window
    kind = "image" if kind == "image" else ""
    try:
        # pywebview erwartet die Endungen mit Semikolon getrennt, sonst lehnt es den Filter ab
        r = window.pick_file(initial, ("Bilder (%s)" % IMAGE_PATTERNS.replace(" ", ";"), "Alle Dateien (*.*)")) \
            if kind else window.pick_file(initial)
    except Exception:  # noqa: BLE001 - kaputter Fensterdialog: Hilfsprozess mit normalem Dateidialog
        r = False
    if r is not False:
        return r
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_pick-file", initial or "", kind]
    else:
        cmd = [sys.executable, "-m", "astrostack", "_pick-file", initial or "", kind]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                         creationflags=0x08000000 if sys.platform == "win32" else 0)
    if out.returncode != 0:
        raise RuntimeError("Datei-Dialog nicht verfügbar – Pfad bitte eintippen.")
    p = out.stdout.strip()
    return os.path.normpath(p) if p else None


def check_pixinsight(path: str) -> dict:
    """Ist das wirklich PixInsight? Ordner statt Datei gewählt → PixInsight.exe darin suchen."""
    p = (path or "").strip().strip('"')
    if p and os.path.isdir(p):
        for cand in (os.path.join(p, "PixInsight.exe"), os.path.join(p, "bin", "PixInsight.exe"),
                     os.path.join(p, "PixInsight"), os.path.join(p, "bin", "PixInsight")):
            if os.path.isfile(cand):
                p = cand
                break
    s = Settings(pixinsight=p)
    exists = os.path.isfile(p)
    name_ok = os.path.basename(p).lower().startswith("pixinsight")
    wbpp = os.path.exists(s.wbpp_script)
    if not exists:
        msg = "Datei nicht gefunden."
    elif not name_ok:
        msg = "Das scheint nicht PixInsight zu sein (erwartet: PixInsight.exe im Ordner …\\PixInsight\\bin)."
    elif not wbpp:
        msg = "PixInsight gefunden, aber das WBPP-Skript fehlt daneben – PixInsight-Installation prüfen."
    else:
        msg = "PixInsight und WBPP gefunden."
    return dict(path=p, ok=exists and name_ok and wbpp, exists=exists, wbpp=wbpp, message=msg)


def _pick_folder_powershell(initial: str):
    """Ersatz unter Windows: der Ordner-Dialog des Systems."""
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description='Ordner wählen';"
          "$d.SelectedPath=$env:AS_INITIAL;"
          "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
          "if($d.ShowDialog((New-Object System.Windows.Forms.Form -Property @{TopMost=$true})) -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}")
    env = dict(os.environ, AS_INITIAL=initial or "")
    return subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env, creationflags=0x08000000)


def _linux_initial_dir(initial: str) -> str:
    """Existing folder for a Linux dialog; empty input starts at home, never at the file system root."""
    raw = (initial or "").strip().strip('"')
    if not raw:
        return os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(raw))
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path if os.path.isdir(path) else os.path.expanduser("~")


def _linux_folder_command(initial: str):
    start = _linux_initial_dir(initial)
    if shutil.which("zenity"):
        folder = start if start.endswith(os.sep) else start + os.sep
        return ["zenity", "--file-selection", "--directory", "--title=Ordner wählen",
                "--filename=%s" % folder]
    if shutil.which("kdialog"):
        return ["kdialog", "--getexistingdirectory", start, "--title", "Ordner wählen"]
    return None


def _linux_pick_folder(initial: str):
    """Native Linux folder dialog; False lets the caller use the Tk helper when no tool is installed."""
    cmd = _linux_folder_command(initial)
    if not cmd:
        return False
    name = os.path.basename(cmd[0])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Ordner-Dialog nicht gestartet (%s): Zeitüberschreitung." % name) from e
    except OSError as e:
        raise RuntimeError("Ordner-Dialog nicht gestartet (%s): %s" % (name, e)) from e
    if out.returncode == 1:                    # cancel: no second dialog, no implicit path
        return None
    if out.returncode != 0:
        detail = (out.stderr or "").strip() or ("Exit-Code %d" % out.returncode)
        raise RuntimeError("Ordner-Dialog nicht gestartet (%s): %s" % (name, detail))
    selected = (out.stdout or "").strip()
    return os.path.normpath(selected) if selected else None


def pick_folders(initial: str = "") -> list[str]:
    """Mehrere Ordner wählen: im Programmfenster mit Mehrfachauswahl, unter Linux mit zenity --multiple,
    sonst einer (wie ``pick_folder``)."""
    from . import window
    r = window.pick_folders(initial)
    if r is not False:
        return r or []
    if sys.platform.startswith("linux") and shutil.which("zenity"):
        start = _linux_initial_dir(initial)
        try:
            out = subprocess.run(["zenity", "--file-selection", "--directory", "--multiple", "--separator=\n",
                                  "--title=Ordner wählen", "--filename=%s" % (start.rstrip(os.sep) + os.sep)],
                                 capture_output=True, text=True, timeout=900)
            if out.returncode in (0, 1):
                return [os.path.normpath(x) for x in out.stdout.splitlines() if x.strip()]
        except Exception:  # noqa: BLE001 – dann eben einzeln
            pass
    one = pick_folder(initial)
    return [one] if one else []


def pick_folder(initial: str = "") -> str | None:
    """Nativer Ordner-Dialog: im Programmfenster direkt, sonst über einen Hilfsprozess."""
    from . import window
    r = window.pick_folder(initial)
    if r is not False:
        return r
    if sys.platform.startswith("linux"):
        native = _linux_pick_folder(initial)
        if native is not False:
            return native
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "_pick-folder", initial or ""]
    else:
        cmd = [sys.executable, "-m", "astrostack", "_pick-folder", initial or ""]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                             creationflags=0x08000000 if sys.platform == "win32" else 0)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Ordner-Dialog nicht verfügbar (%s) – Pfad bitte eintippen." % e)
    if out.returncode != 0 and sys.platform == "win32":
        out = _pick_folder_powershell(initial)
    if out.returncode != 0:
        if sys.platform.startswith("linux"):
            raise RuntimeError("Ordner-Dialog nicht verfügbar – bitte zenity oder kdialog installieren.")
        raise RuntimeError("Ordner-Dialog nicht verfügbar – Pfad bitte eintippen.")
    p = out.stdout.strip()
    return os.path.normpath(p) if p else None


def _check_folders(folders):
    if isinstance(folders, str):
        folders = [folders]
    if not isinstance(folders, (list, tuple)):
        raise ValueError("Ordner bitte als Liste angeben.")
    folders = [f.strip().strip('"') for f in folders if isinstance(f, str) and f.strip()]
    if not folders:
        raise ValueError("Bitte mindestens einen Ordner angeben.")
    for f in folders:
        if not os.path.isdir(f):
            raise ValueError("Ordner nicht gefunden: " + f)
    return folders


def start_preview(folders: list[str]) -> dict:
    """Schritt 1 beim neuen Projekt: nur Header lesen und zeigen, welche Ziele gefunden wurden."""
    folders = _check_folders(folders)
    s = settings()
    s.sources = list(dict.fromkeys(folders + s.sources))[:8]
    s.save()

    def fn(job):
        return project.preview_targets(folders, s, progress=lambda i, n: job.set_progress(
            stage="Ordner durchsuchen", step="scan", done=i, total=n))
    return JOBS.submit("scan", "Ordner durchsuchen", s.workdir, fn).as_dict()


def _resolve_assign(assign: dict | None) -> dict | None:
    """Projektnamen der Rückfrage → Ziel-Namen bestehender Projekte (auch wenn umbenannt)."""
    if not assign:
        return None
    by_title = {}
    for p in project.list_projects(settings()):
        by_title[p["title"].strip().lower()] = p["target"]
        by_title[p["target"].strip().lower()] = p["target"]
    return {k: (by_title.get(v.strip().lower(), v.strip()) if v and v.strip() else None) for k, v in assign.items()}


def start_analysis(folders: list[str], target: str | None, assign: dict | None = None, title: str | None = None,
                   into: str | None = None, project_name: str | None = None) -> dict:
    folders = _check_folders(folders)
    s = settings()
    s.sources = list(dict.fromkeys(folders + s.sources))[:8]
    s.save()
    assign = _resolve_assign(assign)
    names = sorted({v for v in (assign or {}).values() if v}) or ([title] if title else [])

    def fn(job):
        res = project.run_report(folders, target or None, s, log=job.write,
                                 progress=lambda **kw: job.set_progress(**kw), cancel=job.cancel,
                                 assign=assign, title=(title or None), into=into)
        for r in res or []:                     # explicit analysis rebuilds inventory and asset previews
            try:
                assets.refresh(project.pdir_of(s, r["project"]), render_previews=True)
            except Exception as e:  # noqa: BLE001 - a failed inventory must not discard the analysis
                job.write("Asset-Inventar nicht aktualisiert: %s" % e)
        return res
    job = JOBS.submit("analyze", "Analyse " + (", ".join(names) if names else target or ", ".join(
        os.path.basename(f) or f for f in folders)), s.workdir, fn,
        project=project_name or (names[0] if len(names) == 1 else None))
    return job.as_dict()


def reanalyze(name: str) -> dict:
    pdir = project.pdir_of(settings(), name)
    src = project.sources(pdir)
    if not src.get("roots"):
        raise ValueError("Für dieses Projekt sind keine Quellordner gespeichert – bitte Ordner wählen.")
    # Projektschlüssel bleibt der Titel; die Lights-Objektnamen dürfen davon abweichen.
    return start_analysis(src["roots"], project.meta(pdir)["target"], into=project.meta(pdir)["target"], project_name=name)


def start_stack(name: str, only: list[str] | None = None) -> dict:
    """Stacking einreihen. only = nur diese Läufe (einmalig; die gespeicherten Plan-Einstellungen bleiben)."""
    s = settings()
    pdir = project.pdir_of(s, name)
    project.assert_stack_ready(pdir)
    preview = project.apply_edits(pdir, write=False)
    target = preview["target"]
    if only is not None:
        if isinstance(only, str):
            only = [only]
        known = [r["name"] for r in preview.get("runs", [])]
        bad = [n for n in only if n not in known]
        if bad or not only:
            raise ValueError("Unbekannter Lauf %s – vorhanden: %s" % (", ".join(bad) or "(keiner angegeben)", ", ".join(known)))

    def fn(job):
        # Plan erst beim Start lesen: in der Warteschlange darf man noch Frames und Optionen ändern.
        project.assert_stack_ready(pdir)
        plan = project.apply_edits(pdir)
        plan["workdir"] = pdir
        if only is not None:
            for r in plan["runs"]:
                r["enabled"] = r["name"] in only
            job.write("Nur diese Läufe: " + ", ".join(only))
        for line in project.apply_top_percent(pdir, plan):
            job.write(line)
        st = pi.run_plan(plan, s, log=job.write, cancel=job.cancel,
                         on_status=lambda x: job.set_progress(
                             stage=x.stage, step=x.step, run=x.run, run_index=x.run_index, runs_total=x.runs_total,
                             run_steps=x.run_steps, steps_done=x.steps_done, done=x.done, total=x.total,
                             eta_s=x.eta_s, message=x.message, alert=x.alert, alert_raw=x.alert_raw, hints=x.hints, substeps=x.substeps,
                             causes=x.causes,
                             failed_step=x.failed_step, logdir=x.logdir))
        return st.as_dict()
    job = JOBS.submit("stack", "Stacking " + target + (" (" + ", ".join(only) + ")" if only else ""), s.workdir, fn,
                      project=name)
    return job.as_dict()


def _preview_cache_dir() -> str | None:
    s = settings()
    return getattr(s, "preview_cache_dir", None) or os.environ.get("ASTROSTACK_PREVIEW_CACHE") or None


def _analysis_dir(pdir: str) -> str:
    return os.path.join(pdir, "analysis")


def _cached_media(path: str, pdir: str, variant: str) -> dict | None:
    from .. import previews
    fn = getattr(previews, "cached_media", None)
    if fn is None:                          # older previews module: stay cache-only, report unprepared
        return None
    return fn(path, _analysis_dir(pdir), variant=variant, preview_cache_dir=_preview_cache_dir())


def _dims(media: dict | None, *keys) -> tuple:
    for key in keys:
        if media and media.get(key) is not None:
            return media.get(key)
    return None


def _media_url(kind: str, name: str, path: str, media: dict | None = None) -> str:
    url = "/api/%s/%s?path=%s" % (kind, urllib.parse.quote(name), urllib.parse.quote(path))
    version = (media or {}).get("key") or (media or {}).get("version")
    return url + ("&v=" + urllib.parse.quote(str(version)) if version else "")


def _frame_media(row: dict, pdir: str, name: str, media: dict | None = None) -> dict:
    """Attach cache-only media metadata; never inspect the original frame here."""
    path = row["path"]
    media = media or {}
    thumb = media.get("thumb") if media else _cached_media(path, pdir, "thumb")
    screen = media.get("screen") if media else _cached_media(path, pdir, "screen")
    full = media.get("full") if media else _cached_media(path, pdir, "full")
    row["thumb_url"] = _media_url("frame-thumb", name, path, thumb)
    row["preview_url"] = _media_url("frame-preview", name, path, screen)
    row["full_url"] = _media_url("frame-full", name, path, full)
    row["width"] = _dims(screen or thumb, "orig_width", "source_width", "original_width") or row.get("width")
    row["height"] = _dims(screen or thumb, "orig_height", "source_height", "original_height") or row.get("height")
    row["preview_width"] = _dims(screen, "preview_width", "width")
    row["preview_height"] = _dims(screen, "preview_height", "height")
    row["media_ready"] = bool(screen or thumb)
    row["media_error"] = None if row["media_ready"] else "Vorschau nicht vorbereitet"
    return row


def _cached_media_map(paths: list[str], pdir: str) -> dict[str, dict]:
    """One manifest read per frame list; bulk only avoids repeated manifest parsing."""
    from .. import previews
    fn = getattr(previews, "cached_media_bulk", None)
    if fn is None:
        return {p: dict(thumb=_cached_media(p, pdir, "thumb"), screen=_cached_media(p, pdir, "screen"),
                        full=_cached_media(p, pdir, "full")) for p in paths}
    return fn(paths, _analysis_dir(pdir), variants=("thumb", "screen", "full"),
              preview_cache_dir=_preview_cache_dir())


def cached_frame_media(pdir: str, path: str, variant: str) -> dict | None:
    """Only already prepared cache media; explicit full-zoom preparation is separate."""
    if not _known_frame(pdir, path):
        raise PermissionError("unbekannte Datei")
    return _cached_media(path, pdir, variant)


def start_prepare_previews(name: str) -> dict:
    """Explicitly prepare browse media for the persisted project membership."""
    s = settings()
    pdir = project.pdir_of(s, name)
    frames = project.frames(pdir)
    if not frames:
        raise ValueError("Keine Frames im Projektindex – bitte zuerst eine Analyse ausführen.")
    meta = dict(created=time.strftime("%Y-%m-%dT%H:%M:%S"), paths=[f["path"] for f in frames])
    os.makedirs(_analysis_dir(pdir), exist_ok=True)
    with open(os.path.join(_analysis_dir(pdir), "preview-request.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)

    def fn(job):
        from .. import previews
        job.set_progress(stage="Vorschauen vorbereiten", step="previews", done=0, total=len(frames))
        return previews.prepare_frames(frames, _analysis_dir(pdir), preview_cache_dir=_preview_cache_dir())
    return JOBS.submit("previews", "Vorschauen " + name, s.workdir, fn, project=name).as_dict()


def start_prepare_assets(name: str) -> dict:
    """Prepare only the bounded asset locations; no light scan or analysis is required."""
    s = settings()
    pdir = project.pdir_of(s, name)

    def fn(job):
        job.set_progress(stage="Bilder vorbereiten", step="assets", done=0, total=0)
        entries = assets.refresh(pdir, render_previews=True)
        return {"assets": len(entries), "prepared": sum(1 for a in entries if a.get("preview"))}
    return JOBS.submit("assets", "Bilder vorbereiten " + name, s.workdir, fn, project=name).as_dict()


def _cache_roots(pdir: str) -> list[str]:
    from .. import previews
    roots = [os.path.join(_analysis_dir(pdir), n) for n in ("frame-thumbs", "previews", "asset-previews")]
    try:
        roots.append(previews.cache_root(_preview_cache_dir()))
    except Exception:  # noqa: BLE001 - fall back to the project-local cache roots
        if _preview_cache_dir():
            roots.append(_preview_cache_dir())
    return [os.path.normcase(os.path.realpath(r)) for r in roots]


def media_path_allowed(pdir: str, path: str) -> bool:
    """Only derived cache files may be served; never arbitrary project or source paths."""
    candidate = os.path.normcase(os.path.realpath(path))
    for root in _cache_roots(pdir):
        try:
            if os.path.commonpath([candidate, root]) == root:
                return True
        except ValueError:                      # different drive: not below this root
            continue
    return False


def _selection_delta(pdir: str, path: str) -> dict:
    fn = getattr(project, "selection_delta", None)
    if fn is None:
        raise RuntimeError("Frame-Auswahl benötigt project.selection_delta (Projektmodul unvollständig).")
    return fn(pdir, path)


def _selection_status(pdir: str) -> dict:
    """Use A's status helper; never invent a ready/current selection locally."""
    fn = getattr(project, "selection_delta", None)
    if fn is not None:
        try:
            return fn(pdir, None)
        except TypeError:                       # older helper without the optional path
            return fn(pdir)
    ex = project.detail_extras(pdir)
    if "selection_ready" in ex and "selection_revision" in ex:
        return dict(revision=ex["selection_revision"], analysis_required=ex["analysis_required"],
                    selection_ready=ex["selection_ready"])
    raise RuntimeError("Auswahlstatus nicht verfügbar – project.selection_delta fehlt.")


def _local_thumb_path(pdir: str, path: str | None) -> str | None:
    """Only project-local derivatives; an external NAS path is not even stat'ed."""
    if not path:
        return None
    p = os.path.abspath(path if os.path.isabs(path) else os.path.join(pdir, path))
    root = os.path.abspath(pdir)
    try:
        if os.path.commonpath([os.path.normcase(p), os.path.normcase(root)]) != os.path.normcase(root):
            return None
    except ValueError:
        return None
    real = os.path.realpath(p)
    try:
        if os.path.commonpath([os.path.normcase(real), os.path.normcase(os.path.realpath(root))]) != os.path.normcase(os.path.realpath(root)):
            return None
    except ValueError:
        return None
    return real if os.path.isfile(real) else None


def cached_project_thumb(pdir: str) -> str | None:
    """Existing local thumbnail only: browsing must not stat external originals."""
    own = _local_thumb_path(pdir, project.meta(pdir).get("thumb"))
    if own:
        return own
    for r in reversed(project.stacks(pdir)):
        for o in r.get("outputs", []):
            local = _local_thumb_path(pdir, o.get("preview"))
            if local:
                return local
    return _local_thumb_path(pdir, os.path.join(pdir, "thumb.jpg"))


def project_detail(name: str) -> dict:
    pdir = project.pdir_of(settings(), name)
    ex = project.detail_extras(pdir)
    plan = ex["plan"]
    hist = project.stacks(pdir)
    outs = sorted({o["name"] for r in hist for o in r.get("outputs", [])}) or \
        [o for r in plan.get("runs", []) if r.get("enabled", True) for o in r.get("outputs", [])]
    for r in hist:
        for o in r["outputs"]:
            if o.get("preview"):
                stored = project._rel(pdir, o["preview"])
                o["preview_url"] = "/api/stack-preview/%s?file=%s" % (
                    urllib.parse.quote(name), urllib.parse.quote(stored.replace("\\", "/")))
    asset_rows = []
    for a in ex["assets"]:
        a = dict(a)
        preview, err = assets.cached_preview(pdir, a["id"]) if a.get("id") else (None, None)
        if preview:
            a["preview_url"] = "/api/asset-preview/%s?id=%s%s" % (
                urllib.parse.quote(name), urllib.parse.quote(a["id"]),
                "&v=" + urllib.parse.quote(str(a["preview_version"])) if a.get("preview_version") else "")
        elif a.get("exists") is False:
            a["preview_error"] = err or "Datei fehlt"
        else:
            a["preview_error"] = err or "Vorschau nicht vorbereitet"
        asset_rows.append(a)
    asset_state = assets.state(pdir)
    plan = project.with_ranking(pdir, dict(plan, runs=[dict(r) for r in plan.get("runs", [])]))
    blockers = []
    try:
        project.assert_stack_ready(pdir)
    except ValueError as e:
        blockers.append(str(e))
    if not os.path.exists(settings().pixinsight):
        blockers.append("PixInsight wurde nicht gefunden – Pfad in den Einstellungen prüfen.")
    return dict(name=name, meta=project.meta(pdir), plan=plan, sources=project.sources(pdir), stack_blockers=blockers,
                analysis_required=ex["analysis_required"], assets=asset_rows,
                asset_scan_locations=ex["asset_scan_locations"],
                assets_migration_needed=asset_state["migration_needed"],
                assets_snapshot=asset_state,
                facts=project.project_facts(pdir, plan, hist),
                overrides=project._read(os.path.join(pdir, "overrides.json"), {}), stacks=hist,
                checklist=checklist.items(outs), pdir=pdir)


def _first_flag(flags) -> str | None:
    for f in flags or []:
        txt = f.get("text") if isinstance(f, dict) else f
        if txt:
            return str(txt)
    return None


def project_insights(name: str) -> dict:
    """Compact analysis summary for the overview (diagnosis, nights, sky tracks, counts).

    Read-only from the cached report snapshot: no originals are touched and the heavy
    per-frame thumbnails stay out of the response."""
    from ..report import build as report_build
    pdir = project.pdir_of(settings(), name)
    try:
        data = report_build.cached_data(pdir)
    except FileNotFoundError:
        return dict(available=False)
    frames = data.get("frames") or []
    reasons: dict[str, int] = {}
    counts = dict(lights=len(frames), used=0, suggest=0, warn=0, error=0)
    for f in frames:
        auto = f.get("auto_status") or f.get("status")
        if f.get("status") not in ("rejected", "suggest"):
            counts["used"] += 1
        if auto in ("suggest", "warn", "error"):
            counts[auto] += 1
        if auto == "suggest":
            r = _first_flag(f.get("flags"))
            if r:
                reasons[r] = reasons.get(r, 0) + 1
    sky = []
    for n in data.get("sky") or []:
        xs = [fr.get("x") for fr in n.get("frames") or [] if fr.get("x") is not None]
        sky.append(dict(night=n.get("night"), x=n.get("x"), obj=n.get("obj"), moon=n.get("moon"), sun=n.get("sun"),
                        illum=n.get("illum"), shots=[min(xs), max(xs)] if xs else None, filters=n.get("filters")))
    diag = data.get("diag") or {}
    return dict(available=True, meta=data.get("meta") or {}, kpi=data.get("kpi") or [], nights=data.get("nights") or [],
                diag=diag.get("checks") or [], calib=data.get("checks") or [], rec=data.get("rec") or [],
                sky=sky, counts=counts, reasons=sorted(reasons.items(), key=lambda kv: -kv[1]))


# Nur diese Änderungen kollidieren mit einer laufenden Analyse desselben Projekts (sie schreibt Plan und Auswahl neu).
# Alles andere – Häkchen in anderen Projekten, Namen, Löschen, neue Aufträge einreihen – geht jederzeit.
ANALYSIS_CONFLICTS = {"override", "overrides", "sources", "plan"}


def check_idle(action: str, name: str | None = None) -> None:
    if action in ANALYSIS_CONFLICTS and _analysis_running(name):
        raise RuntimeError("Dieses Projekt wird gerade analysiert – die Änderung bitte danach wiederholen.")


def _analysis_running(name: str | None = None) -> bool:
    """Läuft eine Analyse, die dieses Projekt betrifft? Unbekanntes Projekt (neue Ordner) zählt vorsichtshalber mit."""
    running = getattr(JOBS, "running", None)
    j = running() if running else JOBS.active()
    if not (j and j.kind == "analyze"):
        return False
    return name is None or getattr(j, "project", None) in (None, name)


def cancel_project_jobs(name: str) -> int:
    n = 0
    for j in list(getattr(JOBS, "jobs", [])):
        if j.project == name and j.state in ("queued", "running"):
            JOBS.cancel(j)
            n += 1
    return n


def _known_frame(pdir: str, path: str) -> bool:
    """Nur Dateien ausliefern, die zum Projekt gehören (Schutz vor beliebigen Pfaden).
    Mitgliedschaft im Frameindex genügt; die Bewertung wird nicht geladen."""
    return bool(path) and path in project.indexed_frame_paths(pdir)


def _registered_stack_preview(pdir: str, file: str) -> str | None:
    """Resolve only previews recorded in stack metadata, including external drives."""
    if not file:
        return None
    candidate = file if os.path.isabs(file) else os.path.join(pdir, file)
    candidate = os.path.realpath(candidate)
    registered = {os.path.normcase(os.path.realpath(o["preview"]))
                  for r in project.stacks(pdir) for o in r.get("outputs", []) if o.get("preview")}
    if candidate.lower().endswith(".jpg") and os.path.normcase(candidate) in registered:
        return candidate
    return None


def frame_thumb(pdir: str, path: str) -> str:
    """Explicit media preparation helper; browser tile requests use the cache-only endpoint."""
    if not _known_frame(pdir, path):
        raise PermissionError("unbekannte Datei")
    from .. import previews
    return previews.frame_thumbnail(path, _analysis_dir(pdir))


def project_frames(name: str) -> dict:
    """Frame list plus cache-only media and selection status for the review grid."""
    pdir = project.pdir_of(settings(), name)
    fr = project.frames(pdir)
    media = _cached_media_map([f["path"] for f in fr], pdir)
    for f in fr:
        _frame_media(f, pdir, name, media.get(f["path"], {}))
    status = _selection_status(pdir)
    return dict(frames=fr, summary=project.frames_summary(fr),
                plan_stale=bool(project.sources(pdir).get("plan_stale")),
                revision=status.get("revision"),
                analysis_required=status.get("analysis_required"),
                selection_ready=status.get("selection_ready"),
                media_preparation_needed=any(not f.get("media_ready") and f.get("type") != "unknown" for f in fr))


def start_requality(name: str, stack_id: str) -> dict:
    """Kennzahlen für ältere Stacks (ohne Messung) nachträglich berechnen."""
    s = settings()
    pdir = project.pdir_of(s, name)
    rec = next((r for r in project.stacks(pdir) if r.get("id") == stack_id), None)
    if not rec:
        raise ValueError("Stack nicht gefunden")
    plan = project.load_plan(pdir)

    def fn(job):
        for i, o in enumerate(rec["outputs"]):
            if not o.get("exists"):
                continue
            job.set_progress(stage="Qualität messen: " + os.path.basename(o["file"]), step="quality", done=i, total=len(rec["outputs"]))
            img = stackqa.load_master(o["file"])
            sh = stackqa.channel_shifts(img) if img.shape[0] == 3 else {}
            k = stackqa.kpis(img, plan.get("pixscale"), sh)
            pv = stackqa.preview_jpeg(img, os.path.join(pdir, "stacks", os.path.splitext(os.path.basename(o["file"]))[0] + ".jpg"))
            project.update_stack_output(pdir, stack_id, i, kpi=k, preview=pv)
            job.write("%s: Rauschen %.2f ADU, %d Sterne" % (os.path.basename(o["file"]), k["noise"], k["stars"]))
        return {"ok": True}
    return JOBS.submit("quality", "Kennzahlen " + rec.get("run", ""), s.workdir, fn, project=name).as_dict()


def start_selftest(folder: str) -> dict:
    """Selbsttest mit echtem PixInsight: legt nur einen neuen Testordner an, stackt ihn und schreibt einen Bericht."""
    folder = (folder or "").strip().strip('"')
    if not folder or not os.path.isdir(folder):
        raise ValueError("Ordner für den Selbsttest nicht gefunden: %s" % (folder or "–"))
    s = settings()

    def fn(job):
        from .. import selftest
        res = selftest.run(s, folder, log=job.write, cancel=job.cancel,
                           progress=lambda **kw: job.set_progress(**kw))
        logbook.event("selftest", ok=res.get("ok"), report=res.get("report"), environment=res.get("environment"),
                      steps=res.get("steps"), stack=res.get("stack"), cosmetic=res.get("cosmetic"))
        return res
    return JOBS.submit("selftest", "Selbsttest", s.workdir, fn).as_dict()


def start_pi_script(script: str, args: list | None = None, timeout_min: float | None = None) -> dict:
    """Eigenes PixInsight-Skript aus dem Arbeitsverzeichnis ausführen (Diagnose/Entwicklung, per KI bedienbar)."""
    from .. import piscript
    s = settings()
    path, args, notes = piscript.check(script, s.workdir, args)

    def fn(job):
        for n in notes:
            job.write("Hinweis: " + n)
        job.set_progress(stage="PixInsight-Skript", step="pi_script")
        res = piscript.run(s.pixinsight, path, args, s.workdir, log=job.write, cancel=job.cancel,
                           timeout_s=(timeout_min or 30) * 60)
        res["notes"] = notes
        logbook.event("pi_script", script=path, args=args, exit_code=res["exit_code"], state=res["state"],
                      duration_s=res["duration_s"], logfile=res["logfile"])
        if res["state"] == "timeout":           # Ergebnis bleibt lesbar, Auftrag gilt als fehlgeschlagen
            res.update(state="failed", error="Zeitlimit erreicht – PixInsight wurde beendet.")
        return res
    return JOBS.submit("pi_script", "PixInsight-Skript " + os.path.basename(path), s.workdir, fn).as_dict()


_MCP = {"handler": None, "url": None}


def _mcp_handler(base_url: str):
    """MCP-Werkzeuge im eigenen Prozess, die über HTTP mit dieser App sprechen (gleiche Regeln wie stdio)."""
    from .. import appclient, mcp, mcp_http
    if _MCP["handler"] is None or _MCP["url"] != base_url:
        tools = mcp.Tools(appclient.Client(url=base_url, autostart=False, who="mcp"))
        _MCP.update(handler=mcp_http.Handler(mcp.Server(tools)), url=base_url)
    return _MCP["handler"]


def quit_app() -> dict:
    """Programm beenden (Knopf in den Einstellungen) – wichtig, wenn kein eigenes Fenster läuft."""
    from . import window
    j = JOBS.running()
    for q in JOBS.queued():
        JOBS.cancel(q)
    if j:
        j.cancel.set()
        j.write("Abbruch: Programm wird beendet")
    logbook.event("app_quit", job_cancelled=bool(j))

    def bye():
        time.sleep(2.5 if j else 0.4)
        w = window.STATE.get("window")
        if w is not None:
            try:
                w.destroy()
                return
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}


def open_path(path: str):
    if path.startswith("https://github.com/%s" % updater.REPO):        # nur die eigene Release-Seite
        webbrowser.open(path)
        return
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_in_pixinsight(paths: list[str]):
    s = settings()
    if not os.path.exists(s.pixinsight):
        raise FileNotFoundError("PixInsight nicht gefunden: " + s.pixinsight)
    subprocess.Popen([s.pixinsight] + paths)


# ------------------------------------------------------------------ HTTP

VIA_AI = " · per Claude"


class Handler(BaseHTTPRequestHandler):
    server_version = "astrostack/" + __version__

    def log_message(self, fmt, *args):  # leise
        pass

    # Antworten
    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)
        except OSError:
            self._json({"error": "nicht gefunden"}, 404)

    def _media(self, pdir: str, path: str, ctype: str, version: str | None = None):
        """Cacheable derived media with an immutable versioned URL and ETag revalidation."""
        if not media_path_allowed(pdir, path):
            return self._json({"error": "Pfad nicht erlaubt"}, 403)
        try:
            st = os.stat(path)
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._json({"error": "Vorschau nicht gefunden"}, 404)
        tag_src = version or "%s|%s|%s" % (os.path.normcase(os.path.realpath(path)), st.st_mtime_ns, st.st_size)
        etag = '"%s"' % hashlib.sha1(tag_src.encode("utf-8")).hexdigest()[:20]
        # Without a content key the file may be overwritten in place (for example id.jpg).
        cache = "public, max-age=31536000, immutable" if version else "no-cache, must-revalidate"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        self._send(200, body, ctype, cache=cache, headers={"ETag": etag})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _via(self, d):
        """Aufträge, die eine KI über den MCP-Server anstößt, im Fenster als solche kennzeichnen."""
        if self.headers.get("X-B2-Client") == "mcp" and isinstance(d, dict) and d.get("id"):
            j = JOBS.get(d["id"])
            if j and not j.title.endswith(VIA_AI):
                j.title += VIA_AI
                d["title"] = j.title
        return d

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(b, dict):
            raise ValueError("Anfrage muss ein JSON-Objekt sein")
        return b

    # Routen
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden"}, 403)
        u = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
        q = urllib.parse.parse_qs(u.query)
        if parts == ["mcp"]:                       # MCP über HTTP: nur POST (kein SSE-Strom nötig)
            return self._send(405, b"", "text/plain", headers={"Allow": "POST"})
        try:
            if not parts:
                return self._file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
            if parts == ["api", "state"]:
                return self._json(state())
            if parts[:2] == ["api", "job"] and len(parts) == 3:
                j = JOBS.get(parts[2])
                return self._json(j.as_dict(int(q.get("tail", ["300"])[0])) if j else {"error": "unbekannt"}, 200 if j else 404)
            if parts == ["api", "sys"]:
                return self._json(sysinfo())
            if parts == ["api", "update"]:
                return self._json(updater.status())
            if parts == ["api", "claude"]:                 # Verbindung zur Claude-Desktop-App (MCP)
                from .. import claude_link
                return self._json(claude_link.status())
            if parts == ["api", "steps"]:
                return self._json(dict(analyze=steps.ANALYZE, stack=steps.STACK))
            if parts[:2] == ["api", "project"] and len(parts) == 4 and parts[3] == "frames":
                return self._json(project_frames(parts[2]))
            if parts[:2] == ["api", "project"] and len(parts) == 4 and parts[3] == "insights":
                return self._json(project_insights(parts[2]))
            if parts[:2] == ["api", "frame-stars"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                path = q.get("path", [""])[0]
                if not _known_frame(pdir, path):
                    return self._json({"error": "unbekannte Datei"}, 403)
                from .. import fitsmeta, starmap
                try:
                    bayer = fitsmeta.read_meta(path).bayer
                    return self._json(starmap.frame_stars(path, os.path.join(_analysis_dir(pdir), "stars"), bayer,
                                                          hot=q.get("hot", ["0"])[0] == "1"))
                except (OSError, ValueError) as e:
                    return self._json({"error": "Sterne nicht lesbar: %s" % e}, 404)
            if parts[:2] == ["api", "frame-thumb"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                try:
                    media = cached_frame_media(pdir, q.get("path", [""])[0], "thumb")
                except PermissionError as e:
                    return self._json({"error": str(e)}, 403)
                if not media:
                    return self._json({"error": "Vorschau nicht vorbereitet – bitte Vorschauen explizit vorbereiten."}, 404)
                version = q.get("v", [""])[0] or media.get("key")
                return self._media(pdir, media["path"], "image/jpeg", version)
            if parts[:2] == ["api", "project"] and len(parts) == 3:
                return self._json(project_detail(parts[2]))
            if parts[:2] == ["api", "frame-preview"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                try:
                    media = cached_frame_media(pdir, q.get("path", [""])[0], "screen")
                except PermissionError as e:
                    return self._json({"error": str(e)}, 403)
                if not media:
                    return self._json({"error": "Vorschau nicht vorbereitet – bitte Vorschauen explizit vorbereiten."}, 404)
                version = q.get("v", [""])[0] or media.get("key")
                return self._media(pdir, media["path"], "image/jpeg", version)
            if parts[:2] == ["api", "frame-full"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                path = q.get("path", [""])[0]
                try:
                    media = cached_frame_media(pdir, path, "full")
                except PermissionError as e:
                    return self._json({"error": str(e)}, 403)
                if not media:                   # explicit zoom is the one place that may prepare full media
                    frame = next((f for f in project.frames(pdir) if f["path"] == path), None)
                    if not frame:
                        return self._json({"error": "unbekannte Datei"}, 403)
                    from .. import previews
                    try:
                        media = previews.full_prepare(path, _analysis_dir(pdir),
                                                      preview_cache_dir=_preview_cache_dir())
                    except previews.PreviewError as e:
                        return self._json({"error": str(e)}, 422)
                if not media:
                    return self._json({"error": "Großansicht nicht verfügbar"}, 422)
                version = q.get("v", [""])[0] or media.get("key")
                return self._media(pdir, media["path"], "image/jpeg", version)
            if parts[:2] == ["api", "stack-preview"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                f = _registered_stack_preview(pdir, q.get("file", [""])[0])
                if not f:
                    return self._json({"error": "verboten"}, 403)
                return self._file(f, "image/jpeg")
            if parts[:2] == ["api", "asset-preview"] and len(parts) == 3:
                # only registered assets (per id), only an already rendered preview
                pdir = project.pdir_of(settings(), parts[2])
                path, err = assets.cached_preview(pdir, q.get("id", [""])[0])
                if err:
                    return self._json({"error": err}, 422)
                return self._media(pdir, path, "image/jpeg", q.get("v", [""])[0] or None)
            if parts[:2] == ["api", "project-thumb"] and len(parts) == 3:
                pdir = project.pdir_of(settings(), parts[2])
                t = cached_project_thumb(pdir)
                if not t:
                    return self._json({"error": "kein Bild"}, 404)
                try:
                    with open(t, "rb") as fh:
                        body = fh.read()
                except OSError:
                    return self._json({"error": "kein Bild"}, 404)
                # Die Kachel-URL trägt eine Version (?v=…); ändert sich das Bild, ändert sich die URL.
                return self._send(200, body, "image/jpeg",
                                  cache="private, max-age=86400" if q.get("v", [""])[0].strip("|") else "no-cache")
            if tuple(parts) in STATIC_FILES:
                rel, ctype = STATIC_FILES[tuple(parts)]
                return self._file(os.path.join(HERE, rel), ctype)
            if parts == ["static", "chart.js"]:
                return self._file(os.path.join(os.path.dirname(HERE), "report", "vendor", "chart.umd.js"), "text/javascript; charset=utf-8")
            if parts[0] == "p" and len(parts) == 3 and parts[2] == "report.html":
                from ..report import build as report_build
                pdir = project.pdir_of(settings(), parts[1])
                body = report_build.cached_html(pdir)
                if isinstance(body, str):
                    body = body.encode("utf-8")
                return self._send(200, body, "text/html; charset=utf-8")
            return self._json({"error": "nicht gefunden"}, 404)
        except FileNotFoundError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001
            logbook.error("HTTP %s %s" % (self.command, self.path), kind_detail="http_error")
            return self._json({"error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-800:]}, 500)

    def _base_url(self) -> str:
        return "http://127.0.0.1:%d/" % self.server.server_address[1]

    def _mcp_post(self):
        """MCP über HTTP: Schlüssel und Origin prüfen, JSON-RPC verarbeiten."""
        from .. import mcp_http
        if not mcp_http.origin_ok(self.headers):
            return self._json({"error": "forbidden origin"}, 403)
        if not mcp_http.authorized(self.headers, mcp_http.ensure_token(settings())):
            return self._send(401, json.dumps({"error": "Schlüssel fehlt oder ist falsch (Einstellungen → KI-Bedienung)"},
                                              ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                              headers={"WWW-Authenticate": 'Bearer realm="b2stacker"'})
        n = int(self.headers.get("Content-Length") or 0)
        code, body = _mcp_handler(self._base_url()).handle_body(self.rfile.read(n) if n else b"")
        if body is None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._json(body, code)

    def do_POST(self):
        if self._host_ok() and urllib.parse.urlparse(self.path).path.rstrip("/") == "/mcp":
            try:
                return self._mcp_post()
            except Exception as e:  # noqa: BLE001
                return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}, 500)
        if not self._host_ok() or self.headers.get("X-Astrostack") != "1":
            return self._json({"error": "forbidden"}, 403)
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.strip("/").split("/") if p]
        try:
            b = self._body()
            if parts == ["api", "settings"]:
                return self._json(save_settings(b))
            if parts == ["api", "pick-file"]:
                return self._json({"path": pick_file(b.get("initial", ""), b.get("kind", ""))})
            if parts == ["api", "check-pixinsight"]:
                return self._json(check_pixinsight(b.get("path", "")))
            if parts == ["api", "pick-folder"]:
                return self._json({"path": pick_folder(b.get("initial", ""))})
            if parts == ["api", "scan-preview"]:
                return self._json(self._via(start_preview(b.get("folders", []))))
            if parts == ["api", "analyze"]:
                return self._json(self._via(start_analysis(b.get("folders", []), b.get("target"), b.get("assign"),
                                                           b.get("title"))))
            if parts[:2] == ["api", "job"] and len(parts) == 4 and parts[3] == "cancel":
                j = JOBS.get(parts[2])
                if j:
                    JOBS.cancel(j)
                return self._json({"ok": bool(j)})
            if parts == ["api", "open"]:
                open_path(b["path"])
                return self._json({"ok": True})
            if parts == ["api", "move-projects"]:
                moved = project.move_projects(b["old"], settings().workdir, b.get("projects", []))
                logbook.event("move_projects", old=b["old"], new=settings().workdir, moved=moved)
                return self._json({"moved": moved})
            if parts == ["api", "pi-script"]:
                return self._json(self._via(start_pi_script(b.get("script", ""), b.get("args") or [],
                                                            b.get("timeout_min"))))
            if parts == ["api", "selftest"]:
                return self._json(self._via(start_selftest(b.get("folder", ""))))
            if parts == ["api", "install"]:
                from .. import install
                res = install.install(quit_fn=lambda: threading.Timer(1.5, quit_app).start())
                return self._json(res)
            if parts == ["api", "mcp-info"]:
                from .. import mcp_http
                return self._json(mcp_http.info(self._base_url(), mcp_http.ensure_token(settings())))
            if parts == ["api", "mcp-token"]:
                from .. import mcp_http
                tok = mcp_http.new_token(settings())
                logbook.event("mcp_token_renewed")
                return self._json(mcp_http.info(self._base_url(), tok))
            if parts == ["api", "ui-event"]:          # Diagnose aus dem Fenster (z. B. "Auftrag fertig gesehen")
                kind = "".join(c for c in str(b.get("kind", "event")) if c.isalnum() or c == "_")[:40]
                logbook.event("ui_" + kind, **{k: str(v)[:300] for k, v in b.items() if k != "kind"})
                return self._json({"ok": True})
            if parts == ["api", "focus"]:
                from . import window
                return self._json({"ok": window.focus()})
            if parts == ["api", "quit"]:
                return self._json(quit_app())
            if parts in (["api", "claude", "install"], ["api", "claude", "uninstall"]):
                from .. import claude_link
                res = claude_link.install() if parts[2] == "install" else claude_link.uninstall()
                logbook.event("claude_link", action=parts[2], files=res.get("changed"), text=res.get("text"))
                return self._json(res)
            if parts == ["api", "update", "check"]:
                return self._json(updater.check(force=True))
            if parts == ["api", "update", "install"]:
                return self._json(updater.start_install(quit_app, busy_fn=JOBS.active))
            if parts == ["api", "open-pixinsight"]:
                open_in_pixinsight(b["paths"])
                return self._json({"ok": True})
            if parts == ["api", "project-create"]:
                pdir = project.create_project(settings(), b.get("title", ""), b.get("roots") or [])
                return self._json(project_detail(os.path.basename(pdir)))
            if parts[:2] == ["api", "project"] and len(parts) == 4:
                name, action = parts[2], parts[3]
                check_idle(action, name)
                pdir = project.pdir_of(settings(), name)
                if action == "delete":                  # nur Archiv-Flag – kein Ordner wird angefasst
                    cancelled = cancel_project_jobs(name)
                    project.archive_project(pdir)
                    logbook.event("project_archived", project=name, jobs_cancelled=cancelled)
                    return self._json({"ok": True})
                if action == "stack-meta":
                    project.update_stack_meta(pdir, b.get("stack_id", ""), name=b.get("name"))
                    return self._json(project_detail(name))
                if action == "stack-delete":
                    project.remove_stack(pdir, b.get("stack_id", ""))
                    return self._json(project_detail(name))
                if action == "asset-link":
                    assets.link(pdir, b.get("path", ""))
                    return self._json(project_detail(name))
                if action == "plan":
                    if not isinstance(b.get("runs") or {}, dict) or not isinstance(b.get("options") or {}, dict):
                        raise ValueError("Plan-Änderung: runs und options müssen Objekte sein")
                    if any("calib" in (v or {}) for v in (b.get("runs") or {}).values()):
                        logbook.event("calib_choice", project=name, change=b.get("runs"))
                    return self._json(project.with_ranking(pdir, project.save_edits(pdir, b)))
                if action == "override":
                    project.set_override(pdir, b["path"], b.get("value"))
                    return self._json(_selection_delta(pdir, b["path"]))
                if action == "prepare-previews":
                    return self._json(start_prepare_previews(name))
                if action == "prepare-assets":
                    return self._json(start_prepare_assets(name))
                if action == "reanalyze":
                    return self._json(self._via(reanalyze(name)))
                if action == "stack":
                    return self._json(self._via(start_stack(name, b.get("only"))))
                if action == "overrides":              # viele Entscheidungen auf einmal (KI/MCP)
                    project.set_overrides(pdir, b.get("decisions") or {})
                    return self._json(_selection_status(pdir))
                if action == "meta":
                    project.save_meta(pdir, **{k: v for k, v in b.items() if k in ("name", "description", "checklist")})
                    return self._json(project_detail(name))
                if action == "thumb":
                    if b.get("frame") and not _known_frame(pdir, b["frame"]):
                        raise ValueError("unbekannte Datei")
                    rel = None
                    if b.get("stack_preview"):
                        rel = project._rel(pdir, b["stack_preview"]) if os.path.isabs(b["stack_preview"]) else b["stack_preview"]
                    project.set_thumbnail(pdir, stack_preview=rel, frame=b.get("frame"))
                    return self._json(project_detail(name))
                if action == "sources":
                    if _analysis_running(name):         # kein Quellwechsel mitten in einer Analyse
                        raise RuntimeError("Eine Analyse läuft – Quellordner bitte danach ändern.")
                    project.set_sources(pdir, b.get("roots", []))
                    return self._json(project_detail(name))
                if action == "calib-inspect":
                    plan = project.load_plan(pdir)
                    run = next((r for r in plan["runs"] if r["name"] == b["run"]), None)
                    if not run:
                        raise ValueError("Lauf nicht gefunden")
                    return self._json(project.inspect_calib_folder(b["folder"], b["kind"], run))
                if action == "requality":
                    return self._json(start_requality(name, b["stack_id"]))
            return self._json({"error": "nicht gefunden"}, 404)
        except (ValueError, RuntimeError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        except FileNotFoundError as e:
            return self._json({"error": "nicht gefunden: %s" % e}, 404)
        except Exception as e:  # noqa: BLE001
            logbook.error("HTTP %s %s" % (self.command, self.path), kind_detail="http_error")
            return self._json({"error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-800:]}, 500)


def serve(port: int = 8765, open_browser: bool = True) -> ThreadingHTTPServer:
    s = settings()
    logbook.setup(s.workdir)
    logbook.event("app_start", workdir=s.workdir, pixinsight=s.pixinsight, pixinsight_ok=os.path.exists(s.pixinsight),
                  frozen=bool(getattr(sys, "frozen", False)), python=sys.version.split()[0], platform=sys.platform)
    try:
        updater.cleanup_after_update()
    except Exception:  # noqa: BLE001
        pass
    try:                                      # installiert: Claude-Eintrag auf diese Datei halten
        from .. import claude_link, install
        if install.is_installed():
            st = claude_link.status()
            if st.get("installed") and not st.get("current"):
                claude_link.install()
                logbook.event("claude_link", action="repoint", files=st.get("files"))
    except Exception:  # noqa: BLE001
        pass
    if not os.environ.get("B2_NO_UPDATE_CHECK"):
        updater.status()                      # Prüfung im Hintergrund anstoßen
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("Kein freier Port ab %d" % port)
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("astrostack läuft: " + url + "  (dieses Fenster offen lassen – Schließen beendet das Programm)", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return httpd


def already_running(port: int) -> str | None:
    """Läuft schon eine Instanz? Dann nur den Browser öffnen statt einen zweiten Server zu starten."""
    import urllib.request
    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url + "api/state", timeout=1.5) as r:
            if json.loads(r.read()).get("version"):
                return url
    except Exception:  # noqa: BLE001
        return None
    return None


def main(port: int = 8765, open_browser: bool = True):
    url = already_running(port)
    if url:
        print("astrostack läuft bereits: " + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    httpd = serve(port, open_browser)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
