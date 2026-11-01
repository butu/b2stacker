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
- **Flats je Nachtgruppe**: Wird die Kamera zwischen Nächten gedreht (Rotator, auch manuell), passen Flats nur zur
  jeweiligen Stellung. Stellung je Nacht aus `ROTATOR` im Header, sonst aus dem Sternfeld gemessen (Paar-Vektoren,
  modulo 180° wegen Meridian-Flip). Ab 2° Unterschied eigene Gruppe mit eigenen Flats.
  WBPP-Umsetzung im Automationsmodus: `groupingKeywordsEnabled=true`, `keywords=ASFLAT pre`; der Schlüsselwert kommt
  aus dem Pfad (`…/ASFLAT_1/…`) – Lights und Flats werden dafür über Verzeichnis-Verknüpfungen (Windows-Junctions,
  keine Kopien) eingebunden. Bias/Darks ohne Schlüssel gelten für alle Gruppen (WBPP: Kalibrier-Gruppen ohne
  Schlüssel passen zu jedem Wert). Aufräumen entfernt nur die Verknüpfungen, nie die Ziele.
- **Protokoll für Agenten**: `<Arbeitsverzeichnis>/logs/events-JJJJ-MM.jsonl` (+ `b2stacker.log`, README dort).
  `astrostack logs --kind error --trace` zeigt die letzten Fehler.
- **Zielnamen** werden vereinheitlicht (Katalog-Präfixe M/NGC/IC/Sh2/vdB/LDN/LBN/Abell/…, Leerzeichen, Bindestriche,
  führende Nullen). Ohne das entstanden für dasselbe Objekt mehrere Projekte. Vor dem Anlegen zeigt ein Vorschau-Scan
  alle Ziele; Koordinaten < 0,5° auseinander → Hinweis „vermutlich dasselbe Feld“.
- **Master-Kalibrierdateien** (masterBias/-Dark/-Flat, FITS oder XISF) werden erkannt und bevorzugt. WBPP erkennt
  Masters nur am IMAGETYP („Master Bias“ usw.) – fehlt das, wird eine korrigierte Kopie übergeben, sonst würde WBPP
  den Master als einzelnes Rohbild behandeln.

## Nachtrag 2026-11-01: Geistersterne durch falsche Registrierung (NGC 1999)

- Symptom: im Stack oben links „dunkle Löcher“ mit Spikes und Farbsäumen, Sternzahl 46 772 (sonst ~1 000), Exzentrizität 0,67.
- Ursache: StarAlignment hat **einen** normalen Frame (`…_0034`) falsch registriert – nur 18 von 527 Sternpaaren
  (Inliers 3 %), Maßstab 0,953. WBPP meldet trotzdem „Registration successful“ und bricht nicht ab. Die
  Verzerrungskorrektur hat das Bild dann verbogen; helle Sterne der Plejaden-Ecke landeten oben links. Die
  Pixel-Ablehnung entfernte die Kerne, Halos/Spikes blieben → Löcher.
- Lösung (0.5.1, `wbpplog.py`): nach jedem Lauf das WBPP-Protokoll lesen; Frames mit Maßstab ≠ 1 (±1 %),
  Inliers < 5 % oder Quality < 0,40 werden ausgelassen und der Lauf **einmal** wiederholt (WBPP-Cache → nur
  Normalisierung + Integration neu). Hinweis landet im Stack (`notes`, `excluded` in stacks.json) und im Logbuch
  (`registration_check`). Frames mit niedriger, aber plausibler Qualität (0,42, Maßstab 0,999) bleiben drin.
- Nebenbefunde: Local Normalization wurde übersprungen (2 Frames ohne Skalenschätzung → „Unable to determine
  the LN reference frame“); Flats mit ROTATOR 0 vs. Lights 92,6°.
