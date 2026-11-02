"""Ordner scannen: alle FITS/XISF finden, Header übersetzen, Lights und Kalibriersätze gruppieren."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import fitsmeta as fm
from .catalog import canonical, open_catalog

DEFAULT_SKIP_DIRS = ("work", "final", "_claude", "$recycle.bin", ".git", "__pycache__")
# NINA-Snapshot-Ordner: Test-Lights ignorieren, Kalibrierframes (z. B. Bias) aber verwenden
SNAPSHOT_DIRS = ("snapshot", "snapshots")
FLAT_SESSION_GAP_MIN = 10        # neue Flat-Sitzung nach so vielen Minuten Pause …
FLAT_SESSION_EXP_JUMP = 2.5      # … oder wenn die Belichtung sprunghaft wechselt (Panel-Serien)
DEFAULT_EXCLUDE_DIRS = ("_rejected", "_verworfen", "_alt", "_bad", "rejected")


@dataclass
class ScanOptions:
    skip_dirs: tuple[str, ...] = DEFAULT_SKIP_DIRS
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDE_DIRS   # werden gelesen, aber als ausgeschlossen markiert
    custom_filters: dict[str, str] = field(default_factory=dict)
    workers: int = 8
    target_map: dict[str, str] = field(default_factory=dict)   # Ziel-Schlüssel → Projekt (vom Benutzer bestätigt)
    catalog_dir: str | None = None


# Katalog-Präfixe: verschiedene Schreibweisen → ein Schlüssel ("Sh2-82", "Sh 2-82", "SH2_82" → "sh2-82")
_CATALOGS = [
    (r"(?:m|messier)", "m"), (r"ngc", "ngc"), (r"ic", "ic"), (r"(?:sh\s*2|sharpless)", "sh2-"),
    (r"(?:vdb|van\s*den\s*bergh)", "vdb"), (r"ldn", "ldn"), (r"lbn", "lbn"), (r"(?:abell|ab|pn\s*a66)", "abell"),
    (r"(?:c|caldwell)", "c"), (r"(?:b|barnard)", "b"), (r"ced", "ced"), (r"rcw", "rcw"), (r"gum", "gum"),
    (r"(?:cr|collinder)", "cr"), (r"(?:mel|melotte)", "mel"), (r"ugc", "ugc"), (r"pgc", "pgc"), (r"arp", "arp"),
    (r"(?:hcg|hickson)", "hcg"), (r"lynds", "ldn"), (r"dwb", "dwb"), (r"(?:vdb-?ha|vdbh)", "vdbha"),
]


def target_key(name: str | None) -> str:
    """Zielname vereinheitlichen: Groß/klein, Leerzeichen, Bindestriche, führende Nullen, Katalog-Schreibweisen.
    Zusätze wie „Panel 2“ bleiben erhalten (Mosaik-Felder sind eigene Projekte)."""
    n = (name or "").strip().lower()
    n = re.sub(r"[_\-.,;:]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    for pat, pre in _CATALOGS:
        m = re.match(r"^%s\s*0*(\d+)([a-z]?)\b(.*)$" % pat, n)
        if m:
            rest = re.sub(r"\s+", " ", m.group(3)).strip()
            return pre + m.group(1) + m.group(2) + ((" " + rest) if rest else "")
    return n.replace(" ", "")


def _norm_target(name: str | None) -> str:
    return target_key(name)


def normalize_roots(roots) -> list[str]:
    """Canonical roots (realpath, no trailing separator); duplicates removed."""
    out, seen = [], set()
    for r in roots or []:
        if not r:
            continue
        c = os.path.realpath(os.path.expanduser(str(r)))
        k = os.path.normcase(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def find_files(roots: list[str], opt: ScanOptions) -> list[tuple[str, bool]]:
    """→ [(path, excluded)]. Files reached via overlapping roots are kept once, keyed by canonical path."""
    out = []
    seen = set()
    for root in normalize_roots(roots):
        for dirpath, dirnames, filenames in os.walk(root):
            low = os.path.basename(dirpath).lower()
            dirnames[:] = [d for d in dirnames if d.lower() not in opt.skip_dirs and not d.startswith(".")]
            rel_parts = [p.lower() for p in os.path.relpath(dirpath, root).split(os.sep)]
            excluded = any(any(p.startswith(x) or x in p for x in opt.exclude_dirs) for p in rel_parts) or \
                any(x in low for x in opt.exclude_dirs)
            for fn in filenames:
                if fn.lower().endswith(fm.FITS_EXT + fm.XISF_EXT):
                    p = os.path.abspath(os.path.join(dirpath, fn))
                    ck = canonical(p)
                    if ck not in seen:
                        seen.add(ck)
                        out.append((p, excluded))
    return sorted(out)


@dataclass
class LightGroup:
    target: str
    filter: str | None
    filter_kind: str
    is_osc: bool
    settings: tuple
    frames: list = field(default_factory=list)
    names: dict = field(default_factory=dict)          # gefundene Schreibweisen → Anzahl

    def nights(self) -> dict[str, list]:
        d = defaultdict(list)
        for f in self.frames:
            d[f.night or "?"].append(f)
        return dict(sorted(d.items()))

    def summary(self) -> dict:
        used = [f for f in self.frames if not f.excluded]
        return dict(target=self.target, filter=self.filter, filter_kind=self.filter_kind, is_osc=self.is_osc,
                    camera=self.settings[0], gain=self.settings[1], offset=self.settings[2], readout=self.settings[3],
                    binning=self.settings[4], size=[self.settings[5], self.settings[6]],
                    frames=len(self.frames), excluded=len(self.frames) - len(used),
                    hours=round(sum(f.exposure or 0 for f in used) / 3600, 2),
                     hours_total=round(sum(f.exposure or 0 for f in self.frames) / 3600, 2),
                    nights={n: len(v) for n, v in self.nights().items()})


@dataclass
class CalibSet:
    type: str
    filter: str | None
    settings: tuple
    folder: str
    exposure: float | None
    frames: list = field(default_factory=list)
    session: str = ""                 # Startzeit der Sitzung (Flats)

    @property
    def id(self) -> str:
        return "%s|%s|%s" % (self.folder, self.exposure, self.session)

    def summary(self) -> dict:
        fr = self.frames
        exps = [f.exposure for f in fr if f.exposure is not None]
        def rng(attr):
            vals = [getattr(f, attr) for f in fr if getattr(f, attr) is not None]
            return [min(vals), max(vals)] if vals else None
        return dict(type=self.type, filter=self.filter, folder=self.folder, frames=len(fr),
                    excluded=sum(1 for f in fr if f.excluded), is_master=any(f.is_master for f in fr),
                    camera=self.settings[0], gain=self.settings[1], offset=self.settings[2], readout=self.settings[3],
                    binning=self.settings[4], size=[self.settings[5], self.settings[6]], exposure=self.exposure,
                    date=rng("date_loc") or rng("date_obs"), ccd_temp=rng("ccd_temp"), focus_pos=rng("focus_pos"),
                    rotator=rng("rotator"), type_reasons=sorted({f.type_reason for f in fr}),
                    warnings=sorted({w for f in fr for w in f.warnings}), id=self.id, session=self.session,
                    exposure_range=[min(exps), max(exps)] if exps else None,
                    sky=bool(exps and max(exps) > 1.15 * min(exps)))


@dataclass
class Inventory:
    roots: list[str]
    lights: list[LightGroup]
    calib: list[CalibSet]
    unknown: list
    errors: list

    def summary(self) -> dict:
        return dict(roots=self.roots, lights=[g.summary() for g in self.lights],
                    calib=[c.summary() for c in self.calib],
                    unknown=[u.path for u in self.unknown], errors=self.errors)


def scan(roots: list[str], opt: ScanOptions | None = None, progress=None) -> Inventory:
    opt = opt or ScanOptions()
    roots = normalize_roots(roots)
    files = find_files(roots, opt)
    metas, errors = [], []
    cat = open_catalog(opt.catalog_dir) if opt.catalog_dir else None

    def work(item):
        path, excl = item
        try:
            # Raw header may come from cache; type/filter/folder are translated fresh (context-dependent).
            raw = cat.header(path) if cat else fm.read_header(path)
            m = fm.finalize(fm.from_header(raw, path), opt.custom_filters)
            m.excluded = excl
            return m, None
        except Exception as e:  # defekte Dateien nicht abbrechen lassen
            return None, (path, str(e))

    with ThreadPoolExecutor(max_workers=opt.workers) as ex:
        for i, (m, err) in enumerate(ex.map(work, files)):
            if m:
                metas.append(m)
            else:
                errors.append(err)
            if progress:
                progress(i + 1, len(files))
    if cat:
        cat.flush()

    lights: dict[tuple, LightGroup] = {}
    calib: dict[tuple, CalibSet] = {}
    flat_pool: dict[tuple, list] = {}
    unknown = []
    for m in metas:
        snap = any(p.lower() in SNAPSHOT_DIRS for p in os.path.dirname(m.path).split(os.sep))
        if m.type == fm.LIGHT and not m.is_master:
            if snap:                       # Testaufnahmen aus dem Snapshot-Ordner nie stacken
                continue
            target = m.object or _guess_target(m.path, roots)
            tk = target_key(target)
            target = opt.target_map.get(tk, target)            # bestätigte Zuordnung (z. B. zusammengelegt)
            key = (target_key(target), m.filter, m.settings_key())
            g = lights.get(key)
            if g is None:
                g = lights[key] = LightGroup(target, m.filter, m.filter_kind, m.is_osc, m.settings_key())
            g.frames.append(m)
            g.names[m.object or target] = g.names.get(m.object or target, 0) + 1
        elif m.type == fm.FLAT and m.is_master:            # fertiger Master: eigener Satz je Datei
            c = CalibSet(fm.FLAT, m.filter, m.settings_key(), os.path.dirname(m.path),
                         round(m.exposure, 3) if m.exposure is not None else None, [m], session="Master")
            calib[("master", m.path)] = c
        elif m.type == fm.FLAT:
            flat_pool.setdefault((m.filter, m.settings_key(), os.path.dirname(m.path)), []).append(m)
        elif m.type in (fm.BIAS, fm.DARK, fm.DARKFLAT):
            exp = round(m.exposure, 3) if m.exposure is not None else None
            folder = os.path.dirname(m.path)
            key = (m.type, None, m.settings_key(), folder, exp if m.type in (fm.DARK, fm.DARKFLAT) else None,
                   m.path if m.is_master else None)
            c = calib.get(key)
            if c is None:
                c = calib[key] = CalibSet(m.type, None, m.settings_key(), folder, exp)
            c.frames.append(m)
        else:
            unknown.append(m)
    for (flt, settings, folder), frames in flat_pool.items():
        for sess in flat_sessions(frames):
            exp = _median_exp(sess)
            c = CalibSet(fm.FLAT, flt, settings, folder, exp, list(sess),
                         session=(sess[0].date_loc or sess[0].date_obs or "")[:19])
            calib[(fm.FLAT, flt, settings, folder, c.session, exp)] = c
    # gleiche Ziele mit verschiedener Schreibweise: einheitlicher Anzeigename (häufigste Schreibweise)
    by_key: dict[str, dict] = {}
    for g in lights.values():
        d = by_key.setdefault(target_key(g.target), {})
        for n, c in g.names.items():
            d[n] = d.get(n, 0) + c
    for g in lights.values():
        if g.target in opt.target_map.values():
            continue
        names = by_key.get(target_key(g.target)) or {}
        if names:
            g.target = max(names.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    for g in lights.values():
        g.frames.sort(key=lambda f: f.date_obs or "")
    for c in calib.values():
        c.frames.sort(key=lambda f: f.date_obs or "")
    return Inventory(roots=list(roots), lights=sorted(lights.values(), key=lambda g: (g.target.lower(), g.filter or "")),
                     calib=sorted(calib.values(), key=lambda c: (c.type, c.filter or "", c.folder)),
                     unknown=unknown, errors=errors)


def flat_sessions(frames: list) -> list[list]:
    """Flats eines Ordners in Sitzungen teilen: nach Zeitlücken oder Belichtungssprüngen.

    Himmelsflats (NINA Sky-Flat-Assistent) haben pro Frame eine andere Belichtung, weil die Dämmerung
    heller/dunkler wird – sie gehören trotzdem zusammen. Panel-Serien mit 0,006 s und 0,062 s nicht.
    """
    fr = sorted(frames, key=lambda f: f.date_obs or f.date_loc or "")
    out, cur = [], []
    import datetime as _dt

    def t(f):
        s = (f.date_obs or f.date_loc or "")[:19]
        try:
            return _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    for f in fr:
        if cur:
            p = cur[-1]
            tp, tf = t(p), t(f)
            gap = (tf - tp).total_seconds() / 60 if (tp and tf) else 0
            e1, e2 = p.exposure or 0, f.exposure or 0
            jump = (max(e1, e2) / min(e1, e2)) if (e1 > 0 and e2 > 0) else 1
            if gap > FLAT_SESSION_GAP_MIN or jump > FLAT_SESSION_EXP_JUMP:
                out.append(cur)
                cur = []
        cur.append(f)
    if cur:
        out.append(cur)
    return out


def _median_exp(frames):
    v = sorted(f.exposure for f in frames if f.exposure is not None)
    return round(v[len(v) // 2], 3) if v else None


def _guess_target(path: str, roots: list[str]) -> str:
    """Ohne OBJECT-Header: ersten Ordner unterhalb der Wurzel nehmen, der kein Datum/Typ/Filter ist."""
    for r in roots:
        if path.startswith(os.path.abspath(r)):
            rel = os.path.relpath(os.path.dirname(path), r).split(os.sep)
            for p in rel:
                if p in (".", "") or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", p) or fm.type_from_string(p)[0] != fm.UNKNOWN:
                    continue
                if p.lower() in ("rgb", "hao3", "l", "r", "g", "b", "ha", "oiii", "sii"):
                    continue
                return p
            return os.path.basename(os.path.abspath(r))
    return "unbenannt"
