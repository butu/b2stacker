"""Rig-Diagnose: sucht Hinweise auf Probleme mit Fokus, Nachführung, Optik, Dithering, Kamera, Wetter."""
import datetime
import json
import os

import numpy as np

from ..analyze import thumb_name


def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return float('nan')

def _shift(a, b):
    from scipy import ndimage as ndi
    def prep(t):
        t = t - ndi.gaussian_filter(t, 8); sd = t.std() or 1
        return np.clip(t, -3 * sd, 20 * sd)
    a, b = prep(a), prep(b)
    F = np.fft.fft2(a) * np.conj(np.fft.fft2(b)); c = np.fft.fftshift(np.fft.ifft2(F / np.maximum(np.abs(F), 1e-9)).real)
    y, x = np.unravel_index(np.argmax(c), c.shape); cy, cx = np.array(c.shape) // 2
    def sub(v0, vm, vp):
        d = vm - 2 * v0 + vp; return 0.0 if d == 0 else 0.5 * (vm - vp) / d
    y1, x1 = min(max(y, 1), c.shape[0] - 2), min(max(x, 1), c.shape[1] - 2)
    dy = y - cy + sub(c[y1, x1], c[y1 - 1, x1], c[y1 + 1, x1]); dx = x - cx + sub(c[y1, x1], c[y1, x1 - 1], c[y1, x1 + 1])
    return float(np.hypot(dy, dx) * 16)   # Thumbnail-Pixel = 16 native Pixel

