"""Kalibrierdaten prüfen und den Lights zuordnen.

Lehren aus vdB 131 (siehe docs/ERKENNTNISSE.md):
  * Flats zu hell (hellster Kanal > ~48k ADU) → Grün wird nichtlinear gestaucht → runder Farbring.
  * Auslesemodus/Gain/Offset müssen bei Lights, Flats, Bias, Darks identisch sein.
  * Gemischte Serien in einem Ordner (andere Belichtung/Helligkeit) nicht zusammenwerfen.
  * Jeder Filter braucht eigene Flats.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import fitsmeta as fm
from . import imageio

LEVEL = "level"  # good | info | warning | critical


@dataclass
class Thresholds:
    flat_bright_warn: float = 0.66     # Anteil am Vollausschlag (~43k ADU)
    flat_bright_crit: float = 0.73     # ~48k ADU: Knick bei IMX571/ToupTek
    flat_dark_warn: float = 0.06       # dunkelster Kanal am Rand < ~4k ADU → verrauscht
    color_falloff_warn: float = 2.0    # % Farbabfall Mitte→Rand
    color_falloff_crit: float = 4.0
    series_spread_warn: float = 3.0    # % Helligkeitsstreuung innerhalb eines Satzes
    rot_tol: float = 2.0               # Grad
    focus_tol: float = 60.0            # Schritte
    flat_max_age_days: int = 60
    flat_min_frames: int = 5           # darunter unbrauchbar (Rauschen des Master-Flats)
    flat_good_frames: int = 15
    dark_temp_tol: float = 2.0
    sample_frames: int = 12            # pro Satz höchstens so viele Frames vermessen


@dataclass
class Finding:
    level: str
    title: str
    text: str
    ref: str = ""

    def to_dict(self):
        return dict(level=self.level, title=self.title, text=self.text, ref=self.ref)


# ----------------------------------------------------------------------------- Flats

def flat_frame_stats(path: str, bayer: str | None, data: bytes | None = None) -> dict:
    a = imageio.load(path, data)
    if a.ndim == 3:                                 # bereits debayerter Master (3 Kanäle)
        ch = {"R": a[0], "G": a[1], "B": a[2]} if a.shape[0] == 3 else {"L": a[0]}
        a = a.mean(axis=0)
    else:
        ch = imageio.split_cfa(a, bayer)
    any_ch = next(iter(ch.values()))
    r = imageio.radius_map(any_ch.shape, 4)
    c, e = r < 0.15, r > 0.85
    out = {}
    for k, v in ch.items():
        s = v[::4, ::4]
        out[k] = dict(center=float(np.median(s[c])), edge=float(np.median(s[e])), p999=float(np.percentile(s, 99.9)))
    res = dict(path=path, channels=out,
               brightest=max(v["center"] for v in out.values()),
               peak=max(v["p999"] for v in out.values()),
               darkest_edge=min(v["edge"] for v in out.values()),
               mean=float(a.mean()))
    if "G" in out:
        g = out["G"]
        for k in ("R", "B"):
            res["falloff_" + k] = ((out[k]["edge"] / g["edge"]) / (out[k]["center"] / g["center"]) - 1) * 100
        res["vignetting"] = g["edge"] / g["center"]
    else:
        res["vignetting"] = out["L"]["edge"] / out["L"]["center"]
    return res


def check_flat_set(cs, th: Thresholds, stats_cache: dict | None = None) -> dict:
    """Vermisst (Stichprobe der) Frames eines Flat-Satzes und bewertet ihn."""
    frames = [f for f in cs.frames if not f.excluded]
    if not frames:
        return dict(verdict="critical", findings=[Finding("critical", "Leerer Satz", "Alle Frames ausgeschlossen").to_dict()])
    step = max(1, len(frames) // th.sample_frames)
    sample = frames[::step][: th.sample_frames]
    stats = []
    for f in sample:
        key = f.path
        if stats_cache is not None and key in stats_cache:
            stats.append(stats_cache[key])
            continue
        try:
            s = flat_frame_stats(f.path, f.bayer)
        except Exception as ex:  # defekte Datei
            s = dict(path=f.path, error=str(ex))
        stats.append(s)
        if stats_cache is not None:
            stats_cache[key] = s
    ok = [s for s in stats if "error" not in s]
    fs: list[Finding] = []
    if not ok:
        return dict(verdict="critical", frames=stats, findings=[Finding("critical", "Flats nicht lesbar", "").to_dict()])
    fsc = imageio.FULL_SCALE
    peak = max(s["peak"] for s in ok)
    bright = float(np.median([s["brightest"] for s in ok]))
    dark_edge = min(s["darkest_edge"] for s in ok)
    if peak > th.flat_bright_crit * fsc:
        fs.append(Finding("critical", "Flats zu hell",
                          "Hellster Kanal erreicht %.0fk ADU (Knick bei ~%.0fk). Der Kanal wird gestaucht → runder Farbring im Stack. "
                          "Neue Flats mit ca. 35 %% Zielwert aufnehmen." % (peak / 1000, th.flat_bright_crit * fsc / 1000)))
    elif peak > th.flat_bright_warn * fsc:
        fs.append(Finding("warning", "Flats knapp unter dem Knick",
                          "Hellster Kanal %.0fk ADU – brauchbar, aber besser unter %.0fk bleiben." % (peak / 1000, th.flat_bright_warn * fsc / 1000)))
    if dark_edge < th.flat_dark_warn * fsc:
        fs.append(Finding("warning", "Flats am Rand sehr dunkel",
                          "Dunkelster Kanal am Rand nur %.1fk ADU – mehr Frames aufnehmen oder etwas heller belichten." % (dark_edge / 1000)))
    fall = []
    for k in ("falloff_R", "falloff_B"):
        vals = [s[k] for s in ok if k in s]
        if vals:
            fall.append((k[-1], float(np.median(vals))))
    worst = max((abs(v) for _, v in fall), default=0)
    if fall and worst > th.color_falloff_crit:
        fs.append(Finding("critical", "Starker Farbabfall im Flat",
                          "Mitte→Rand: " + ", ".join("%s %+.1f %%" % (k, v) for k, v in fall) + ". Typisch für zu helle Flats oder Streulicht."))
    elif fall and worst > th.color_falloff_warn:
        fs.append(Finding("warning", "Farbabfall im Flat",
                          "Mitte→Rand: " + ", ".join("%s %+.1f %%" % (k, v) for k, v in fall) + "."))
    spread = float(np.std([s["brightest"] for s in ok]) / max(bright, 1) * 100)
    if spread > th.series_spread_warn:
        fs.append(Finding("warning", "Helligkeit schwankt im Satz",
                          "Streuung %.1f %% – vermutlich gemischte Serien oder Panel nicht aufgewärmt." % spread))
    if not fs:
        fs.append(Finding("good", "Flats in Ordnung",
                          "Hellster Kanal %.1fk ADU, Farbabfall %s, Streuung %.1f %%." % (
                              bright / 1000, ", ".join("%s %+.1f %%" % (k, v) for k, v in fall) or "–", spread)))
    verdict = "critical" if any(f.level == "critical" for f in fs) else "warning" if any(f.level == "warning" for f in fs) else "good"
    return dict(verdict=verdict, brightest=bright, peak=peak, darkest_edge=dark_edge, falloff=dict(fall), spread=spread,
                frames=stats, findings=[f.to_dict() for f in fs])


# ----------------------------------------------------------------------------- Zuordnung

def _rot_diff(a, b):
    if a is None or b is None:
        return None
    d = abs((a - b) % 180)
    return min(d, 180 - d)


def _days(a: str | None, b: str | None):
    try:
        return (date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days
    except (TypeError, ValueError):
        return None


def _settings_mismatch(a: tuple, b: tuple) -> list[str]:
    """Harte Abweichungen (Kalibrierframe unbrauchbar). Der Kameraname allein zählt nicht, wenn
    Sensorgröße und alle Einstellungen stimmen – das ist meist nur ein anderer Treiber."""
    names = ["Kamera", "Gain", "Offset", "Auslesemodus", "Binning", "Breite", "Höhe"]
    out = []
    for n, x, y in zip(names, a, b):
        if n == "Kamera":
            continue
        if x != y and not (x in (None, "") or y in (None, "")):
            out.append("%s %s ≠ %s" % (n, y, x))
    if a[0] and b[0] and a[0] != b[0] and (a[5] in (None, "") or b[5] in (None, "")):
        out.append("Kamera %s ≠ %s" % (b[0], a[0]))     # ohne Sensorgröße nicht vergleichbar
    return out


def _camera_note(a: tuple, b: tuple) -> list[str]:
    if a[0] and b[0] and a[0] != b[0] and a[5] not in (None, "") and b[5] not in (None, ""):
        return ["anderer Kameraname (%s), gleiche Sensorgröße und Einstellungen" % b[0]]
    return []


def _median(vals):
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def _assigned_empty_first(cands: list, folder: str | None) -> list:
    """Only the assigned set that is currently completely emptied stays the run's explicit choice."""
    if not folder:
        return cands
    for i, c in enumerate(cands):
        if c.get("emptied") and c.get("set", {}).get("folder") == folder:
            c["usable"] = True
            c["assigned"] = True
            return [c] + cands[:i] + cands[i + 1:]
    return cands


