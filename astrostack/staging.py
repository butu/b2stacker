"""Dateien für PixInsight bereitstellen – an genau einer Stelle.

WBPP bekommt alle Bilder über die Kommandozeile (``-r="WBPP.js,arg,arg,…"``). Daraus folgen drei Grenzen:

* **Komma** im Pfad trennt Argumente – PixInsight kennt kein Escaping.
* **Länge**: Windows erlaubt rund 32 000 Zeichen; ``dir=`` geht nur, wenn ein Ordner genau die gewählten Bilder enthält.
* **Flat-Gruppen** erkennt WBPP am Kennwort ``ASFLAT_<n>`` im Pfad (Grouping Keyword).

Nur wenn eine dieser Grenzen greift, legt b2stacker eigene Ordner unter ``<lauf>/_grp`` an. Jede Datei darin ist,
in dieser Reihenfolge: ein **Datei-Symlink** (PixInsight liest weiter direkt vom Original, auch vom NAS), ein
**Hardlink** (gleiches lokales Laufwerk) oder eine **Kopie** aus einem Cache, der Kopien vom Netzlaufwerk über
Läufe hinweg wiederverwendet. Originale werden nie verändert; aufgeräumt werden nur diese eigenen Einträge.

Invarianten (getestet): es landen genau die gewählten Lights im Arbeitsordner, kein Pfad an PixInsight enthält
ein Komma, Gruppen ohne gewählte Lights entfallen samt Flats, Gruppen mit demselben Flat-Satz werden zusammengelegt.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field

GROUP_KEYWORD = "ASFLAT"
UNSAFE = ',"'                 # trennt bzw. beendet das -r="…"-Argument; '=' ist ab WBPP 1.9 im Wert erlaubt
CACHE_DAYS = 21


class StagingError(Exception):
    pass


def norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def pi_unsafe(p: str) -> bool:
    return any(c in os.path.abspath(p).replace("\\", "/") for c in UNSAFE)


def is_remote(p: str) -> bool:
    from .engines.pixinsight import is_remote_path
    return is_remote_path(p)


def _key(paths) -> str:
    return hashlib.sha1("|".join(sorted(norm(p) for p in paths)).encode("utf-8")).hexdigest()[:10]


def safe_name(name: str) -> str:
    out = "".join("_" if c in ',="' or ord(c) < 32 else c for c in name).strip()
    return out or "bild"


# ---------------------------------------------------------------- Fähigkeiten des Systems

_SYMLINK_OK: dict[str, bool] = {}


def symlinks_allowed(base: str) -> bool:
    """Darf dieser Benutzer Datei-Symlinks anlegen? (Windows: Entwicklermodus oder Adminrechte)"""
    base = os.path.abspath(base)
    if base in _SYMLINK_OK:
        return _SYMLINK_OK[base]
    ok = False
    try:
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(prefix="symtest_", dir=base)
        src, dst = os.path.join(d, "a"), os.path.join(d, "b")
        open(src, "w").close()
        try:
            os.symlink(src, dst)
            ok = os.path.exists(dst)
        except (OSError, NotImplementedError, AttributeError):
            ok = False
        for p in (dst, src):
            try:
                os.unlink(p)
            except OSError:
                pass
        os.rmdir(d)
    except OSError:
        ok = False
    _SYMLINK_OK[base] = ok
    return ok


def long_paths_enabled() -> bool | None:
    """Windows: LongPathsEnabled in der Registry (None = unbekannt/anderes System)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except OSError:
        return False


# ---------------------------------------------------------------- Plan: was wird wie bereitgestellt

@dataclass
class Group:
    key: str
    lights: list
    flats: list
    nights: list = field(default_factory=list)
    flat_set: str | None = None


@dataclass
class Layout:
    lights: list                               # Pfade, die PixInsight bekommt (Reihenfolge = Auswahl)
    calib: dict                                # bias/darks/darkflats/flats → Pfade für PixInsight
    reference: str | None
    grouping: bool = False
    groups: list = field(default_factory=list)
    stats: dict = field(default_factory=lambda: dict(symlink=0, hardlink=0, copy=0, cached=0, direct=0, bytes_copied=0,
                                                       remote=0))
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        s = self.stats
        staged = s["symlink"] + s["hardlink"] + s["copy"] + s["cached"]
        if not staged:
            return "Alle Dateien gehen direkt an PixInsight (keine Verknüpfung oder Kopie nötig)."
        parts = []
        if s["symlink"]:
            parts.append("%d verknüpft" % s["symlink"])
        if s["hardlink"]:
            parts.append("%d als Hardlink" % s["hardlink"])
        if s["cached"]:
            parts.append("%d aus dem Kopie-Cache" % s["cached"])
        if s["copy"]:
            parts.append("%d kopiert (%.1f GB)" % (s["copy"], s["bytes_copied"] / 1024 ** 3))
        return "Bereitgestellt: %s; %d direkt." % (", ".join(parts), s["direct"])


