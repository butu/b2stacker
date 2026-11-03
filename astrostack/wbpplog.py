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
_REF = re.compile(r"^Reference image: (.+?)\s*$")
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
        m = _REF.search(line)
        if m and not any(r.get("reference") for r in out):
            out.append({"reference": m.group(1).strip()})
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
    return [r for r in out if r.get("reference") or "scale" in r or r.get("failed")]


def reference_of(regs: list[dict]) -> str | None:
    for r in regs:
        if r.get("reference"):
            return r["reference"]
    return None


LOCK_PX = 0.5          # Verschiebung unter 0,5 px zwischen verschiedenen Nächten/Meridianseiten ist unmöglich


def judge(regs: list[dict], info: dict | None = None) -> list[dict]:
    """Verdächtige Registrierungen mit Begründung (deutsch). Leere Liste = alles plausibel.

    info: {stem (klein): {"night": …, "pier": …}} der Original-Lights – damit wird erkannt, wenn StarAlignment die
    Bilder am Sensor (Hotpixel) statt am Himmel ausgerichtet hat: Bilder aus anderer Nacht oder von der anderen
    Meridianseite liegen dann „perfekt“ (0,0 px, 0,0°) auf der Referenz. → lock=True."""
    bad = []
    ref = reference_of(regs)
    rinfo = (info or {}).get(stem(ref).lower()) if ref else None
    for r in regs:
        if r.get("reference"):
            continue
        why = []
        lock = False
        fi = (info or {}).get(stem(r["file"]).lower())
        if rinfo and fi and stem(r["file"]).lower() != stem(ref).lower() and r.get("scale") is not None:
            t = ((r.get("tx") or 0) ** 2 + (r.get("ty") or 0) ** 2) ** 0.5
            other = []
            if fi.get("night") and rinfo.get("night") and fi["night"] != rinfo["night"]:
                other.append("andere Nacht")
            if fi.get("pier") and rinfo.get("pier") and fi["pier"] != rinfo["pier"]:
                other.append("andere Meridianseite")
            if other and t < LOCK_PX and abs(r.get("rotation") or 0) < 1:
                lock = True
                why.append("am Sensor statt an den Sternen ausgerichtet (%s, trotzdem %.2f px / %.2f°)" % (
                    " + ".join(other), t, r.get("rotation") or 0))
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
            bad.append(dict(r, why=why, lock=lock))
    return bad


def check_registration(outdir: str, info: dict | None = None) -> dict:
    """Protokoll des letzten WBPP-Laufs in <outdir>/logs prüfen."""
    log = newest_log(os.path.join(outdir, "logs"))
    if not log:
        return {"log": None, "frames": 0, "bad": []}
    try:
        text = open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        return {"log": log, "frames": 0, "bad": []}
    regs = registrations(text)
    frames = [r for r in regs if not r.get("reference")]
    return {"log": log, "frames": len(frames), "bad": judge(regs, info), "regs": frames, "reference": reference_of(regs)}


_SUFFIX = re.compile(r"(_(c|cc|d|r|a|n|cfa))+$", re.I)


def stem(path: str) -> str:
    """Name eines Zwischenprodukts → Name des Original-Lights (…_0034_c_d_r.xisf → …_0034)."""
    b = os.path.splitext(os.path.basename(path.replace("\\", "/")))[0]
    return _SUFFIX.sub("", b)


def light_info(lights: list[str]) -> dict:
    """{stem: {night, pier}} aus den FITS-Headern der Original-Lights (nur Header, schnell)."""
    from .fitsmeta import read_meta
    out = {}
    for p in lights:
        try:
            m = read_meta(p)
        except Exception:  # noqa: BLE001
            continue
        pier = (m.pier_side or "").strip().lower() or None
        out[stem(p).lower()] = {"night": m.night, "pier": pier}
    return out


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


# ---------------------------------------------------------------- Fehler verständlich machen (Bericht 0.8 §9)

_CAUSES = [
    # (Muster, Schweregrad, Text) – Text darf {n}, {kind}, {path} aus dem Treffer nutzen
    (re.compile(r"Canceled: Integration \[(Bias|Dark|Flat|Dark ?flat)[^\]]*frames = (\d+)", re.I), "error",
     "PixInsight konnte keinen Master-{kind} erstellen (nur {n} {kind}-Bild{pl}). Vermutlich ist die Datei schon ein fertiger "
     "Master (z. B. aus Astro Pixel Processor) oder es fehlen Kalibrierbilder – Kalibrierdaten prüfen."),
    (re.compile(r"Cannot create files with a path longer than (\d+) characters", re.I), "error",
     "Windows erlaubt nur Pfade bis {n} Zeichen. Lange Pfade in Windows aktivieren (LongPathsEnabled) oder ein "
     "kürzeres Arbeitsverzeichnis wählen (z. B. D:\\b2)."),
    (re.compile(r"Pipeline builder script failed.*?(TypeError|Error)[: ]*(.*)$", re.I | re.M), "warning",
     "Die Hotpixel-Korrektur in PixInsight ist fehlgeschlagen (Pipeline-Skript passt nicht zu dieser WBPP-Version)."),
    (re.compile(r"Cosmetic Correction requires a master dark", re.I), "warning",
     "PixInsight-Hotpixel-Korrektur braucht einen Master-Dark."),
    (re.compile(r"(?:File|Directory) not found:\s*(.+)$", re.I | re.M), "error",
     "PixInsight findet eine Datei nicht: {path} – ist das Laufwerk erreichbar?"),
    (re.compile(r"Invalid argument", re.I), "error",
     "PixInsight hat ein Argument nicht verstanden (Sonderzeichen im Pfad?)."),
    (re.compile(r"Flat frames not calibrated", re.I), "error",
     "Die Flats konnten nicht kalibriert werden, weil ein Master-Bias bzw. Dark-Flat fehlt."),
    (re.compile(r"Light frames not calibrated", re.I), "error",
     "Die Lights konnten nicht kalibriert werden, weil ein Master (Bias/Dark/Flat) fehlt."),
    (re.compile(r"No frames to be measured|(\d+) frame\(s\) excluded", re.I), "error",
     "Kein Light blieb für die Integration übrig."),
]
ALERT = re.compile(r"(\*\*\* Error|\*\*\* Failed|\*\* Canceled|Problems found before the execution|"
                   r"Pipeline builder script failed|path longer than)", re.I)


def diagnose_text(text: str) -> list[dict]:
    """Bekannte WBPP-Fehler → verständliche Ursachen (Reihenfolge = Wichtigkeit, ohne Doppelte)."""
    out, seen = [], set()
    for rx, level, msg in _CAUSES:
        m = rx.search(text)
        if not m:
            continue
        g = m.groups()
        kind = (g[0] or "").replace("Dark flat", "Dark-Flat").replace("Darkflat", "Dark-Flat").capitalize() if g else ""
        n = next((x for x in g if x and x.isdigit()), "") if g else ""
        txt = msg.format(n=n, kind=kind, pl="" if n == "1" else "er", path=(g[-1] if g else "").strip())
        if txt not in seen:
            seen.add(txt)
            out.append(dict(level=level, text=txt, line=m.group(0)[:200]))
    return out


def diagnose(outdir: str) -> list[dict]:
    log = newest_log(os.path.join(outdir, "logs"))
    if not log:
        return []
    try:
        return diagnose_text(open(log, encoding="utf-8", errors="replace").read())
    except OSError:
        return []
