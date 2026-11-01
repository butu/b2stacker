"""Ordner scannen: alle FITS/XISF finden, Header übersetzen, Lights und Kalibriersätze gruppieren."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import fitsmeta as fm

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


def _norm_target(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def find_files(roots: list[str], opt: ScanOptions) -> list[tuple[str, bool]]:
    """→ [(pfad, ausgeschlossen)]"""
    out = []
    seen = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            low = os.path.basename(dirpath).lower()
            dirnames[:] = [d for d in dirnames if d.lower() not in opt.skip_dirs and not d.startswith(".")]
            rel_parts = [p.lower() for p in os.path.relpath(dirpath, root).split(os.sep)]
            excluded = any(any(p.startswith(x) or x in p for x in opt.exclude_dirs) for p in rel_parts) or \
                any(x in low for x in opt.exclude_dirs)
            for fn in filenames:
                if fn.lower().endswith(fm.FITS_EXT + fm.XISF_EXT):
                    p = os.path.abspath(os.path.join(dirpath, fn))
                    if p not in seen:
                        seen.add(p)
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
    files = find_files(roots, opt)
    metas, errors = [], []

    def work(item):
        path, excl = item
        try:
            m = fm.read_meta(path, opt.custom_filters)
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
            key = (_norm_target(target), m.filter, m.settings_key())
            g = lights.get(key)
            if g is None:
                g = lights[key] = LightGroup(target, m.filter, m.filter_kind, m.is_osc, m.settings_key())
            g.frames.append(m)
        elif m.type == fm.FLAT:
            flat_pool.setdefault((m.filter, m.settings_key(), os.path.dirname(m.path)), []).append(m)
        elif m.type in (fm.BIAS, fm.DARK, fm.DARKFLAT):
            exp = round(m.exposure, 3) if m.exposure is not None else None
            folder = os.path.dirname(m.path)
            key = (m.type, None, m.settings_key(), folder, exp if m.type in (fm.DARK, fm.DARKFLAT) else None)
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
                         session=(sess[0].date_obs or sess[0].date_loc or "")[:19])
            calib[(fm.FLAT, flt, settings, folder, c.session, exp)] = c
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