def plan_groups(run: dict, lights: list) -> list[Group]:
    """Flat-Gruppen auf die Auswahl beschränken und Gruppen mit gleichen Flats zusammenlegen."""
    sel = {norm(p): p for p in lights}
    out: list[Group] = []
    by_flats: dict[tuple, Group] = {}
    for g in run.get("flat_groups") or []:
        gl = [sel[norm(p)] for p in g.get("lights", []) if norm(p) in sel]
        if not gl:
            continue                                  # keine gewählten Lights → Gruppe samt Flats entfällt
        fk = tuple(sorted(norm(p) for p in g.get("flats", [])))
        if fk in by_flats:
            m = by_flats[fk]
            m.lights += gl
            m.nights = sorted(set(m.nights) | set(g.get("nights") or []))
            continue
        grp = Group(key=str(g.get("key", len(out) + 1)), lights=gl, flats=list(g.get("flats", [])),
                    nights=list(g.get("nights") or []), flat_set=g.get("flat_set"))
        by_flats[fk] = grp
        out.append(grp)
    covered = {norm(p) for g in out for p in g.lights}
    rest = [p for p in lights if norm(p) not in covered]
    if out and rest:                                  # Lights ohne Gruppe (z. B. neue Nacht): zur ersten Gruppe
        out[0].lights += rest
    for i, g in enumerate(out, 1):                    # fortlaufende Kennwörter
        g.key = str(i)
    return out


# ---------------------------------------------------------------- Ausführung

class Stager:
    def __init__(self, jobdir: str, cache_root: str, log=None, allow_symlinks: bool | None = None):
        self.base = os.path.join(jobdir, "_grp")
        self.cache = os.path.join(cache_root, "staged")
        self.log = log or (lambda *_a: None)
        self.allow_symlinks = symlinks_allowed(jobdir) if allow_symlinks is None else allow_symlinks
        self.stats = dict(symlink=0, hardlink=0, copy=0, cached=0, direct=0, bytes_copied=0, remote=0)

    def _cache_copy(self, src: str) -> str:
        st = os.stat(src)
        h = hashlib.sha1(("%s|%d|%d" % (norm(src), st.st_size, int(st.st_mtime))).encode("utf-8")).hexdigest()[:16]
        dst = os.path.join(self.cache, h[:2], h + "_" + safe_name(os.path.basename(src)))
        if os.path.exists(dst) and os.path.getsize(dst) == st.st_size:
            self.stats["cached"] += 1
            os.utime(dst)                              # zuletzt benutzt (für das Aufräumen nach Alter)
            return dst
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise StagingError("Datei konnte nicht kopiert werden: %s (%s). Ist das Netzlaufwerk erreichbar? Notlösung: "
                               "Daten auf eine lokale Platte kopieren." % (src, e)) from e
        self.stats["copy"] += 1
        self.stats["bytes_copied"] += st.st_size
        return dst

    def place(self, src: str, dst: str) -> str:
        """Eine Datei unter dst erreichbar machen: Symlink → Hardlink → (Cache-)Kopie."""
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            return dst
        if is_remote(src):
            self.stats["remote"] += 1
        if self.allow_symlinks:
            try:
                os.symlink(os.path.abspath(src), dst)
                if os.path.exists(dst):
                    self.stats["symlink"] += 1
                    return dst
                os.unlink(dst)
            except (OSError, NotImplementedError):
                pass
        try:
            os.link(src, dst)
            self.stats["hardlink"] += 1
            return dst
        except OSError:
            pass
        cached = self._cache_copy(src)
        try:
            os.link(cached, dst)                       # Cache liegt im Arbeitsverzeichnis → meist gleiches Laufwerk
        except OSError:
            shutil.copy2(cached, dst)
        return dst

    def place_set(self, paths: list, folder: str) -> list:
        """Mehrere Dateien in einen eigenen Ordner; Namen ohne Sonderzeichen, Kollisionen durchnummeriert."""
        out, used = [], set()
        for p in paths:
            n = safe_name(os.path.basename(p))
            if n.lower() in used:
                stem, ext = os.path.splitext(n)
                k = 2
                while ("%s_%d%s" % (stem, k, ext)).lower() in used:
                    k += 1
                n = "%s_%d%s" % (stem, k, ext)
            used.add(n.lower())
            out.append(self.place(p, os.path.join(folder, n)))
        return out


