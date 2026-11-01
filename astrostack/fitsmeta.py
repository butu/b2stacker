"""Header-Übersetzer: FITS/XISF-Header verschiedener Aufnahmeprogramme → einheitliches FrameMeta.

Unterstützt (erkannt über SWCREATE/CREATOR/Dateinamen/Ordner):
  NINA, ASIAIR, KStars/Ekos, SharpCap, APT, SGP, MaxIm, PixInsight (Masters).

Der Bildtyp wird in dieser Reihenfolge bestimmt:
  1. IMAGETYP / FRAME / FRAMETYP
  2. Plausibilitätsregel: Belichtung <= 1 ms → Bias (NINA-"Snapshot"-Bias hat IMAGETYP=LIGHT)
  3. Ordner- und Dateinamen (Light/Flat/Dark/Bias/DarkFlat …)
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from . import filters as flt

LIGHT, FLAT, DARK, BIAS, DARKFLAT, UNKNOWN = "light", "flat", "dark", "bias", "darkflat", "unknown"
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
        return (self.camera, self.gain, self.offset, (self.readout or "").lower(), self.binning, self.width, self.height)

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
    return s


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

def from_header(h, path: str) -> FrameMeta:
    """Header-Mapping (astropy Header oder dict) → FrameMeta."""
    m = FrameMeta(path=path)
    m.source = detect_source(h, path)
    m.software = _s(h, "SWCREATE", "CREATOR", "PROGRAM")

    raw_type = _s(h, "IMAGETYP", "FRAME", "FRAMETYP", "IMAGETYPE")
    t, master = type_from_string(raw_type)
    m.is_master = master or os.path.basename(path).lower().startswith("master")
    m.exposure = _f(h, "EXPTIME", "EXPOSURE", "EXP")
    if t != UNKNOWN:
        m.type, m.type_reason = t, "header"
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
    m.ambient_temp = _f(h, "AMBTEMP", "AOCAMBT", "TEMPEXT")
    m.dew_point = _f(h, "DEWPOINT", "AOCDEW")
    m.humidity = _f(h, "HUMIDITY", "AOCHUM")
    m.wind = _f(h, "WINDSPD", "AOCWIND")
    m.night = night_of(m.date_loc, m.date_obs, m.site_lon)
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


def read_meta(path: str, custom_filters: dict[str, str] | None = None) -> FrameMeta:
    return finalize(from_header(read_header(path), path), custom_filters)
