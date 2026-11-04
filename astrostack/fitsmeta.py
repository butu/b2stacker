"""Header-Übersetzer: FITS/XISF-Header verschiedener Aufnahmeprogramme → einheitliches FrameMeta.

Unterstützt (erkannt über SWCREATE/CREATOR/Dateinamen/Ordner):
  NINA, ASIAIR, KStars/Ekos, SharpCap, APT, SGP, MaxIm, PixInsight (Masters).

Der Bildtyp wird in dieser Reihenfolge bestimmt:
  1. IMAGETYP / FRAME / FRAMETYP
  2. Plausibilitätsregel: Belichtung <= 1 ms → Bias (NINA-"Snapshot"-Bias hat IMAGETYP=LIGHT)
  3. Ordner- und Dateinamen (Light/Flat/Dark/Bias/DarkFlat …)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import filters as flt

LIGHT, FLAT, DARK, BIAS, DARKFLAT, UNKNOWN = "light", "flat", "dark", "bias", "darkflat", "unknown"
APP_PREFIX = re.compile(r"^(MDF|MB|MD|MF|BPM)-", re.I)
APP_KINDS = {"MB": "bias", "MD": "dark", "MF": "flat", "MDF": "darkflat", "BPM": None}
FITS_EXT = (".fits", ".fit", ".fts")
XISF_EXT = (".xisf",)


@dataclass
class FrameMeta:
    path: str
    source: str = "unknown"            # nina | asiair | ekos | sharpcap | apt | sgp | maxim | pixinsight | unknown
    type: str = UNKNOWN
    type_reason: str = ""              # woher der Typ stammt (header | exposure | path)
    is_master: bool = False
    object: str | None = None
    filter: str | None = None
    filter_kind: str = flt.UNKNOWN
    exposure: float | None = None
    gain: float | None = None
    offset: float | None = None
    readout: str | None = None
    usb: float | None = None
    binning: int = 1
    width: int | None = None
    height: int | None = None
    bayer: str | None = None            # RGGB … oder None (Mono)
    ccd_temp: float | None = None
    set_temp: float | None = None
    date_obs: str | None = None         # ISO UTC
    date_loc: str | None = None
    night: str | None = None            # Datum der Beobachtungsnacht (Abenddatum, lokal)
    camera: str | None = None
    telescope: str | None = None
    focal_length: float | None = None
    pixel_size: float | None = None
    focus_pos: float | None = None
    focus_temp: float | None = None
    rotator: float | None = None
    pier_side: str | None = None
    ra: float | None = None
    dec: float | None = None
    alt: float | None = None
    airmass: float | None = None
    site_lat: float | None = None
    site_lon: float | None = None
    site_elev: float | None = None
    ambient_temp: float | None = None
    dew_point: float | None = None
    humidity: float | None = None
    wind: float | None = None
    software: str | None = None
    hfr_from_name: float | None = None  # NINA-Muster $$HFR$$ am Dateianfang
    site_fallback: bool = False         # Standort aus den Einstellungen statt aus dem Header
    excluded: bool = False              # liegt in _rejected/_alt/… → standardmäßig nicht verwenden
    warnings: list[str] = field(default_factory=list)

    @property
    def is_osc(self) -> bool:
        return bool(self.bayer)

    @property
    def pixel_scale(self) -> float | None:
        if self.pixel_size and self.focal_length:
            return 206.265 * self.pixel_size * self.binning / self.focal_length
        return None

    def settings_key(self) -> tuple:
        """Kamera-Einstellungen, die bei Lights/Flats/Bias/Darks übereinstimmen müssen."""
        return (camera_norm(self.camera), self.gain, self.offset, (self.readout or "").lower(), self.binning, self.width, self.height)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_osc"] = self.is_osc
        d["pixel_scale"] = self.pixel_scale
        return d


# ----------------------------------------------------------------------------- helpers

def _f(h, *keys):
    for k in keys:
        if k in h:
            v = h[k]
            if isinstance(v, str):
                v = v.strip()
                if v == "":
                    continue
                try:
                    return float(v.replace(",", "."))
                except ValueError:
                    continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
    return None


def _s(h, *keys):
    for k in keys:
        if k in h and h[k] not in (None, ""):
            return str(h[k]).strip().strip("'").strip()
    return None


_TYPE_WORDS = [
    (DARKFLAT, ("darkflat", "flatdark", "dark flat", "flat dark", "flat-dark", "dark-flat", "flatdarks", "darkflats")),
    (BIAS, ("bias", "offset", "zero", "biasframe")),
    (FLAT, ("flat", "flatfield", "flat field", "flatframe", "skyflat", "flats")),
    (DARK, ("dark", "darkframe", "darks")),
    (LIGHT, ("light", "science", "object", "lightframe", "lights")),
]


# Dieselbe Kamera meldet sich je nach Treiber (nativ / ASCOM / Alpaca / ältere Version) unterschiedlich.
CAMERA_ALIASES = {
    "atr3cmos26000kpa": "atr2600c",      # ToupTek/Altair ATR2600C – nativer Treiber 2024
    "atr3cmos26000kmb": "atr2600m",
}


def camera_norm(name: str | None) -> str | None:
    """Kameranamen vereinheitlichen: Groß/klein, Treiber-Zusätze wie (USB2.0), "ZWO CCD", Leerzeichen."""
    if not name:
        return name
    n = name.lower()
    n = re.sub(r"\((usb\s*[23](\.0)?|usb)\)", "", n)
    n = re.sub(r"\b(zwo|ccd|camera|kamera|ascom|alpaca|driver|touptek|altair)\b", "", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return CAMERA_ALIASES.get(n, n) or name


def type_from_string(s: str | None) -> tuple[str, bool]:
    """'Master Flat' → (flat, True); 'Light Frame' → (light, False)."""
    if not s:
        return UNKNOWN, False
    low = s.lower().strip()
    master = "master" in low
    low = low.replace("master", "").replace("frame", "").strip(" _-")
    compact = re.sub(r"[^a-z]", "", low)
    for t, words in _TYPE_WORDS:
        for w in words:
            if re.sub(r"[^a-z]", "", w) == compact:
                return t, master
    for t, words in _TYPE_WORDS:   # enthält …
        if any(re.sub(r"[^a-z]", "", w) in compact for w in words):
            return t, master
    return UNKNOWN, master


def type_from_path(path: str) -> str:
    parts = [p.lower() for p in re.split(r"[\\/]", path)]
    name = parts[-1]
    # Dateiname zuerst (ASIAIR: "Light_M31_300.0s_…", SharpCap: "Dark_…")
    m = re.match(r"^(light|flat|dark|bias|darkflat|flatdark|offset)[ _\-]", name)
    if m:
        return type_from_string(m.group(1))[0]
    for p in reversed(parts[:-1]):
        t, _ = type_from_string(p)
        if t != UNKNOWN:
            return t
    return UNKNOWN


def detect_source(h, path: str) -> str:
    sw = (_s(h, "SWCREATE", "CREATOR", "PROGRAM", "SOFTWARE", "SWOWNER") or "").lower()
    name = os.path.basename(path)
    if "n.i.n.a" in sw or sw.startswith("nina"):
        return "nina"
    if "asiair" in sw or "asi air" in sw:
        return "asiair"
    if "kstars" in sw or "ekos" in sw or "indi" in sw:
        return "ekos"
    if "sharpcap" in sw:
        return "sharpcap"
    if "astro photography tool" in sw or sw.startswith("apt"):
        return "apt"
    if "sequence generator" in sw or "sgpro" in sw:
        return "sgp"
    if "maxim" in sw:
        return "maxim"
    if "pixinsight" in sw:
        return "pixinsight"
    if re.match(r"^(Light|Flat|Dark|Bias)_.+_Bin\d_.+_gain\d+_\d{8}-\d{6}", name):
        return "asiair"
    if "Autorun" in path or "Plan" in path.split(os.sep):
        return "asiair"
    return "unknown"


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt).isoformat()
        except ValueError:
            continue
    try:                                            # z. B. Zeitzone "+00:00" oder "Z" am Ende
        return datetime.fromisoformat(s.rstrip("Z")).replace(tzinfo=None).isoformat()
    except ValueError:
        return None                                 # unlesbar ("gestern abend") → wie fehlend behandeln


_PATH_DATE = (re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)[_T ](\d\d)[-:](\d\d)[-:](\d\d)"),   # NINA 2026-10-20_21-05-00
              re.compile(r"(?<!\d)(20\d\d)(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)(?!\d)"))              # ASIAIR 20261020-213000
_PATH_DAY = re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)")


def night_from_path(path: str) -> str | None:
    """Ersatz, wenn die Aufnahmezeit im Header fehlt: Zeitstempel im Dateinamen, sonst Datumsordner."""
    from datetime import timedelta
    base = os.path.basename(path)
    for rx in _PATH_DATE:
        mm = rx.search(base)
        if mm:
            try:
                t = datetime(*map(int, mm.groups()))
            except ValueError:
                continue
            return (t - timedelta(hours=12)).date().isoformat()
    for part in reversed(os.path.dirname(path).replace("\\", "/").split("/")):
        mm = _PATH_DAY.search(part)
        if mm:
            try:
                return datetime(*map(int, mm.groups())).date().isoformat()   # NINA-Ordner = Abenddatum
            except ValueError:
                continue
    return None


def night_of(date_loc: str | None, date_obs: str | None, lon: float | None) -> str | None:
    """Beobachtungsnacht = lokales Datum minus 12 h (Aufnahme um 02:00 gehört zur Nacht davor)."""
    from datetime import timedelta
    t = None
    if date_loc:
        try:
            t = datetime.fromisoformat(date_loc[:19])
        except ValueError:
            t = None
    if t is None and date_obs:
        try:
            t = datetime.fromisoformat(date_obs[:19]) + timedelta(hours=(lon or 15.0) / 15.0)
        except ValueError:
            return None
    if t is None:
        return None
    return (t - timedelta(hours=12)).date().isoformat()


def _sexa(s: str | None, hours: bool) -> float | None:
    if not s:
        return None
    try:
        parts = re.split(r"[:\s]+", s.strip())
        sign = -1 if parts[0].startswith("-") else 1
        v = abs(float(parts[0])) + float(parts[1]) / 60 + (float(parts[2]) if len(parts) > 2 else 0) / 3600
        return sign * v * (15 if hours else 1)
    except (ValueError, IndexError):
        return None


# ----------------------------------------------------------------------------- main

_FB_CACHE: dict = {}


def fallback_site():
    """(lat, lon, elev) aus den Einstellungen oder None. Liest die Datei nur neu, wenn sie sich geändert hat."""
    from .settings import Settings
    p = Settings.path()
    try:
        key = (p, os.path.getmtime(p))
    except OSError:
        return None
    if _FB_CACHE.get("key") != key:
        try:
            d = json.load(open(p, encoding="utf-8"))
            lat, lon = d.get("site_lat"), d.get("site_lon")
            val = (float(lat), float(lon), float(d.get("site_elev") or 0)) \
                if lat not in (None, "") and lon not in (None, "") else None
        except (OSError, ValueError, TypeError):
            val = None
        _FB_CACHE.update(key=key, val=val)
    return _FB_CACHE["val"]


def from_header(h, path: str) -> FrameMeta:
    """Header-Mapping (astropy Header oder dict) → FrameMeta."""
    m = FrameMeta(path=path)
    m.source = detect_source(h, path)
    m.software = _s(h, "SWCREATE", "CREATOR", "PROGRAM")

    raw_type = _s(h, "IMAGETYP", "FRAME", "FRAMETYP", "IMAGETYPE")
    t, master = type_from_string(raw_type)
    base = os.path.basename(path)
    m.is_master = master or base.lower().startswith("master")
    m.exposure = _f(h, "EXPTIME", "EXPOSURE", "EXP")
    # Fertige Masters aus Astro Pixel Processor: MB-/MD-/MF-/MDF- im Dateinamen, meist ohne IMAGETYP (Bericht 0.8 §8)
    app = APP_PREFIX.match(base)
    sw_all = (_s(h, "SOFTWARE", "SWCREATE", "CREATOR", "PROGRAM") or "").lower()
    if app:
        kind = APP_KINDS[app.group(1).upper()]
        if kind is None:                                  # BPM-: Bad-Pixel-Map – nie als Kalibrierbild
            m.type, m.type_reason = UNKNOWN, "app-bpm"
            m.warnings.append("Bad-Pixel-Map aus Astro Pixel Processor – wird nicht verwendet")
            m.source = detect_source(h, path)
            return m
        t, master = kind, True
        m.is_master = True
        m.type_reason = "app"
        if m.exposure is None:
            e = re.search(r"-E_?(\d+(?:\.\d+)?)s", base)
            m.exposure = float(e.group(1)) if e else None
    elif "astro pixel processor" in sw_all and not raw_type and str(h.get("BITPIX", "")) == "-32":
        m.is_master = True
    try:
        if int(h.get("NCOMBINE") or 0) > 1:
            m.is_master = True
    except (TypeError, ValueError):
        pass
    if t != UNKNOWN:
        m.type, m.type_reason = t, "header"
    if m.exposure is not None and m.exposure < 0:
        m.warnings.append("Belichtungszeit im Header ungültig (%g s) – wird ignoriert" % m.exposure)
        m.exposure = None
    if m.exposure is not None and m.exposure <= 0.001 and not m.is_master and m.type in (LIGHT, UNKNOWN, DARK):
        if m.type == LIGHT:
            m.warnings.append("IMAGETYP=%s, aber Belichtung %.4f s → als Bias behandelt" % (raw_type, m.exposure))
        m.type, m.type_reason = BIAS, "exposure"
    if m.type == UNKNOWN:
        pt = type_from_path(path)
        if pt != UNKNOWN:
            m.type, m.type_reason = pt, "path"

    m.object = _s(h, "OBJECT", "OBJNAME", "TARGET")
    m.filter = _s(h, "FILTER", "FILTER1", "FILTNAME", "INSFLNAM")
    m.gain = _f(h, "GAIN", "EGAIN_SET", "CAM-GAIN")
    if m.gain is None:                                      # APP: "…-IG_120.0-…" im Dateinamen
        g = re.search(r"(?:^|[-_])IG_(\d+(?:\.\d+)?)", base)
        m.gain = float(g.group(1)) if g else None
    m.offset = _f(h, "OFFSET", "BLKLEVEL", "BLACKLEV", "PEDESTAL")
    m.readout = _s(h, "READOUTM", "READMODE", "READOUT")
    m.usb = _f(h, "USBLIMIT", "USBTRAFF", "USB")
    m.binning = int(_f(h, "XBINNING", "BINNING", "CCDXBIN") or 1)
    m.width = int(_f(h, "NAXIS1") or 0) or None
    m.height = int(_f(h, "NAXIS2") or 0) or None
    m.bayer = _s(h, "BAYERPAT", "COLORTYP", "CFAPATT", "BAYER")
    if m.bayer and m.bayer.upper() not in ("RGGB", "BGGR", "GRBG", "GBRG"):
        m.bayer = None
    elif m.bayer:
        m.bayer = m.bayer.upper()
    m.ccd_temp = _f(h, "CCD-TEMP", "CCD_TEMP", "SENSOR-T", "TEMPERAT")
    m.set_temp = _f(h, "SET-TEMP", "SET_TEMP", "SETTEMP")
    m.date_obs = _iso(_s(h, "DATE-OBS", "DATE_OBS"))
    m.date_loc = _iso(_s(h, "DATE-LOC", "DATE_LOC", "LOCALTIM"))
    m.camera = _s(h, "INSTRUME", "CAMERA")
    m.telescope = _s(h, "TELESCOP")
    m.focal_length = _f(h, "FOCALLEN", "FOCAL", "FOCLEN")
    m.pixel_size = _f(h, "XPIXSZ", "PIXSIZE1", "PIXSIZE")
    if m.pixel_size and m.binning > 1 and m.source in ("nina",):
        m.pixel_size = m.pixel_size / m.binning   # NINA schreibt XPIXSZ inkl. Binning
    m.focus_pos = _f(h, "FOCPOS", "FOCUSPOS", "FOCUSER")
    m.focus_temp = _f(h, "FOCTEMP", "FOCUSTEM", "FOCUSTMP")
    m.rotator = _f(h, "ROTATOR", "ROTATANG", "ROTANGLE", "POSANGLE")
    m.pier_side = _s(h, "PIERSIDE", "PIER-SID")
    m.ra = _f(h, "RA", "RA_OBJ") or _sexa(_s(h, "OBJCTRA"), True)
    m.dec = _f(h, "DEC", "DEC_OBJ") or _sexa(_s(h, "OBJCTDEC"), False)
    m.alt = _f(h, "CENTALT", "OBJCTALT", "ALTITUDE")
    m.airmass = _f(h, "AIRMASS")
    m.site_lat = _f(h, "SITELAT", "OBSLAT", "LATITUDE") or _sexa(_s(h, "SITELAT"), False)
    m.site_lon = _f(h, "SITELONG", "OBSLONG", "LONGITUD") or _sexa(_s(h, "SITELONG"), False)
    m.site_elev = _f(h, "SITEELEV", "OBSALT", "ELEVATIO")
    if m.site_lat is None or m.site_lon is None:
        fb = fallback_site()
        if fb:
            m.site_lat, m.site_lon = fb[0], fb[1]
            if m.site_elev is None:
                m.site_elev = fb[2]
            m.site_fallback = True
    m.ambient_temp = _f(h, "AMBTEMP", "AOCAMBT", "TEMPEXT")
    m.dew_point = _f(h, "DEWPOINT", "AOCDEW")
    m.humidity = _f(h, "HUMIDITY", "AOCHUM")
    m.wind = _f(h, "WINDSPD", "AOCWIND")
    raw_date = _s(h, "DATE-OBS", "DATE_OBS")
    if raw_date and m.date_obs is None:
        m.warnings.append("Aufnahmezeit im Header unlesbar (DATE-OBS = %r)" % raw_date[:40])
    m.night = night_of(m.date_loc, m.date_obs, m.site_lon)
    if m.night is None and m.type in (LIGHT, FLAT):
        m.night = night_from_path(path)
        if m.night:
            m.warnings.append("Nacht aus Datei-/Ordnername übernommen (%s), weil die Aufnahmezeit fehlt" % m.night)
    mm = re.match(r"^(\d+\.\d+)_", os.path.basename(path))
    if mm and m.source == "nina":
        m.hfr_from_name = float(mm.group(1))
    return m


def finalize(m: FrameMeta, custom_filters: dict[str, str] | None = None) -> FrameMeta:
    m.filter_kind = flt.classify(m.filter, custom_filters)
    if m.type in (BIAS, DARK, DARKFLAT):
        m.filter_kind = flt.UNKNOWN if m.filter is None else m.filter_kind
    return m


def read_header(path: str):
    """Header einer FITS- oder XISF-Datei als dict-ähnliches Objekt."""
    low = path.lower()
    if low.endswith(FITS_EXT):
        from astropy.io import fits
        with fits.open(path, memmap=True, lazy_load_hdus=True) as hd:
            return dict(hd[0].header)
    if low.endswith(XISF_EXT):
        from .xisf import read_header as xh
        return xh(path)
    raise ValueError("unbekanntes Format: " + path)


class FrameUnusable(ValueError):
    """Datei gelesen, aber als Einzelbild unbrauchbar. Die Meldung ist bereits für Menschen formuliert."""


def friendly_error(e: BaseException) -> str:
    """Technische Lese-Fehler in einen verständlichen Satz übersetzen (Original bleibt im Protokoll)."""
    msg = str(e)
    low = msg.lower()
    if isinstance(e, FrameUnusable):
        return msg
    if isinstance(e, FileNotFoundError):
        return "Datei nicht mehr vorhanden (verschoben oder gelöscht?)"
    if isinstance(e, PermissionError):
        return "kein Lesezugriff auf die Datei (gesperrt oder fehlende Rechte)"
    if isinstance(e, MemoryError):
        return "zu wenig Arbeitsspeicher zum Laden dieses Bildes"
    if "empty or corrupt fits" in low or "empty file" in low:
        return "Datei ist leer oder beschädigt"
    if "no simple card" in low or "does not appear to be a valid fits" in low:
        return "keine gültige FITS-Datei (Inhalt passt nicht zur Dateiendung)"
    if "cannot reshape array" in low or "truncated" in low or "buffer is too small" in low:
        return "Datei unvollständig (Kopie oder Download abgebrochen?)"
    if "unbekanntes format" in low or "xisf" in low:
        return msg
    return "Datei nicht lesbar: " + msg


def read_meta(path: str, custom_filters: dict[str, str] | None = None) -> FrameMeta:
    return finalize(from_header(read_header(path), path), custom_filters)