def match_calibration(inv, flat_checks: dict | None = None, th: Thresholds | None = None,
                      assigned: dict | None = None) -> list[dict]:
    """Für jede Light-Gruppe: passende Flats/Bias/Darks/DarkFlats mit Begründung.
    assigned: {(filter, settings): {kind: folder}} aus dem vorherigen Plan – nur dieser Satz darf leer bleiben."""
    th = th or Thresholds()
    flat_checks = flat_checks or {}
    assigned = assigned or {}
    out = []
    for g in inv.lights:
        want = assigned.get((g.filter, tuple(g.settings))) or {}
        used = [f for f in g.frames if not f.excluded]
        if not used:
            continue
        last_night = max(f.night or "" for f in used)
        foc = _median([f.focus_pos for f in used if f.night == last_night])
        rot = _median([f.rotator for f in used])
        exp = _median([f.exposure for f in used])
        temp = _median([f.ccd_temp for f in used])
        res = dict(group=g.summary(), flats=[], bias=[], darks=[], darkflats=[], findings=[])
        # ---- Flats
        cands = []
        for c in inv.calib:
            if c.type != fm.FLAT or (c.filter or "").lower() != (g.filter or "").lower():
                continue
            keep_empty = c.folder == want.get("flats") and not any(not f.excluded for f in c.frames)
            if not c.frames and not keep_empty:
                continue
            if keep_empty:
                chk = flat_checks.get(c.id)
                cands.append(dict(set=c.summary(), score=0.0, reasons=["alle Frames manuell abgewählt"],
                                  _frames=[], check=chk and dict(verdict=chk["verdict"], findings=chk["findings"]),
                                  emptied=True, assigned=True, usable=True))
                continue
            mism = _settings_mismatch(g.settings, c.settings)
            first = c.frames[0]
            camnote = _camera_note(g.settings, c.settings)
            cfoc = _median([f.focus_pos for f in c.frames])
            crot = _median([f.rotator for f in c.frames])
            age = _days(first.date_loc or first.date_obs, last_night)
            rd = _rot_diff(crot, rot)
            reasons, score = [], 0.0
            if mism:
                reasons += mism
                score -= 1000
            if camnote:
                reasons += camnote
                score -= 10
            if rd is not None:
                if rd > th.rot_tol:
                    reasons.append("Rotation weicht um %.1f° ab" % rd); score -= 200
            else:
                reasons.append("Rotation unbekannt"); score -= 5
            if cfoc is not None and foc is not None:
                if abs(cfoc - foc) > th.focus_tol:
                    reasons.append("Fokus %d statt %d" % (cfoc, foc)); score -= 50
            else:
                reasons.append("Fokus unbekannt"); score -= 5
            if age is not None:
                score -= abs(age) * (0.5 if age >= 0 else 2.0)   # Flats nach der Nacht bevorzugen
                if age < -th.flat_max_age_days:
                    reasons.append("%d Tage vor der letzten Nacht aufgenommen" % -age)
            n = len(c.frames)
            is_master = all(f.is_master for f in c.frames)
            if is_master:
                reasons.append("fertiger Master")
                n = th.flat_good_frames                    # Master = bereits integriert
                score += 5
            if n < th.flat_min_frames:
                reasons.append("nur %d Frame%s" % (n, "" if n == 1 else "s")); score -= 400 + 20 * (th.flat_min_frames - n)
            elif n < th.flat_good_frames:
                reasons.append("nur %d Frames (besser %d+)" % (n, th.flat_good_frames)); score -= 3 * (th.flat_good_frames - n)
            excl = all(f.excluded for f in c.frames)
            if excl:
                reasons.append("Ordner als verworfen markiert"); score -= 2000
            chk = flat_checks.get(c.id)
            if chk:
                score -= {"critical": 300, "warning": 30}.get(chk["verdict"], 0)
            cands.append(dict(set=c.summary(), score=round(score, 1), reasons=reasons, _frames=c.frames,
                              check=chk and dict(verdict=chk["verdict"], findings=chk["findings"]),
                              usable=not mism and not excl))
        cands.sort(key=lambda x: -x["score"])
        cands = _assigned_empty_first(cands, want.get("flats"))
        res["flats"] = cands
        if not any(c["usable"] for c in cands):
            res["findings"].append(Finding("critical", "Keine passenden Flats für %s" % (g.filter or "ohne Filter"),
                                           "Mit diesem Filter und diesen Kameraeinstellungen gibt es keine Flats." +
                                           (" Abweichend: " + "; ".join(cands[0]["reasons"]) if cands else "")).to_dict())
        # ---- Bias / DarkFlats / Darks
        for typ, key in ((fm.BIAS, "bias"), (fm.DARKFLAT, "darkflats"), (fm.DARK, "darks")):
            lst = []
            for c in inv.calib:
                if c.type != typ:
                    continue
                keep_empty = c.folder == want.get(key) and not any(not f.excluded for f in c.frames)
                if not c.frames and not keep_empty:
                    continue
                if keep_empty:
                    lst.append(dict(set=c.summary(), score=0.0, reasons=["alle Frames manuell abgewählt"],
                                    _frames=[], emptied=True, assigned=True, usable=True))
                    continue
                mism = _settings_mismatch(g.settings, c.settings)
                reasons = list(mism) + _camera_note(g.settings, c.settings)
                score = (-1000 if mism else 0) - (5 if _camera_note(g.settings, c.settings) else 0)
                if typ == fm.DARK:
                    if c.exposure and exp and abs(c.exposure - exp) / exp > 0.02:
                        reasons.append("Belichtung %.0f s statt %.0f s" % (c.exposure, exp)); score -= 500
                    ct = _median([f.ccd_temp for f in c.frames])
                    if ct is not None and temp is not None and abs(ct - temp) > th.dark_temp_tol:
                        reasons.append("Temperatur %.1f °C statt %.1f °C" % (ct, temp)); score -= 100
                age = _days((c.frames[0].date_loc or c.frames[0].date_obs), last_night)
                if age is not None:
                    score -= abs(age) * 0.1
                if all(f.excluded for f in c.frames):
                    reasons.append("Ordner als verworfen markiert"); score -= 2000
                if all(f.is_master for f in c.frames):
                    reasons.append("fertiger Master"); score += 5
                elif len(c.frames) < 10:
                    reasons.append("nur %d Frames" % len(c.frames)); score -= 5 * (10 - len(c.frames))
                lst.append(dict(set=c.summary(), score=round(score, 1), reasons=reasons, usable=score > -400, _frames=c.frames))
            lst.sort(key=lambda x: -x["score"])
            lst = _assigned_empty_first(lst, want.get(key))
            res[key] = lst
        if not any(b["usable"] for b in res["bias"]) and not any(b["usable"] for b in res["darkflats"]):
            res["findings"].append(Finding("warning", "Kein Bias und keine Dark-Flats",
                                           "Flats können nicht sauber kalibriert werden.").to_dict())
        if not any(d["usable"] for d in res["darks"]):
            res["findings"].append(Finding("info", "Keine passenden Darks",
                                           "b2stacker entfernt die Hotpixel vor dem Stacken selbst. Besser: Darks mit gleicher "
                                           "Belichtungszeit, Gain und Temperatur aufnehmen und beim Aufnehmen dithern.").to_dict())
        best = next((c for c in cands if c["usable"]), None)
        if best and not best.get("emptied") and best["set"]["frames"] < th.flat_min_frames and not best["set"].get("is_master"):
            res["findings"].append(Finding("warning", "Sehr wenige Flats (%d)" % best["set"]["frames"],
                                           "Ein Master-Flat aus so wenigen Frames bringt Rauschen ins Bild. "
                                           "Besser 20–30 Flats je Filter aufnehmen.").to_dict())
        if best:
            same = [c for c in inv.calib if c.type == fm.FLAT and c.folder == best["set"]["folder"]
                    and (c.filter or "").lower() == (g.filter or "").lower()]
            if len(same) > 1:
                res["findings"].append(Finding("info", "Mehrere Flat-Sitzungen im gewählten Ordner",
                                               "%s enthält %d Sitzungen (%s). Verwendet wird nur: %s." % (
                                                   best["set"]["folder"], len(same),
                                                   ", ".join(_sess_label(c.summary()) for c in same),
                                                   _sess_label(best["set"]))).to_dict())
        out.append(res)
    return out


