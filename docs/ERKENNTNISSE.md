# Erkenntnisse AstroStacking (Stand 31.10.2026)

## Setup
- ATR2600C (IMX571, Farbkamera), Gain 100 / Offset 50, High Conversion Gain, Sensor -10 °C
- Lacerta 1200 mm f/4 (Newton, Komakorrektor), 0,646"/px, Filter RGB und HaO3
- Keine Darks; Dithering in NINA; Bias 2024-11-05 (Kopien mit IMAGETYP=BIAS in calib\)

## Flats
- NICHT zu hell: ab ca. 48k ADU wird der hellste Kanal (Grün) nichtlinear gestaucht.
  Folge: runder Farbring im Stack (Mitte grün, Rand magenta), Sterntest merkt es nicht.
- NINA Flat-Wizard: Ziel ca. 35 %, Toleranz 10 %. Hellster Kanal in der Mitte < 40k ADU,
  dunkelster Kanal am Rand > 5k ADU. Panel 5-10 min aufwärmen lassen.
- Bewährt (31.10.2026): RGB 0,047 s -> G-Mitte 35,8k; HaO3 1,0 s -> G 35,1k, R 14k; Farbabfall < 1 %.
- Messreihe Helligkeit -> Farbfehler am Rand (R/B gegen G):
  39-46k: -1...-1,5 % | 47-48,5k: -1,5...-2 % | 49k: -3 % | 49,6k: -5,5...-6,4 %
- Jeder Filter eigene Flats (Staub auf dem Filter, Spektrum). 0,05 s Belichtung ist ok (kein Flackern).
- Auslesemodus/Gain/Offset von Lights, Flats, Bias müssen identisch sein.
- Problematische Sätze: FLAT_2025-07-13 (49k, ~3 %), HaO3-Skyflats 2025-03-18 (49,6k),
  FLAT_2026-10-31_Serie1_verworfen (49,6k, ~6 %), FLAT_2026-04-14 (6 LCG-Frames), FLAT_2025-02-05 (LCG).

## WBPP-Automation (PixInsight, WBPP 2.7.8)
- Kanaltrennung (debayerOutputMethod=separated) ist im Automation-Modus DEFEKT: WBPP zerlegt zwar,
  registriert/stackt dann aber die unzerlegten CFA-Frames -> einkanaliger, unbrauchbarer Master.
- Daher: HaO3 als Farbbild stacken, danach Ha = R, OIII = (G+B)/2 (postprocess.js rgb= bzw.
  tools/xisf_split_haoiii.py). Per-Kanal-Ausrichtung entfaellt -> in der Bearbeitung OIII per
  StarAlignment auf Ha ausrichten (Versatz bei vdB 131 ~0,6").
- Auch "RGB getrennt registrieren" bei tiefen Objekten geht so nicht automatisch.
- Warnung "negative pixel values after calibration" bei dunklen HaO3-Frames: WBPP setzt automatisch
  einen Sockel (17 DN), unkritisch.
- Watcher-Aenderungen wirken erst nach Neustart des Watchers.

## Prüfmethoden
- Farbring: Hintergrund-Farbkarte des Stacks (B/G, R/G) + Anteil "rein radial"; Flat-Farbabfall
  Mitte->Rand vergleichen. Additive Fehler (Sockel, Streulicht, nichtlineares Flat) zeigen sich NICHT an Sternfarben.
- Kanalversatz RGB (atmosph. Dispersion): bei vdB 131 ~0,2" (RGB, hoch) bzw. ~0,6" (HaO3, tief) -> RGB getrennt
  registrieren nur unter ca. 30° Höhe.

## Rig-Befunde vdB 131
- Westseite (nach Meridian Flip): Sterne gleichgerichtet elongiert (Kohärenz 0,40 vs 0,18) -> Balance/Spiel/Kabelzug.
- RGB: Ecke unten rechts elongierter (e 0,70 vs 0,49-0,59) -> leichter Hinweis Kollimation/Tilt.
- Dithering nur ca. jeder 3. Frame, ~7 px -> besser jeden Frame (keine Darks!).
- Taupunkt in 6 Nächten < 1,5 °C Abstand -> Heizung/Taukappe prüfen.
- Autofokus 12.10. Position 2158 schwach (HFR 2,8 -> 4,1).
- HFR realistisch: ~2,6 px = 1,7" (FWHM ~3,4"), Exzentrizität 0,42, Bildfeld eben (+5 % Ecken).

