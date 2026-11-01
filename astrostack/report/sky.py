"""Höhe/Azimut von Objekt, Mond und Sonne pro Nacht (Abend bis Morgen, sommerzeitfest)."""
import datetime
import numpy as np


def sky_tracks(rows, meta0, tzname='Europe/Vienna'):
    """Altitude/azimuth of target, moon and sun for every night (local evening to morning)."""
    import warnings; warnings.filterwarnings('ignore')
    from astropy.time import Time
    from astropy.coordinates import EarthLocation, AltAz, SkyCoord, get_body
    from astropy.utils import iers
    import astropy.units as u
    iers.conf.auto_download = False
    try:
        from zoneinfo import ZoneInfo; tz = ZoneInfo(tzname)
    except Exception:
        tz = None
    if meta0.site_lat is None or meta0.site_lon is None or meta0.ra is None or meta0.dec is None:
        return None
    loc = EarthLocation(lat=meta0.site_lat * u.deg, lon=meta0.site_lon * u.deg, height=(meta0.site_elev or 0) * u.m)
    tgt = SkyCoord(meta0.ra * u.deg, meta0.dec * u.deg)
    out = []
    nights = []
    for r in rows:
        if r['night'] and r['night'] not in nights and r['t']: nights.append(r['night'])
    for night in nights:
        d0 = datetime.datetime.fromisoformat(night + 'T12:00:00')
        if tz: d0 = d0.replace(tzinfo=tz)
        else: d0 = d0.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=1)))
        step = 10
        u0 = d0.astimezone(datetime.timezone.utc)   # local noon in UTC; x = 12 + real elapsed hours (DST-safe)
        utc = [u0 + datetime.timedelta(minutes=step * i) for i in range(int(21 * 60 / step) + 1)]
        local = utc
        t = Time([x.replace(tzinfo=None) for x in utc], scale='utc')
        fr = AltAz(obstime=t, location=loc)
        oa = tgt.transform_to(fr)
        mo = get_body('moon', t, loc); su = get_body('sun', t, loc)
        ma = mo.transform_to(fr); sa = su.transform_to(fr)
        illum = (1 - np.cos(su.separation(mo).rad)) / 2
        sun_alt = sa.alt.deg
        dark = np.nonzero(sun_alt < -6)[0]
        if len(dark) == 0: continue
        i0, i1 = max(dark[0] - 6, 0), min(dark[-1] + 6, len(local) - 1)
        xs = [round((x - u0).total_seconds() / 3600 + 12, 3) for x in utc[i0:i1 + 1]]
        fr_rows = [r for r in rows if r['night'] == night and r['t']]
        ft = Time([r['t'][:23] for r in fr_rows], scale='utc')
        ffr = AltAz(obstime=ft, location=loc)
        foa = tgt.transform_to(ffr); fmo = get_body('moon', ft, loc).transform_to(ffr)
        frames = []
        for k, r in enumerate(fr_rows):
            ut = datetime.datetime.fromisoformat(r['t'][:19]).replace(tzinfo=datetime.timezone.utc)
            frames.append(dict(x=round((ut - u0).total_seconds() / 3600 + 12, 3), alt=round(float(foa[k].alt.deg), 1), az=round(float(foa[k].az.deg), 1),
                               malt=round(float(fmo[k].alt.deg), 1), maz=round(float(fmo[k].az.deg), 1),
                               filter=r['filter'], status=r['status'], frame=r['frame']))
        sl = slice(i0, i1 + 1)
        out.append(dict(night=night, t0=int(u0.timestamp() * 1000), filters=sorted(set(r['filter'] for r in fr_rows)), x=xs,
                        obj=[round(v, 1) for v in oa.alt.deg[sl]], objaz=[round(v, 1) for v in oa.az.deg[sl]],
                        moon=[round(v, 1) for v in ma.alt.deg[sl]], moonaz=[round(v, 1) for v in ma.az.deg[sl]],
                        sun=[round(v, 1) for v in sun_alt[sl]], illum=round(float(np.median(illum[sl])) * 100),
                        frames=frames))
    return out