def _sess_label(s: dict) -> str:
    t = (s.get("session") or "")[11:16]
    er = s.get("exposure_range")
    exp = ("%g–%g s" % tuple(er)) if (er and er[1] > er[0] * 1.15) else ("%g s" % s["exposure"] if s.get("exposure") else "?")
    return "%s%s × %d%s" % ((t + " ") if t else "", exp, s["frames"], " Himmelsflats" if s.get("sky") else "")


class _FlatStatsCache:
    """Dict-like view on the catalog; stats are keyed by content hash, the path is set per run."""

    def __init__(self, cat, bayer_by_path: dict):
        self._cat = cat
        self._bayer = bayer_by_path
        self._mem = {}
        self._pending = {}      # path -> (sha, stat-sig) taken before measuring

    def __contains__(self, path) -> bool:
        if path in self._mem:
            return True
        try:
            sha, sig = self._cat.resolve(path)
        except OSError:
            return False
        if not sha:
            return False
        hit = self._cat.flat_get(sha, self._bayer.get(path))
        if hit is None:
            self._pending[path] = (sha, sig)
            return False
        self._mem[path] = dict(hit, path=path)
        return True

    def __getitem__(self, path):
        return self._mem[path]

    def __setitem__(self, path, stats: dict) -> None:
        self._mem[path] = stats
        pending = self._pending.pop(path, None)
        if not pending or "error" in stats:
            return
        sha, sig = pending
        # Store only if the file still matches the hashed content; otherwise the stats may belong to newer bytes.
        if _stat_sig(path) != sig:
            return
        self._cat.flat_put(sha, self._bayer.get(path), {k: v for k, v in stats.items() if k != "path"})