## Offene Punkte / TODO
- Diagnose erweitern: Flat-Helligkeit pro Kanal (> 45k Warnung), Farbabfall pro Flat-Frame,
  Auslesemodus-Abgleich, runder Farbring im Stack.
- vdB 131: RGB mit neuen Flats (35 %) neu stacken, HaO3 mit neuen HaO3-Flats.
- Sh2 82 prüfen (Flats 2025-07-13 zu hell).

## Nachtrag 01.11.2026
- Abstürze bei ImageIntegration (0xC000041D, 0xC00000FD) und "Out of memory" kamen von zu wenig
  freiem Arbeitsspeicher (abgestürzte PixInsight-Instanzen blieben im Speicher, dazu die
  Linux-VM der Claude-App). Nach Aufräumen lief HaO3 mit 113 Frames in 1:24 h durch.
- PC ging nachts in den Energiesparmodus -> Local Normalization "dauerte" 8 h.
- Ergebnis vdB 131 (Flats bei 35 %): Farbring weg (B/G radial 5 %, vorher 45 %);
  HaO3 deckungsgleich mit RGB (Ha 0,28 px, OIII 0,30 px, OIII-Ha 0,36 px).


## Nachtrag 01.11.2026 (b2stacker 0.2)

- **Bias liegen bei NINA oft unter `Bias/SNAPSHOT/…`** (Snapshot-Modus, IMAGETYP=LIGHT, 0 s). Snapshot-Ordner dürfen
  daher nicht pauschal übersprungen werden: Kalibrierframes daraus verwenden, Test-Lights nicht.
- **Himmelsflats haben pro Frame eine andere Belichtung** (Dämmerung). Flats deshalb nach *Sitzung* gruppieren
  (Zeitlücke > 10 min oder Belichtungssprung > 2,5×), nicht nach Belichtung – sonst zerfällt eine Himmelsflat-Serie
  in Einzelframes, und im schlimmsten Fall wird ein einzelnes Flat verwendet (so passiert bei vdB 15, 01.11.).
- **Anzahl der Flats gehört in die Bewertung**: unter 5 Frames stark abwerten, unter 15 leicht. Ordner mit
  `_alt`/`_verworfen` nie verwenden.
- **Farbsäume**: WBPP richtet das debayerte Farbbild als Ganzes aus. Der verbleibende R/B-Versatz (atmosphärische
  Dispersion) wird am Master an den Sternen gemessen und ab 0,15 px per Subpixel-Verschiebung korrigiert.
  Bei Dualband liegt Ha dadurch exakt auf OIII.
- **Qualitätskennzahlen** am linearen Master: Rauschen (aus Nachbarpixel-Differenzen, unempfindlich gegen Nebel),
  FWHM/HFR, Sternzahl, Exzentrizität, Farbversatz, Verlauf. Vergleichbar innerhalb derselben Kamera/Einstellungen.
- **Dateinamen**: `Ziel_Filter_Datum_Stunden.xisf` – eindeutig, nichts wird überschrieben; Historie in `stacks.json`
  mit Pfaden relativ zum Projekt (Arbeitsverzeichnis lässt sich verschieben).
- **Kameranamen unterscheiden sich je nach Treiber** (z. B. `ATR3CMOS26000KPA` nativ 2024 vs. `ATR2600C(USB2.0)`,
  `ZWO CCD ASI2600MC Pro` vs. `ZWO ASI2600MC Pro`). Namen werden normalisiert; ein abweichender Name bei gleicher
  Sensorgröße, Gain, Offset, Auslesemodus und Binning ist nur ein Hinweis, kein Ausschluss (vdB 15, 01.11.).