def prepare(run: dict, calib: dict, reference: str | None, jobdir: str, cache_root: str, max_cmdline: int,
            log=None, allow_symlinks: bool | None = None, cmd_len=None) -> Layout:
    """Pfade für WBPP festlegen. Ohne Notwendigkeit bleiben Originale unangetastet (PixInsight liest direkt)."""
    log = log or (lambda *_a: None)
    lights = list(run["lights"])
    groups = plan_groups(run, lights)
    grouping = len(groups) > 1
    stg = Stager(jobdir, cache_root, log, allow_symlinks)
    lay = Layout(lights=lights, calib={k: list(v or []) for k, v in calib.items()}, reference=reference,
                 grouping=grouping, groups=groups)
    if run.get("flat_groups") and not grouping:
        n = len([g for g in run.get("flat_groups") or [] if g.get("lights")])
        if n > 1:
            lay.notes.append("Flat-Gruppen: alle gewählten Lights nutzen denselben Flat-Satz – kein Grouping nötig.")
    if len(groups) == 1:                              # nur die Flats der verbleibenden Gruppe
        lay.calib["flats"] = list(groups[0].flats)
    if grouping:
        new_lights, new_flats = [], []
        for g in groups:
            gdir = os.path.join(stg.base, "%s_%s" % (GROUP_KEYWORD, g.key))
            new_lights += stg.place_set(g.lights, os.path.join(gdir, "L"))
            new_flats += stg.place_set(g.flats, os.path.join(gdir, "F"))
        # Reihenfolge der Auswahl beibehalten (für Abbildungen Light → Original)
        lay.lights, lay.calib["flats"] = new_lights, new_flats
    # Sonderzeichen im Pfad: betroffene Sätze in eigene Ordner (gleiche Datei nur einmal)
    for kind in ("bias", "darks", "darkflats", "flats"):
        ps = lay.calib.get(kind) or []
        if ps and any(pi_unsafe(p) for p in ps) and not (grouping and kind == "flats"):
            lay.calib[kind] = stg.place_set(ps, os.path.join(stg.base, "%s_%s" % (kind, _key(ps))))
    if not grouping and any(pi_unsafe(p) for p in lay.lights):
        lay.lights = stg.place_set(lay.lights, os.path.join(stg.base, "lights_" + _key(lay.lights)))
    if reference and pi_unsafe(reference):
        lay.reference = stg.place(reference, os.path.join(stg.base, "ref", safe_name(os.path.basename(reference))))
    # Kommandozeile zu lang: Lights in einen Ordner, der genau die Auswahl enthält (→ ein dir=-Argument)
    if cmd_len and cmd_len(lay) > max_cmdline and not grouping:
        lay.lights = stg.place_set(lay.lights, os.path.join(stg.base, "lights_" + _key(lay.lights)))
        lay.notes.append("Sehr viele Einzeldateien – Lights über einen eigenen Ordner übergeben.")
    staged = stg.stats["symlink"] + stg.stats["hardlink"] + stg.stats["copy"] + stg.stats["cached"]
    total = len(lay.lights) + sum(len(v or []) for v in lay.calib.values()) + (1 if lay.reference else 0)
    stg.stats["direct"] = max(0, total - staged)
    lay.stats = stg.stats
    if stg.stats["copy"] and not stg.allow_symlinks:
        lay.notes.append("%d Dateien mussten kopiert werden (%.1f GB). Mit dem Windows-Entwicklermodus "
                         "(Einstellungen → System → Für Entwickler) verknüpft b2stacker stattdessen – ohne Kopie."
                         % (stg.stats["copy"], stg.stats["bytes_copied"] / 1024 ** 3))
    for p in lay.lights + [x for v in lay.calib.values() for x in (v or [])]:
        if pi_unsafe(p):                               # letzte Sicherung vor PixInsight
            raise StagingError("Pfad mit Sonderzeichen konnte nicht ersetzt werden: " + p)
    return lay


def prune_cache(cache_root: str, days: int = CACHE_DAYS) -> int:
    """Kopien vom Netzlaufwerk, die länger nicht benutzt wurden, löschen (nur eigene Cache-Dateien)."""
    import time
    root = os.path.join(cache_root, "staged")
    if not os.path.isdir(root):
        return 0
    limit, n = time.time() - days * 86400, 0
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                if os.path.getmtime(p) < limit:
                    os.unlink(p)
                    n += 1
            except OSError:
                pass
    return n