def _stat_sig(path: str):
    from .catalog import file_sig
    try:
        return file_sig(path)
    except OSError:
        return None


def check_all(inv, th: Thresholds | None = None, progress=None, catalog_dir=None,
              assigned: dict | None = None) -> dict:
    """Measure all flat sets and match light groups. With ``catalog_dir``, flat stats are reused per content;
    verdicts and light matching are always recomputed. assigned preserves prior per-run calibration choices."""
    from .catalog import open_catalog
    th = th or Thresholds()
    flats = [c for c in inv.calib if c.type == fm.FLAT]
    cat = open_catalog(catalog_dir) if catalog_dir else None
    cache = _FlatStatsCache(cat, {f.path: f.bayer for c in flats for f in c.frames}) if cat else {}
    checks = {}
    for i, c in enumerate(flats):
        checks[c.id] = check_flat_set(c, th, cache)
        if progress:
            progress(i + 1, len(flats), c.folder)
    if cat:
        cat.flush()
    matches = match_calibration(inv, checks, th, assigned=assigned)
    return dict(flat_checks={k: {kk: vv for kk, vv in v.items() if kk != "frames"} | {"frames": v.get("frames", [])}
                             for k, v in checks.items()}, matches=matches)


def public(obj):
    """Entfernt interne Schlüssel (``_frames``) für die JSON-Ausgabe."""
    if isinstance(obj, dict):
        return {k: public(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [public(v) for v in obj]
    return obj