def rig_diagnostics(rows, scale, analysis_dir):
    """Looks for problems with the rig. Returns checks (good/warning/critical/info) plus chart data."""
    rr = [r for r in rows if r.get('tiles_fwhm')]
    if not rr: return None
    rr.sort(key=lambda r: r['t'])
    use = [r for r in rr if r['status'] not in ('rejected', 'suggest')]
    checks = []
    def add(level, area, title, text): checks.append(dict(level=level, area=area, title=title, text=text))
    # ---- star size / HFR realism
    hfr = [_f(r['hfr_name']) for r in use if not np.isnan(_f(r['hfr_name']))]
    if hfr and scale:
        hm = float(np.median(hfr)); fw_as = 2 * hm * scale
        lvl = 'good' if fw_as < 4.5 else 'warning' if fw_as < 6 else 'critical'
        add(lvl, 'Schärfe', 'HFR realistisch: %.2f px = %.1f" (≈ FWHM %.1f")' % (hm, hm * scale, fw_as),
            'Bei %.2f"/px und typischem mitteleuropäischem Seeing von 2–4" sind %.1f" FWHM %s. Spannweite der Nächte: %.2f–%.2f px.' % (
                scale, fw_as, 'plausibel und gut' if lvl == 'good' else 'eher weich – Seeing, Fokus oder Nachführung prüfen',
                min(np.median([_f(r['hfr_name']) for r in use if r['night'] == n]) for n in set(r['night'] for r in use)),
                max(np.median([_f(r['hfr_name']) for r in use if r['night'] == n]) for n in set(r['night'] for r in use))))
    # ---- eccentricity realism (from frame metrics)
    ecc = [r['ecc'] for r in use if r['ecc']]
    if ecc:
        em = float(np.median(ecc))
        lvl = 'good' if em < 0.45 else 'warning' if em < 0.55 else 'critical'
        add(lvl, 'Nachführung', 'Exzentrizität %.2f' % em,
            {'good': 'Unter 0,45: runde Sterne, Nachführung und Guiding passen.', 'warning': '0,45–0,55: leicht ovale Sterne. Guiding-Log, Balance und Wind prüfen.',
             'critical': 'Über 0,55: deutlich elongierte Sterne. Nachführung, Guiding oder Optik prüfen.'}[lvl])
    # ---- pier side (meridian flip) comparison
    piers = {}
    for side in ('East', 'West'):
        s = [r for r in use if r['pier'] == side]
        if len(s) >= 8:
            piers[side] = dict(n=len(s), coh=float(np.nanmedian([_f(r['coherence']) for r in s])), ecc=float(np.nanmedian([r['ecc'] for r in s if r['ecc']])),
                               hfr=float(np.nanmedian([_f(r['hfr_name']) for r in s])))
    if len(piers) == 2:
        e, w = piers['East'], piers['West']; worse = 'West' if w['coh'] > e['coh'] else 'East'
        ratio = max(w['coh'], e['coh']) / max(min(w['coh'], e['coh']), 0.05)
        side_de = {'East': 'Ostseite', 'West': 'Westseite'}
        if ratio > 1.5 and max(w['coh'], e['coh']) > 0.25:
            add('warning', 'Nachführung', 'Sterne auf der %s gleichgerichtet elongiert' % side_de[worse],
                'Richtungs-Kohärenz %s %.2f gegenüber %.2f auf der anderen Seite (0 = zufällig, 1 = alle gleich gerichtet). Weil die Optik bei beiden Seiten gleich ist, deutet das auf die Montierung: Balance nach dem Meridian Flip, Spiel in DEC/RA oder Kabelzug. Guiding-RMS beider Seiten vergleichen.' % (side_de[worse], max(w['coh'], e['coh']), min(w['coh'], e['coh'])))
        else:
            add('good', 'Nachführung', 'Ost- und Westseite gleich gut', 'Sternform vor und nach dem Meridian Flip ohne nennenswerten Unterschied (Kohärenz %.2f / %.2f).' % (e['coh'], w['coh']))
    # ---- field: curvature / tilt / spacing
    maps = {}
    for f in sorted(set(r['filter'] for r in use)):
        s = [r for r in use if r['filter'] == f]
        T = np.array([[np.nan if v is None else v for v in json.loads(r['tiles_fwhm'])] for r in s], dtype=float)
        E = np.array([[np.nan if v is None else v for v in json.loads(r['tiles_ecc'])] for r in s], dtype=float)
        A = np.array([[np.nan if v is None else v for v in json.loads(r['tiles_ang'])] for r in s], dtype=float)
        Tn = T / T[:, 4:5]
        ang = []
        for k in range(9):
            col = A[:, k][~np.isnan(A[:, k])]
            z = np.mean(np.exp(2j * np.radians(col))) if len(col) else 0
            ang.append(round(float(np.degrees(np.angle(z)) / 2), 1) if len(col) else None)
        maps[f] = dict(fwhm=[round(float(v), 3) for v in np.nanmedian(Tn, 0)], ecc=[round(float(v), 3) for v in np.nanmedian(E, 0)], ang=ang,
                       radial=round(float(np.nanmedian([_f(r['radial']) for r in s])), 2), n=len(s))
    allT = np.array([[np.nan if v is None else v for v in json.loads(r['tiles_fwhm'])] for r in use], dtype=float)
    allT = np.nanmedian(allT / allT[:, 4:5], 0)
    corner = float(np.nanmean(allT[[0, 2, 6, 8]]))
    lr = abs(np.nanmean(allT[[0, 3, 6]]) - np.nanmean(allT[[2, 5, 8]])); tb = abs(np.nanmean(allT[[0, 1, 2]]) - np.nanmean(allT[[6, 7, 8]]))
    if np.isfinite(corner):
        lvl = 'good' if corner < 1.12 else 'warning' if corner < 1.3 else 'critical'
        add(lvl, 'Optik', 'Bildfeld: Ecken %+.0f %% gegenüber Mitte' % ((corner - 1) * 100),
            'Bis ~+12 %% ist für einen Newton mit Komakorrektor und APS-C-Sensor sehr gut. %s' % (
                '' if lvl == 'good' else 'Deutlich größere Sterne am Rand: Abstand Komakorrektor–Sensor (Backfocus) prüfen.'))
    else:
        add('info', 'Optik', 'Bildfeld: nicht messbar', 'Zu wenige Sterne in den Bildecken, um Mitte und Rand zu vergleichen.')
    if np.isfinite(lr) and np.isfinite(tb):
        lvl = 'good' if max(lr, tb) < 0.06 else 'warning'
        add(lvl, 'Optik', 'Verkippung (Tilt): %s' % ('keine erkennbar' if lvl == 'good' else 'Hinweis'),
            'Sternschärfe links/rechts unterscheidet sich um %.0f %%, oben/unten um %.0f %%.%s' % (lr * 100, tb * 100, '' if lvl == 'good' else ' Eine Seite ist unschärfer: Sensorverkippung oder Kollimation prüfen.'))
    for f, m in maps.items():
        ce = [m['ecc'][k] for k in (0, 2, 6, 8)]
        spread = max(ce) - min(ce)
        if spread > 0.15:
            names = ['oben links', 'oben rechts', 'unten links', 'unten rechts']; worst = names[int(np.argmax(ce))]
            add('warning', 'Optik', '%s: Sterne %s stärker elongiert' % (f, worst),
                'Exzentrizität in den Ecken %s. Eine einzelne auffällige Ecke deutet auf leichte Verkippung, Kollimation oder Komakorrektor-Position.' % ', '.join('%.2f' % v for v in ce))
    rad = float(np.nanmedian([_f(r['radial']) for r in use]))
    if not np.isfinite(rad):
        pass
    elif abs(rad) > 0.35:
        add('warning', 'Optik', 'Elongation %s zum Bildrand' % ('radial' if rad > 0 else 'tangential'),
            'Radial-Score %.2f. %s' % (rad, 'Radial gestreckte Sterne am Rand: typisches Koma, Backfocus des Komakorrektors prüfen.' if rad > 0 else 'Tangential gestreckt: Astigmatismus oder Bildfeldwölbung, Kollimation prüfen.'))
    else:
        add('good', 'Optik', 'Koma/Astigmatismus unauffällig', 'Keine systematische radiale oder tangentiale Streckung am Rand (Score %.2f).' % rad)
    # ---- focus: autofocus runs and drift
    from itertools import groupby
    focus = []; bad_af = []; seg_len = []; drift = []
    for night, v in groupby(rr, key=lambda r: r['night']):
        v = list(v); hn = float(np.nanmedian([_f(r['hfr_name']) for r in v]))
        t0 = datetime.datetime.fromisoformat(v[0]['t'][:19])
        pts = [dict(h=round((datetime.datetime.fromisoformat(r['t'][:19]) - t0).total_seconds() / 3600, 3), hfr=_f(r['hfr_name']),
                    focpos=_f(r['focpos']), temp=_f(r['foctemp']), filter=r['filter'], frame=r['frame'], status=r['status']) for r in v]
        for fp, sgi in groupby(pts, key=lambda q: q['focpos']):
            sg = list(sgi); hs = [q['hfr'] for q in sg]
            seg_len.append(len(sg))
            if len(sg) >= 3 and (np.median(hs) > 1.15 * hn or hs[-1] / hs[0] > 1.3):
                bad_af.append('%s Position %d (HFR %.2f→%.2f, Nacht-Median %.2f)' % (night[5:].replace('-', '.'), fp, hs[0], hs[-1], hn))
        hh = np.array([q['h'] for q in pts]); hv = np.array([q['hfr'] for q in pts]); am = np.array([r['airmass'] or 1 for r in v])
        if len(v) >= 5 and hh.max() > 1:
            slope = np.polyfit(hh, hv, 1)[0] / np.median(hv) * 100
            slope_c = np.polyfit(hh, hv / am ** 0.6, 1)[0] / np.median(hv / am ** 0.6) * 100
            if slope > 6 and slope_c > 6: drift.append('%s (+%.0f %%/h)' % (night[5:].replace('-', '.'), slope))
        focus.append(dict(night=night, pts=pts, median=hn))
    if bad_af:
        add('warning', 'Fokus', 'Autofokus-Läufe mit schwachem Ergebnis: %d' % len(bad_af),
            'Danach war die HFR deutlich höher oder stieg schnell an: ' + '; '.join(bad_af) + '. Autofokus-Kurve dieser Läufe in NINA ansehen (zu wenige Schritte, Wolken, Seeing-Spitze?).')
    else:
        add('good', 'Fokus', 'Autofokus zuverlässig', 'Alle Autofokus-Läufe lieferten eine HFR im Bereich des Nacht-Medians.')
    if drift:
        add('warning', 'Fokus', 'Fokus driftet in der Nacht', 'HFR steigt schneller als durch die sinkende Höhe erklärbar: ' + ', '.join(drift) + '. Autofokus öfter oder per Temperatur auslösen.')
    else:
        add('good', 'Fokus', 'Kein Fokus-Drift', 'HFR-Verlauf innerhalb der Nächte erklärt sich durch Höhe und Seeing.')
    if seg_len:
        add('info', 'Fokus', 'Autofokus etwa alle %.0f Frames' % np.median(seg_len),
            'Das sind rund %.0f Minuten. Häufig genug; wenn Zeit verloren geht, Auslösung nach Temperaturänderung (z. B. 1 °C) statt fester Frame-Zahl.' % (np.median(seg_len) * (rows[0]['exp'] or 600) / 60))
    tp = [(q['temp'], q['focpos']) for n in focus for q in n['pts'] if not np.isnan(q['temp']) and not np.isnan(q['focpos'])]
    if len(set(tp)) > 8:
        x = np.array([a for a, b in tp]); y = np.array([b for a, b in tp]); rcoef = float(np.corrcoef(x, y)[0, 1]); sl = float(np.polyfit(x, y, 1)[0])
        if abs(rcoef) > 0.8:
            add('info', 'Fokus', 'Temperaturkoeffizient %.1f Schritte/°C' % sl, 'Fokusposition folgt der Temperatur sehr deutlich (r = %.2f). In NINA als Temperaturkompensation eintragen.' % rcoef)
        else:
            add('info', 'Fokus', 'Kein klarer Temperatur-Zusammenhang', 'Fokusposition und Temperatur korrelieren nur schwach (r = %.2f, %.1f Schritte/°C). Autofokus per Zeit/HFR bleibt richtig.' % (rcoef, sl))
    # ---- dithering
    dith = []; frac = []; amp = []
    for night, v in groupby(rr, key=lambda r: r['night']):
        v = [r for r in v if r['status'] != 'rejected']; sh = []
        for a, b in zip(v, v[1:]):
            if a['pier'] != b['pier']: continue
            pa = os.path.join(analysis_dir, 'thumbs', thumb_name(a['path']))
            pb = os.path.join(analysis_dir, 'thumbs', thumb_name(b['path']))
            if not (os.path.exists(pa) and os.path.exists(pb)): continue
            d = _shift(np.load(pb), np.load(pa))
            if d < 200: sh.append(round(d, 1))
        if sh:
            dith.append(dict(night=night, shifts=sh)); frac.append(np.mean(np.array(sh) > 2)); amp += [x for x in sh if x > 2]
    if dith:
        fr = float(np.mean(frac)); am_ = float(np.median(amp)) if amp else 0
        if fr > 0.8 and am_ >= 5: lvl, txt = 'good', 'Fast jeder Frame verschoben (%.0f %%), Versatz %.0f px. Ideal für die Rejection ohne Darks.' % (fr * 100, am_)
        elif fr > 0.3: lvl, txt = 'warning', 'Nur %.0f %% der Frames verschoben (etwa jeder %.0f.), Versatz %.0f px. Reicht meist, aber ohne Darks besser jeden Frame dithern: Hotpixel und Walking Noise werden sonst nicht ganz entfernt.' % (fr * 100, 1 / max(fr, 0.01), am_)
        else: lvl, txt = 'critical', 'Kaum Versatz zwischen den Frames (%.0f %%). Dithering scheint nicht aktiv. Ohne Darks bleiben Hotpixel im Stack.' % (fr * 100)
        add(lvl, 'Dithering', 'Dithering %s' % {'good': 'aktiv', 'warning': 'nur jeden %.0f. Frame' % (1 / max(fr, 0.01)), 'critical': 'nicht erkennbar'}[lvl], txt)
    # ---- cooling, dew, clipping
    bad_t = sorted(set(r['night'] for r in rr if r['temp'] is not None and r['settemp'] is not None and abs(r['temp'] - r['settemp']) > 1))
    add('warning' if bad_t else 'good', 'Kamera', 'Kühlung' + (' nicht erreicht' if bad_t else ' stabil'),
        ('Solltemperatur verfehlt in: ' + ', '.join(n[5:].replace('-', '.') for n in bad_t) + '. Kühlleistung bei warmen Nächten reicht nicht; Sollwert dann z. B. auf −5 °C setzen, damit alle Frames gleich sind.') if bad_t else 'Sensor hielt die Solltemperatur in allen Nächten.')
    sets = sorted(set(r['settemp'] for r in rr if r['settemp'] is not None))
    if len(sets) > 1:
        per = {}
        for r in rr: per.setdefault(r['settemp'], set()).add(r['night'][5:].replace('-', '.'))
        add('info', 'Kamera', 'Unterschiedliche Sensortemperaturen',
            'Sollwerte: ' + '; '.join('%.0f °C (%s)' % (k, ', '.join(sorted(v))) for k, v in sorted(per.items())) + '. Ohne Darks unkritisch, das Rauschen ist bei wärmerem Sensor etwas höher. Für spätere Darks einen festen Sollwert wählen, den die Kamera auch im Sommer erreicht.')
    dew = []
    for night, v in groupby(rr, key=lambda r: r['night']):
        v = list(v); sp = [_f(r['ambtemp']) - _f(r['dewpoint']) for r in v]
        sp = [x for x in sp if not np.isnan(x)]
        if sp and np.median(sp) < 1.5: dew.append('%s (%.1f °C)' % (night[5:].replace('-', '.'), np.median(sp)))
    if dew:
        add('warning', 'Wetter', 'Taupunkt fast erreicht in %d Nächten' % len(dew),
            'Abstand Luft- zu Taupunkttemperatur: ' + ', '.join(dew) + '. Taukappe und Heizband für Fangspiegel/Korrektor prüfen; beschlagene Optik zeigt sich als sinkende Sternzahl und wachsende Halos.')
    p001 = [_f(r['p001']) for r in rr]; sat = [_f(r['sat_frac']) for r in rr]
    add('good' if np.nanmin(p001) > 5 else 'warning', 'Kamera', 'Offset ' + ('passt' if np.nanmin(p001) > 5 else 'zu niedrig'),
        'Dunkelste Pixel liegen bei %.0f ADU: %s' % (np.nanmin(p001), 'kein Abschneiden im Schwarzen.' if np.nanmin(p001) > 5 else 'Schwarz wird abgeschnitten, Offset erhöhen.'))
    add('good' if np.nanmax(sat) < 1e-3 else 'warning', 'Kamera', 'Belichtung ' + ('passt' if np.nanmax(sat) < 1e-3 else 'zu lang'),
        'Gesättigte Pixel: max. %.3f %% des Bildes.' % (np.nanmax(sat) * 100))
    order = {'critical': 0, 'warning': 1, 'info': 3, 'good': 2}
    checks.sort(key=lambda c: order[c['level']])
    return dict(checks=checks, maps=maps, focus=focus, dither=dith, piers=piers)

