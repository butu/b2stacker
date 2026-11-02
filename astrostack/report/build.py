"""HTML-Bericht für ein Ziel bauen: Übersicht, Kalibrierung, Rig-Diagnose, Stacking-Plan, Charts, Galerie."""
from __future__ import annotations

import base64
import datetime
import html
import io
import json
import os
import statistics as st

import numpy as np
from PIL import Image

from .. import fitsmeta as fm
from ..analyze import thumb_name
from ..calibcheck import match_calibration, public
from .. import discover, rotation
from ..plan import build_plan
from . import assess as A
from .diag import rig_diagnostics
from .sky import sky_tracks

TEMPLATE = os.path.join(os.path.dirname(__file__), "template.html")


def stretch(t: np.ndarray) -> np.ndarray:
    med = np.median(t)
    mad = 1.4826 * np.median(np.abs(t - med)) + 1e-3
    lo = med - 2.8 * mad
    span = max(t.max() - lo, 1e-6)
    x = np.clip((t - lo) / span, 0, 1)
    m = np.clip((med - lo) / span, 1e-6, 1)
    tg = 0.25
    b = m * (tg - 1) / ((2 * tg - 1) * m - tg)
    return np.clip(((b - 1) * x) / ((2 * b - 1) * x - b), 0, 1)


