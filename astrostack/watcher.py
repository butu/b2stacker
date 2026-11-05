"""Watcher: Quellordner und Projektordner regelmäßig auf Änderungen prüfen (Konzept: Ordner & Watcher).

Nur Standardbibliothek, ohne ``watchdog``: ein Hintergrund-Thread schaut alle ``interval`` Sekunden (Standard 45)
nach. Billig, weil keine Datei geöffnet und keine Bilddatei einzeln abgefragt wird – nur die Ordner selbst:
Name, Änderungszeit und Anzahl der Einträge je Unterordner (die Änderungszeit eines Ordners ändert sich, wenn darin
eine Datei dazukommt, verschwindet oder umbenannt wird – unter Windows/NTFS, SMB und Linux gleich).

- Quellordner geändert → ``core.refresh`` für das Projekt (der bekannte automatische Ablauf, doppelte Anstöße
  werden zusammengelegt). Entprellt: erst wenn sich der Ordner zwei Durchgänge lang nicht mehr ändert, also nicht
  mitten im Kopieren und nicht bei jedem Bild, das NINA während der Nacht schreibt.
- Projektordner geändert → neue Bilder in die Galerie (``gallery.scan_project_dir``), nicht während gestackt wird.
- Offline-Quellen (NAS aus, Platte getrennt) kosten nichts: jeder Ordner wird mit kurzer Frist geprüft; wer nicht
  antwortet, wird einige Durchgänge lang übersprungen.
- Der erste Durchgang merkt sich nur den Stand (beim Start prüft ``refresh_all`` ohnehin alles).
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import gallery

DEFAULT_INTERVAL = 45.0
PROBE_TIMEOUT = 2.0          # so lange darf ein Quellordner für die erste Antwort brauchen
OFFLINE_SKIP = 4             # danach so viele Durchgänge Pause


def signature(root: str, skip=None, max_dirs: int = 20000) -> tuple | None:
    """Fingerabdruck eines Ordnerbaums aus Ordner-Zeitstempeln und Eintragszahlen. None = nicht erreichbar."""
    skip = {s.lower() for s in (skip or ())}
    out = []
    stack = [root]
    try:
        while stack and len(out) < max_dirs:
            d = stack.pop()
            try:
                st = os.stat(d)
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                if d == root:
                    return None
                continue
            out.append((d, st.st_mtime_ns, len(entries)))
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False) and e.name.lower() not in skip and not e.name.startswith("."):
                        stack.append(e.path)
                except OSError:
                    pass
    except OSError:
        return None
    return tuple(sorted(out))


class Watcher:
    """Hintergrund-Thread; ``get_core`` liefert die aktuelle ``Core`` (sie kann wechseln, z. B. nach einer
    Wiederherstellung). ``tick()`` ist ein einzelner Durchgang (für Tests ohne Thread)."""

    def __init__(self, get_core, interval: float = DEFAULT_INTERVAL):
        self.get_core = get_core
        self.interval = max(0.05, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[tuple, tuple | None] = {}       # (art, schlüssel) → letzter Fingerabdruck
        self._pending: dict[tuple, tuple | None] = {}    # geändert, wartet auf Ruhe
        self._skip: dict[str, int] = {}                  # offline: Pfad → noch zu überspringende Durchgänge
        self._core_id = None
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="watch-probe")
        self.ticks = 0
        self.triggered: list[tuple] = []                 # (art, Projekt) – für Tests und Protokoll

    # ------------------------------------------------------------ Thread
    def start(self) -> "Watcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="watcher", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
        self._pool.shutdown(wait=False, cancel_futures=True)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 – der Watcher darf nie sterben
                try:
                    from . import logbook
                    logbook.error("Watcher")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    self.get_core().db.release()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(self.interval)

    # ------------------------------------------------------------ ein Durchgang
    def _probe(self, path: str, skip=None) -> tuple | None | bool:
        """Fingerabdruck mit Frist; False = übersprungen (offline, wird später wieder versucht)."""
        if self._skip.get(path, 0) > 0:
            self._skip[path] -= 1
            return False
        fut = self._pool.submit(signature, path, skip)
        try:
            sig = fut.result(timeout=PROBE_TIMEOUT)
        except FutureTimeout:
            self._skip[path] = OFFLINE_SKIP           # hängt (Netzlaufwerk weg): eine Weile in Ruhe lassen
            return False
        if sig is None:
            self._skip[path] = 1                       # nicht da: nächsten Durchgang auslassen
        return sig

    def _changed(self, key: tuple, sig) -> bool:
        """Entprellt: True, wenn sich der Stand geändert hat und seit dem letzten Durchgang ruhig ist."""
        if key not in self._seen:
            self._seen[key] = sig
            return False
        if sig == self._seen[key]:
            self._pending.pop(key, None)
            return False
        if key in self._pending and self._pending[key] == sig:
            self._pending.pop(key)
            self._seen[key] = sig
            return True
        self._pending[key] = sig
        return False

    def tick(self) -> list[tuple]:
        """Einmal alle Projekte prüfen. → ausgelöste Aktionen [(art, projekt_id)]"""
        core = self.get_core()
        if self._core_id != id(core):                   # neue Core (z. B. nach Wiederherstellung): neu anfangen
            self._seen.clear()
            self._pending.clear()
            self._core_id = id(core)
        self.ticks += 1
        done: list[tuple] = []
        projects = core.db.query("SELECT id, folder FROM projects ORDER BY id")
        for p in projects:
            if self._stop.is_set():
                break
            pid = int(p["id"])
            busy = core.tasks.active(pid)
            # Quellordner
            folders = core.db.query("SELECT id, path, online FROM source_folders WHERE project_id = ?", (pid,))
            src_changed = False
            for f in folders:
                sig = self._probe(f["path"])
                if sig is False:
                    continue
                if self._changed(("src", pid, f["path"]), sig):
                    src_changed = True
                elif sig is not None and not f["online"] and ("src", pid, f["path"]) in self._seen:
                    src_changed = True                 # war offline, ist wieder da
                    self._seen[("src", pid, f["path"])] = sig
            if src_changed:
                core.refresh(pid, reason="neue oder geänderte Dateien")
                done.append(("refresh", pid))
            # Projektordner (Galerie)
            pdir = core.ws.project_dir(p["folder"])
            sig = self._probe(pdir, skip=gallery.SKIP_DIRS)
            if sig is False:
                continue
            key = ("proj", pid, p["folder"])
            if self._changed(key, sig):
                if any(t.lane == "stack" for t in busy):
                    self._seen[key] = None              # nach dem Stacken noch einmal ansehen
                    continue
                res = gallery.scan_project_dir(core.db, core.ws, pid)
                if res["new"] or res["back"] or res["missing"]:
                    core._notify("project", id=pid)
                    core._notify("projects")
                done.append(("gallery", pid))
        self.triggered += done
        return done
