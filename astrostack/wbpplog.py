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
    (re.compile(r"Numeric parameter value out of range: ([-\d.]+?)\.? Valid range", re.I), "error",
     "PixInsight hat einen Parameterwert abgelehnt ({path} liegt außerhalb des gültigen Bereichs) – das ist ein Fehler "
     "in b2stacker, bitte melden."),
    (re.compile(r"Registration failed[: ]*(.*)$", re.I | re.M), "error",
     "Die Ausrichtung (Registrierung) der Lights ist fehlgeschlagen."),
    (re.compile(r"No active frames to be integrated", re.I), "error",
     "Nach der Ausrichtung blieb kein Light für die Integration übrig – es entstand kein Stack."),
    (re.compile(r"^(?:(?!Diagnostics).)*\*\*\* Error[^\n]*Cannot create files with a path longer than (\d+) characters", re.I | re.M), "error",
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
# Echte Fehler während des Laufs → rote Meldung. WBPPs Vorab-Diagnose („Problems found before the execution“,
# „Diagnostics: …“) ist nur ein Hinweis: sie prüft z. B. die Windows-Einstellung für lange Pfade, nicht die Pfade
# dieses Laufs (Bericht 2 §11).
ALERT = re.compile(r"(!!! Error|\*\*\* Error|\*\*\* Failed|\*\* Canceled|Pipeline builder script failed)", re.I)
HINT = re.compile(r"(Problems found before the execution|Diagnostics:)", re.I)
LONGPATH_HINT = re.compile(r"Diagnostics:.*path longer than (\d+)", re.I)


def classify(line: str) -> str | None:
    """WBPP-Zeile → "alert" | "hint" | None. Diagnose-Zeilen sind nie ein Alarm."""
    if HINT.search(line):
        return "hint"
    if ALERT.search(line):
        return "alert"
    return None


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


# ---------------------------------------------------------------- Live-Fortschritt aus dem WBPP-Protokoll (Bericht 2 §12)

_BEGIN_INT = re.compile(r"\* Begin integration of (Bias|Dark|Flat|Dark ?flat|Light)[^\n]*frames", re.I)
_WRITE_MASTER = re.compile(r"\* Writing master (Bias|Dark|Flat|Dark ?flat|Light)[^\n]*frame", re.I)
_CAL_FRAME = re.compile(r"Calibration frame (\d+)\s*:\s*(.+?)\s*--->\s*(.+)$", re.I)
_KIND_DE = {"bias": "Bias", "dark": "Dark", "flat": "Flat", "darkflat": "Dark-Flat", "light": "Light"}


def _k(word: str) -> str:
    return word.lower().replace(" ", "").replace("-", "")


class CalibTracker:
    """Liest die Zeilen, die b2stacker ohnehin mitliest, und macht daraus Unterschritte des Kalibrierens:
    Master-Bias/-Dark/-Flat erstellen, Flats kalibrieren (n/N), Lights kalibrieren (n/N)."""

    def __init__(self, n_lights: int, n_flats: int = 0):
        self.n_lights, self.n_flats = n_lights, n_flats
        self.subs: list[dict] = []
        self._cal = {"flat": set(), "light": set()}

    def _sub(self, key, title, total=0):
        for s in self.subs:
            if s["key"] == key:
                return s
        for s in self.subs:                       # ein neuer Unterschritt beendet den vorigen
            if s["state"] == "now":
                s["state"] = "done"
        s = dict(key=key, title=title, state="now", done=0, total=total)
        self.subs.append(s)
        return s

    def feed(self, line: str) -> None:
        m = _BEGIN_INT.search(line)
        if m and _k(m.group(1)) != "light":
            k = _k(m.group(1))
            self._sub("master_" + k, "Master-%s erstellen" % _KIND_DE.get(k, k))
            return
        m = _WRITE_MASTER.search(line)
        if m and _k(m.group(1)) != "light":
            s = self._sub("master_" + _k(m.group(1)), "Master-%s erstellen" % _KIND_DE.get(_k(m.group(1)), ""))
            s["state"] = "done"
            return
        m = _CAL_FRAME.search(line)
        if m:
            src, dst = m.group(2), m.group(3)
            low = (dst + " " + src).replace("\\", "/").lower()
            kind = "flat" if ("/calibrated/flat" in low or "/flats_" in low or "/f/" in low or
                              "/flat" in os.path.dirname(src.replace("\\", "/")).lower()) else "light"
            if "/calibrated/light" in low:
                kind = "light"
            if kind == "flat":
                s = self._sub("cal_flat", "Flats kalibrieren", self.n_flats)
            else:
                s = self._sub("cal_light", "Lights kalibrieren", self.n_lights)
            self._cal[kind].add(m.group(1))
            s["done"] = len(self._cal[kind])
            if s["total"] and s["done"] >= s["total"]:
                s["state"] = "done"

    @property
    def lights_done(self) -> int:
        return len(self._cal["light"])

    def as_list(self) -> list[dict]:
        return [dict(s) for s in self.subs]


def hint_text(line: str, max_path: int | None = None) -> str:
    """WBPP-Vorab-Diagnose → kurzer deutscher Hinweis."""
    m = LONGPATH_HINT.search(line)
    if m:
        lim = int(m.group(1))
        if max_path:
            return ("Windows-Einstellung „lange Pfade“ ist aus. Für diesen Lauf %s (längster Pfad ca. %d von %d Zeichen)."
                    % ("unkritisch" if max_path < lim - 10 else "knapp", max_path, lim))
        return "Windows-Einstellung „lange Pfade“ ist aus (Pfade bis %d Zeichen)." % lim
    txt = re.sub(r"^\W*(\[[^\]]*\]\s*)?\**\s*Warning:\s*", "", line).strip()
    return "Hinweis von PixInsight: " + txt