def thumb_b64(analysis_dir: str, path: str, width: int = 440) -> str:
    p = os.path.join(analysis_dir, "thumbs", thumb_name(path))
    if not os.path.exists(p):
        return ""
    im = Image.fromarray((stretch(np.load(p)) * 255).astype(np.uint8))
    im = im.resize((width, max(1, int(width * im.height / im.width))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def calib_checks(matches: list[dict]) -> list[dict]:
    out = []
    for m in matches:
        g = m["group"]
        label = g["filter"] or "ohne Filter"
        best = next((c for c in m["flats"] if c["usable"]), None)
        if best:
            chk = best.get("check") or {}
            verdict = chk.get("verdict", "good")
            lvl = {"critical": "critical", "warning": "warning"}.get(verdict, "good")
            if best["reasons"] and lvl == "good" and any("weicht" in r or "Tage VOR" in r for r in best["reasons"]):
                lvl = "warning"
            s = best["set"]
            text = "%d Frames, %s s, %s. " % (s["frames"], s["exposure"], (s["date"] or ["?"])[0][:10])
            if best["reasons"]:
                text += "Hinweise: " + "; ".join(best["reasons"]) + ". "
            text += " ".join(f["title"] + (": " + f["text"] if f["level"] != "good" else "") for f in chk.get("findings", []))
            out.append(dict(level=lvl, title="Flats %s: %s" % (label, os.path.basename(os.path.dirname(s["folder"])) + "/" + os.path.basename(s["folder"])), text=text.strip()))
        for key, name in (("bias", "Bias"), ("darkflats", "Dark-Flats"), ("darks", "Darks")):
            c = next((x for x in m[key] if x["usable"]), None)
            if c:
                s = c["set"]
                out.append(dict(level="good", title="%s (%s): %s" % (name, label, os.path.basename(s["folder"])),
                                text="%d Frames, Gain %s / Offset %s / %s passt." % (s["frames"], s["gain"], s["offset"], s["readout"] or "–")))
        for f in m["findings"]:
            out.append(dict(level=f["level"], title=f["title"], text=f["text"]))
    seen, res = set(), []
    for c in out:
        k = (c["title"], c["text"])
        if k not in seen:
            seen.add(k)
            res.append(c)
    return res


def night_flat_groups(groups, rows, inv, calib: dict, analysis_dir: str) -> dict:
    """Je Light-Gruppe: Nächte nach Kamera-Stellung gruppieren und je Gruppe Flats zuordnen.
    → {(filter, settings): [dict(key, nights, angle, source, match)]} – nur Einträge mit Drehungs-Info."""
    out = {}
    for g in groups:
        used = [f for f in g.frames if not f.excluded]
        nights = sorted({f.night for f in used if f.night})
        if not nights:
            continue
        hdr = {}
        for n in nights:
            vals = [f.rotator for f in used if f.night == n and f.rotator is not None]
            hdr[n] = float(np.median(vals)) if vals else None
        grows = [r for r in rows if r.get("filter") == g.filter]
        try:
            meas = rotation.night_rotations(grows, analysis_dir, thumb_name) if len(nights) > 1 else {}
        except Exception:  # noqa: BLE001
            meas = {}
        ng = rotation.group_nights(nights, hdr, meas)
        info = dict(header=hdr, measured=meas)
        entry = []
        for grp in ng:
            sub = discover.LightGroup(g.target, g.filter, g.filter_kind, g.is_osc, g.settings,
                                      [f for f in g.frames if f.night in grp["nights"]])
            fake = type("Inv", (), {})()
            fake.lights, fake.calib = [sub], inv.calib
            m = match_calibration(fake, calib.get("flat_checks", {}))
            entry.append(dict(grp, match=m[0] if m else None))
        out[(g.filter, tuple(g.settings))] = dict(groups=entry, info=info)
    return out


def build(target: str, groups, inv, calib: dict, analysis_dir: str, workdir: str, out_html: str) -> dict:
    rows = A.load_rows(os.path.join(analysis_dir, "metrics.csv"))
    pdir = os.path.dirname(out_html)
    ovp = os.path.join(pdir, "overrides.json")
    overrides = json.load(open(ovp, encoding="utf-8")) if os.path.exists(ovp) else {}
    filters, ntab = A.assess(rows, overrides=overrides)
    # Automatische Bewertung ohne manuelle Entscheidung – damit die Oberfläche "selected" ohne Neuberechnung zeigt.
    # auto = ohne manuelle Entscheidung; status/flags = so wie gestackt (inkl. Overrides zum Berichtszeitpunkt).
    with open(os.path.join(analysis_dir, "assessment.json"), "w", encoding="utf-8") as fh:
        json.dump({r["path"]: dict(auto=r["auto_status"], status=r["status"], flags=r["flags"])
                   for r in rows}, fh, ensure_ascii=False)
    rec = A.recommendations(rows, ntab, filters)
    matches = [m for m in calib["matches"] if m["group"]["target"] == target]
    checks = calib_checks(matches)
    flat_groups = night_flat_groups(groups, rows, inv, calib, analysis_dir)
    measured = {r["path"] for r in rows}
    unmeasured = {f.path for g in groups for f in g.frames} - measured
    plan = build_plan(target, groups, rows, ntab, matches, workdir, flat_groups=flat_groups, unmeasured=unmeasured)
    scale = st.median([r["pixscale"] for r in rows if r["pixscale"]]) if any(r["pixscale"] for r in rows) else None
    try:
        diag = rig_diagnostics(rows, scale, analysis_dir)
    except Exception as e:  # Diagnose darf den Bericht nicht verhindern
        diag = None
        rec.append("Rig-Diagnose fehlgeschlagen: %s" % e)
    meta0 = fm.read_meta(groups[0].frames[0].path)
    try:
        sky = sky_tracks(rows, meta0)
    except Exception:
        sky = None
    for r in rows:
        r["img"] = thumb_b64(analysis_dir, r["path"])
    kpi = []
    for f in filters:
        nf = [n for n in ntab if n["filter"] == f]
        kpi.append(dict(filter=f, used_h=sum(n["used_h"] for n in nf), total_h=sum(n["hours"] for n in nf),
                        eff_h=sum(n["eff_h"] for n in nf), n_used=sum(n["n_used"] for n in nf), n=sum(n["n"] for n in nf), nights=len(nf)))
    nights_sorted = sorted(n for n in {r["night"] for r in rows} if n)
    meta = dict(target=target, generated=datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
                camera=" · ".join(str(x) for x in (meta0.camera, meta0.telescope,
                                                   meta0.focal_length and "%.0f mm" % meta0.focal_length,
                                                   meta0.gain is not None and "Gain %g" % meta0.gain) if x),
                scale=round(scale, 3) if scale else None,
                first=nights_sorted[0] if nights_sorted else "", last=nights_sorted[-1] if nights_sorted else "")
    pub_plan = dict(plan)
    pub_plan["runs"] = [dict(r, lightDirs=r["nights"]) for r in plan["runs"]]
    pub_plan["job"] = {k: v for k, v in plan.items() if k not in ("settings",)}
    frames = [{k: r.get(k) for k in ["file", "path", "night", "filter", "frame", "t", "exp", "fwhm", "fwhm_as", "ecc", "nstars", "bg",
                                     "noise", "alt", "moon_illum", "moon_alt", "temp", "status", "auto_status", "override", "weight", "img"]}
              | {"flags": [f[1] for f in r["flags"]]} for r in rows]
    data = dict(meta=meta, filters=filters, kpi=kpi, nights=ntab, checks=checks, rec=rec, plan=pub_plan, sky=sky,
                diag=diag, frames=frames)
    tpl = open(TEMPLATE, encoding="utf-8").read()
    # Chart.js eingebettet: Bericht funktioniert auch ohne Internet (Sternwarte)
    chartjs = open(os.path.join(os.path.dirname(__file__), "vendor", "chart.umd.js"), encoding="utf-8").read()
    tpl = tpl.replace('<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>',
                      "<script>" + chartjs.replace("</script", "<\\/script") + "</script>")
    out = tpl.replace("/*__DATA__*/null", json.dumps(public(data), ensure_ascii=False, default=str).replace("</", "<\\/")).replace(
        "__TITLE__", html.escape(target + " – Rohdaten"))
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    open(out_html, "w", encoding="utf-8").write(out)
    pdir = os.path.dirname(out_html)
    # Basis-Plan (unverändert) + Kandidaten-Pfade getrennt; plan.json = Basis + Benutzer-Änderungen
    cands = {}
    for r in plan["runs"]:
        for kind, lst in r.get("calib_choices", {}).items():
            for c in lst:
                cands[c["id"]] = c.pop("paths", [])
    with open(os.path.join(pdir, "calib_candidates.json"), "w", encoding="utf-8") as fh:
        json.dump(cands, fh, ensure_ascii=False)
    with open(os.path.join(pdir, "plan_base.json"), "w", encoding="utf-8") as fh:
        json.dump(public(plan), fh, indent=2, ensure_ascii=False)
    with open(os.path.join(pdir, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(public(plan), fh, indent=2, ensure_ascii=False)
    return dict(html=out_html, plan=os.path.join(os.path.dirname(out_html), "plan.json"), frames=len(rows))
