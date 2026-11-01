"""WBPP-Protokoll auswerten – vor allem: hat die Registrierung (StarAlignment) jeden Frame richtig erwischt?

Hintergrund (NGC 1999, 2026-11-01): StarAlignment meldete für einen völlig normalen Frame „Registration successful“,
obwohl RANSAC nur 18 von 527 Sternpaaren fand (Inliers 3 %, Maßstab 0,953 – physikalisch unmöglich). Die
Verzerrungskorrektur hat das Bild danach wild verbogen; helle Sterne landeten in einer anderen Bildecke. Die
Pixel-Ablehnung hat die Sternkerne entfernt, Halos und Spikes blieben → dunkle „Löcher“ mit Spikes im Stack.
WBPP selbst bricht in so einem Fall nicht ab – also prüfen wir das Protokoll selbst.
"""
from __future__ import annotations

import glob
import os
import re

# Grenzwerte: gute Frames liegen bei Maßstab 0,999–1,001 (gleiche Optik!), Quality > 0,7, Inliers > 0,3
SCALE_TOL = 0.01
MIN_INLIERS = 0.05
MIN_QUALITY = 0.40

_NUM = r"([-+]?\d+(?:\.\d+)?)"
_PATTERNS = {
    "matches": re.compile(r"(\d+) star pair matches in \d+ RANSAC"),
    "inliers": re.compile(r"Inliers\s*:\s*" + _NUM),
    "quality": re.compile(r"Quality\s*:\s*" + _NUM),
    "rotation": re.compile(r"rotation\s*:\s*" + _NUM + r"\s*deg"),
    "scale": re.compile(r"\bscale\s*:\s*" + _NUM),
    "tx": re.compile(r"translation_x\s*:\s*" + _NUM),
    "ty": re.compile(r"translation_y\s*:\s*" + _NUM),
}
_LOAD = re.compile(r"\* Loading target file: (.+?)\s*$")
_FAIL = re.compile(r"(Unable to find an initial set of putative star pair matches|Registration failed|"
                   r"\*\*\* Error.*StarAlignment)", re.I)


def newest_log(logdir: str) -> str | None:
    files = [f for f in glob.glob(os.path.join(logdir, "*.log")) if os.path.isfile(f)]
    return max(files, key=os.path.getmtime) if files else None


def registrations(text: str) -> list[dict]:
    """Je registriertem Frame die Kennzahlen aus dem StarAlignment-Teil des WBPP-Protokolls."""
    out: list[dict] = []
    cur: dict | None = None
    in_sa = False
    for raw in text.splitlines():
        line = re.sub(r"^\[[^\]]*\]\s?", "", raw)        # Zeitstempel weg
        if "Begin registration of light frames" in line:
            in_sa = True
            continue
        if not in_sa:
            continue
        if "===== StarAlignment:" in line or "End registration of light frames" in line:
            break
        m = _LOAD.search(line)
        if m:
            cur = {"file": m.group(1).strip()}
            out.append(cur)
            continue
        if cur is None:
            continue
        if "matches" not in cur:
            m = _PATTERNS["matches"].search(line)
            if m:
                cur["matches"] = int(m.group(1))
                continue
        for k in ("inliers", "quality", "rotation", "scale", "tx", "ty"):
            if k not in cur:
                m = _PATTERNS[k].search(line)
                if m:
                    cur[k] = float(m.group(1))
                    break
        if _FAIL.search(line):
            cur["failed"] = True
        if "Registration successful" in line:
            cur["ok"] = True
    return [r for r in out if "scale" in r or r.get("failed")]


def judge(regs: list[dict]) -> list[dict]:
    """Verdächtige Registrierungen mit Begründung (deutsch). Leere Liste = alles plausibel."""
    bad = []
    for r in regs:
        why = []
        if r.get("failed") and not r.get("ok"):
            why.append("nicht registriert")
        sc = r.get("scale")
        if sc is not None and abs(sc - 1) > SCALE_TOL:
            why.append("Maßstab %.3f statt 1,000" % sc)
        if r.get("inliers") is not None and r["inliers"] < MIN_INLIERS:
            why.append("nur %.0f %% passende Sterne" % (100 * r["inliers"]))
        if r.get("quality") is not None and r["quality"] < MIN_QUALITY:
            why.append("Qualität %.2f" % r["quality"])
        if why:
            bad.append(dict(r, why=why))
    return bad


def check_registration(outdir: str) -> dict:
    """Protokoll des letzten WBPP-Laufs in <outdir>/logs prüfen."""
    log = newest_log(os.path.join(outdir, "logs"))
    if not log:
        return {"log": None, "frames": 0, "bad": []}
    try:
        text = open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        return {"log": log, "frames": 0, "bad": []}
    regs = registrations(text)
    return {"log": log, "frames": len(regs), "bad": judge(regs), "regs": regs}


_SUFFIX = re.compile(r"(_(c|cc|d|r|a|n|cfa))+$", re.I)


def stem(path: str) -> str:
    """Name eines Zwischenprodukts → Name des Original-Lights (…_0034_c_d_r.xisf → …_0034)."""
    b = os.path.splitext(os.path.basename(path.replace("\\", "/")))[0]
    return _SUFFIX.sub("", b)


def map_to_lights(bad: list[dict], lights: list[str]) -> list[str]:
    """Verdächtige WBPP-Dateien den Original-Lights des Plans zuordnen."""
    by = {}
    for p in lights:
        by.setdefault(stem(p).lower(), p)
        b = os.path.splitext(os.path.basename(p))[0].lower()
        by.setdefault(b, p)
    out = []
    for r in bad:
        s = stem(r["file"]).lower()
        p = by.get(s)
        if p is None:                                    # gestagte Kopien tragen ein Präfix (z. B. 0294b169_name)
            for k, v in by.items():
                if s.endswith("_" + k) or s.endswith(k):
                    p = v
                    break
        if p and p not in out:
            out.append(p)
    return out
