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

## WBPP 3.2.0 (PixInsight 1.9.6, geprüft 04.11.2026 am Quellcode)
- Installierte Version laut `BPP-global.js`: WBPP 3.2.0 (12.10.2026); die Datei-Köpfe sagen noch 3.1.2.
- Fast Integration: `autoIntegrationMode` ist standardmäßig `true` und schaltet Light-Gruppen ab
  200 Frames (vor 3.2.0: 150) still auf Fast Integration um. Steht nicht in der `paramList`-Hilfe, wird aber
  im Automation-Modus wie jeder Parameter übernommen (`importParametersUnguarded`) → b2stacker übergibt
  `autoIntegrationMode=false` (ab 0.10.1).
- `bestFrameRefernceMethod` (alte Schreibweise) wird in 3.2.0 noch als Alias von `bestFrameReferenceMethod` gelesen.
- Eigene Skripte brauchen `#engine v8` als erste Zeile (Pflicht, vor allen anderen Direktiven). Ohne die Zeile
  läuft ein `-r=`-Skript unter 1.9.6 nicht und PixInsight endet trotzdem mit Code 0 – ohne Meldung.
  Event-Script und Pipeline-Builder-Script dagegen ohne `#engine` (WBPP kompiliert sie selbst).
- „Allow execution of unsigned scripts“ ist bei Benjamin an; WBPP ist signiert, eigene Skripte nicht.
- Im Automatik-Modus speichert WBPP keinen Zwischenstand (`persistProgress`) → kein Recovery-Dialog nach Abbruch.
- Fortschritt offiziell über das Event-Script (`onPipelineStart`, `onOperationStart`, `onOperationDone`,
  `onPipelineEnd`; Beispiel `BatchPreprocessing/examples/event-script-example.js`).
- Parameterliste: `automationMode=true,paramList` bzw. Alt+A im WBPP-Dialog; Quelle `BPP-automation.js`.

## Weg B mit echtem PixInsight (04.11.2026, PixInsight 1.9.6 build 1713, WBPP 3.2.0)
Getestet über das MCP-Werkzeug `run_pixinsight_script` mit dem Selbsttest-Datensatz (8 Lights in 2 Nächten,
je 5 Flats, 5 Bias, Ordnername mit Komma) und 210 kleinen Test-Lights. Skripte: `astrostack/pjsr/b2launch.js`.

| Testpunkt | Ergebnis |
|---|---|
| 1 Versionen, Parameter | PixInsight 1.9.6.0 build 1713, WBPP 3.2.0. Parameternamen = Eigenschaften von `engine` (`BPP-ParametersManager.js`). |
| 2 Pfade mit Leerzeichen und Komma | funktioniert: echte Pfade aus der JSON-Auftragsdatei, kein Linken/Kopieren. Nur Skript und Auftrag stehen auf der Kommandozeile. |
| 3 Weg A vs. B | **Weg B** (eigenes Startskript, WBPP per `#include "…/BPP-main.js"`) funktioniert und ist gewählt. Weg A nicht nötig. |
| 4 Fast Integration | Weg B: 210 Lights, ohne Einstellung → Post-Gruppe `fastIntegration: true`; mit `autoIntegrationMode=false` → aus. Alter CLI-Weg (0.10.1): ohne Schalter plant WBPP „Fast Integration“, mit `autoIntegrationMode=false` normale Integration. |
| 5 Flat-Gruppen ohne ASFLAT | funktioniert: Gruppen-Schlüssel `B2SESSION` als `fileItem.fileKeywords` vor `engine.rebuild()`; „Master Flat automatic match“ wählt je Nacht den richtigen Master-Flat. Bias ohne Schlüssel passt zu allen. |
| 6 Drizzle je Gruppe | funktioniert: im Startskript `group.setDrizzleData({enabled, scale, dropShrink})` an den Light-Gruppen nach der Kalibrierung → zusätzliches Master `…_drizzle_2x.xisf` (2400×1600 aus 1200×800). |
| 7 Getrenntes Debayern | **funktioniert in 3.2.0** über Weg B: `debayerOutputMethod=1` → Kanäle einzeln registriert (`…_c_d_R_r.xisf`), Master R/G/B mono, ohne CFA-Muster (Schachbrett-Abweichung < 0,05 %). Der 2.7.8-Fehler ist damit für den Neubau erledigt. |
| 8 Event-Script | funktioniert: `onPipelineStart/OperationStart/OperationDone/PipelineEnd` schreiben eine Statusdatei (Index, Name, Endzeit). Index ist lückenhaft (nur verfolgte Schritte) – Fortschritt = Index/Gesamtzahl. Event-Script ohne `#engine` (WBPP kompiliert es). |
| 9 Abbruch / Recovery | geprüft: Abbruch mitten in der Pipeline beendet PixInsight (Code 1), Ergebnisdatei steht auf Phase „run“, `ok: false`. Der nächste Lauf startet sofort normal, kein Recovery-Dialog (Automatik-Modus speichert keinen Zwischenstand). Achtung: Befehle über die Fernverbindung kommen bis zu ~10 s verzögert an. |
| 10 Signatur | nicht nötig, solange „Allow execution of unsigned scripts“ an ist (bei Benjamin an). Pflicht ist `#engine v8` als erste Zeile. |
| 11 NAS/OneDrive | offen. |

Weitere Befunde:
- `engine.rejection[]` enthält ImageIntegration-Konstanten, nicht 0–5: Nummern mit `engine.rejectionFromIndex()`
  umrechnen (sonst wird 1 als MinMax gelesen). Reihenfolge wie in der Hilfe: 0 Percentile, 1 Winsorized,
  2 Linear Fit, 3 ESD, 4 RCR, 5 Auto.
- `autocrop=false` wird beachtet (keine `_autocrop`-Variante).
- WBPP legt bei LN ein `LN_Reference_…xisf` im Master-Ordner ab; das Light-Master beginnt mit `masterLight`.
- Master-Bias/-Flat entstehen im Master-Ordner mit dem Schlüssel im Namen (`…_B2SESSION-S1.xisf`) → Grundlage für
  „Master wiederverwenden“.
- Ausgabe von PixInsight auf stdout enthält nur Chromium-GPU-Meldungen (harmlos) und `:LOGFILE:<pfad>:LOGFILE:`.
- Ein in der Claude-App eingetragener Werkzeug-Server ist eine eigene exe-Kopie: nach einem Update bleibt er alt,
  bis der Eintrag auf die neue Datei zeigt (heute: `b2stacker (12).exe` statt `(13).exe`).

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

## Nachtrag 2026-11-01 (2): Die eigentliche Ursache – Ausrichtung an Hotpixeln (NGC 1999)

- 0.5.1 hat den falsch registrierten Frame weggelassen – der Geist blieb identisch. Der eine Frame war ein Nebenschauplatz.
- Wirkliche Ursache: keine Darks, kein Dithering → ~23 000 Hot-/Warmpixel pro 600-s-Bild, rund die Hälfte an
  identischen Sensor-Stellen. StarAlignment nimmt die 2000 hellsten „Sterne“; nach dem Debayern sind ~300 davon
  Hotpixel an festen Positionen. Ergebnis: fast alle Bilder „passen“ mit 0,00 px / 0,00° auf die Referenz – auch die
  Bilder nach dem Meridian-Flip (in Wahrheit 180° gedreht) und die der zweiten Nacht (in Wahrheit ~20 px versetzt).
  Die 180°-gedrehten Ost-Bilder erzeugen die gedrehte Geisterkopie der Plejaden-Ecke oben links.
- Nachweis: Kreuzkorrelation Rohbild West (0000) gegen Ost (0016): Spitze bei 180° (96 σ) statt 0° (24 σ);
  WBPP meldet für 0016 aber Verschiebung 0,04 px, Drehung 0,00°.
- Lösungen (0.5.2):
  1. `hotpix.py`: ohne Darks werden Hot-/Kaltpixel vor WBPP ersetzt (Bayer-bewusst: Abweichung vom Median der
     gleichfarbigen Nachbarn > 6 σ und alle direkten Nachbarn unauffällig → Sterne bleiben unberührt; Sternfluss
     99,9 %). Plus `hotPixelFilterRadius=2` für StarAlignment (über die WBPP-Automation setzbar).
     Simulation: Top-2000-Detektionen an identischer Sensorposition 301 → 1.
  2. `wbpplog.judge(…, info)`: Bilder aus anderer Nacht oder von anderer Meridianseite mit < 0,5 px und < 1° zur
     Referenz = „am Sensor ausgerichtet“ (lock). Dann einmal neu mit Hotpixel-Entfernung (auch mit Darks) und
     `hotPixelFilterRadius=3`, `noiseReductionFilterRadius=2`; klappt es wieder nicht → Fehler statt Geister-Stack.
- WBPP-Automation: beliebige Engine-Parameter per `name=wert` (Parameters.set → importParameters), u. a.
  hotPixelFilterRadius, noiseReductionFilterRadius, minStructureSize, sensitivity, rigidTransformations, maxStars.
  CosmeticCorrection geht in WBPP nur über ein Prozess-Icon – in der Automation nicht nutzbar.

## Nachtrag 2026-11-02: Hotpixel mit PixInsight selbst – per Kommandozeile

- WBPP kennt zwei Kosmetik-Wege: (a) `IC.cosmeticCorrectionHigh` in ImageCalibration – braucht einen Master-Dark;
  (b) CosmeticCorrection-Schritt nach dem Kalibrieren – nur über ein Prozess-Icon (`ccData.CCTemplate`,
  `ProcessInstance.fromIcon`). In der Automation (`-n`) gibt es keine Icons.
- Lösung ohne Icon: WBPP lädt per Kommandozeile `usePipelineBuilderScript=true` und
  `pipelineBuilderScriptFile=<pfad>` (Engine-Parameter, `importParameters`). Das Script (`pjsr/cc_builder.js`) wird
  von WBPP im Engine-Kontext ausgeführt: liefert für `fromIcon("b2stacker_CC")` eine CosmeticCorrection mit
  Auto Detect (hot/cold 3 σ), setzt `setCcTemplate("b2stacker_CC")` auf alle Light-Gruppen und baut dann die normale
  Light-Pipeline (`buildPipelineForLight`). Damit läuft WBPPs eigener CC-Schritt (Ordner `cosmetized/`, Cache, Log).
- Kontrolle nach dem Lauf (`cc_check`): Log muss „applying b2stacker_CC process icon“, `useAutoDetect = true`,
  `hotAutoCheck = true` und „End cosmetic correction“ enthalten. Sonst einmal neu mit der Python-Bereinigung
  (`hotpix.py`, Notlösung).

## Nachtrag 2026-11-03: Windows-Bericht zu b2stacker 0.8 (gelöst in 0.9)
- **Komma im Ordnernamen** (`Bias, Darks, Flats`): PixInsight trennt `-r="script,arg,…"` an jedem Komma, ein
  Escape gibt es nicht → zerrissene Argumente, WBPP fand die Kalibrierdaten nicht. Lösung: Bereitstellung über
  Verknüpfungen mit sicherem Namen (`staging.py`), Originalordner bleiben unangetastet.
- **Junctions auf Netzlaufwerk**: `mklink /J` auf ein UNC-Ziel → WinError 123. Junctions nur lokal; für Dateien
  auf dem NAS Symlinks (Entwicklermodus) oder eine einmalige Kopie im Cache.
- **Flat-Gruppen** müssen auf die gewählten Lights beschränkt werden, sonst bekommt WBPP Gruppen ohne Lights.
- **Zu wenige Roh-Kalibrierbilder** (1 Bias): WBPP bricht die Integration ab, danach scheitert die
  Flat-Kalibrierung. Mindestens 3 Rohbilder oder ein Master; wird jetzt vor dem Start geprüft.
- **Astro Pixel Processor-Master** (`MB-…`, `MF-…`, `BITPIX -32`, kein `IMAGETYP`) wurden als Lights/unklar
  gelesen; Erkennung an Dateipräfix, Software-Tag und `NCOMBINE`.
- **ASIAIR ohne `OBJECT`**: Ordner wie `25.-27.12.2024` oder `Dual` wurden als Objekte gelesen → mehrere
  Pseudo-Ziele. Objekt jetzt zuerst aus dem Dateinamen (`Light_IC434_180.0s_…`).
- WBPP liest Gruppen-Kennwörter laut Adam-Block-Forum auch aus dem FITS-Header (Pfad hat Vorrang) – mögliche
  spätere Option, um Flat-Gruppen ohne Verknüpfungen zu bilden.

## Nachtrag 2026-11-03 (2): Bericht 2 – NGC 3486, C11 + ASI294MC Pro (0,33"/px)
- **Kritisch:** Nach einem erkannten Hotpixel-Lock schickte die Wiederholung `hotPixelFilterRadius=3` – gültig ist
  0–2. WBPP brach die Registrierung ab („No active frames to be integrated“), b2stacker fand danach die
  `_autocrop`-Datei des verworfenen ersten Versuchs und lieferte sie als „Fertig“ aus. Behoben: eigener Ordner je
  Versuch, Master nur aus diesem Versuch, alle Varianten verwerfen, Parameter vor dem Start begrenzen.
- **Vermutete Ursache des Locks:** WBPP-Voreinstellung `maxStarDistortion = 0,60`. Ein Stern mit Exzentrizität e hat
  das Maß π/4·√(1−e²); bei e = 0,66 sind das ≈ 0,59, bei 0,70 ≈ 0,56 – die meisten echten (leicht ovalen) Sterne
  fallen durch, übrig bleiben runde Hotpixel. Dazu `minStructureSize = 0`. b2stacker setzt diese Werte jetzt aus
  den Messwerten (für NGC 3486: `maxStarDistortion 0,46`, `minStructureSize 7`, Hotpixel-/Rauschfilter 2).
  Noch nicht an echten Daten bestätigt.
- Plausibilitätsgrenze: Stack-FWHM 2,44 px gegenüber 7,7 px in den Einzelbildern → an Hotpixeln ausgerichtet.
- WBPP 3.x meldet vor dem Start „Diagnostics: Cannot create files with a path longer than 256 characters“, sobald
  LongPathsEnabled aus ist – unabhängig von den tatsächlichen Pfaden (hier max. 175 Zeichen).

## Nachtrag 2026-11-04: Härtung mit absichtlich kaputten und realistischen Daten (0.9.3)
- **Ein einziger Frame mit unlesbarem `DATE-OBS`** ließ den ganzen Bericht abstürzen (Nacht = None beim Sortieren).
  Jetzt: Datum aus NINA-/ASIAIR-Dateinamen oder Datumsordner, sonst Frame ohne Nacht – nie Absturz.
- **Satellitenspur** wurde als „unscharf (FWHM 10 px statt 2,8 px)“ aussortiert: Die Spur zerfällt in eine Kette stark
  länglicher Pseudo-Sterne. Unterscheidung zum Nachführfehler: Spur-Detektionen liegen auf einer Geraden,
  nachführbedingt längliche Sterne sind parallel, aber verstreut. Ältere Messungen, die „unscharf“ wirken, werden
  einmalig nachgemessen; alles andere bleibt im Cache (26 MP ≈ 0,9 s Messung je Frame).
- **Identische Dateien in zwei Quellordnern** (Original + Backup) wurden doppelt gestackt. Erkennung über SHA256.
- **Filter „Ha/OIII 7nm“** wurde als reines OIII erkannt (Teilstring), „OIII-SII“ als reines SII.
- **Gleicher Filter, anderes Gain/Binning/Format** ergab zwei Läufe mit demselben Namen – die sich dann sogar dieselben
  Lights teilten (Lauf-Name ist Schlüssel für Bearbeitungen und Frame-Zuordnung).
- `bool("false")` ist in Python `True`: Plan-Optionen, die als Text ankamen, blieben eingeschaltet. Jetzt werden
  alle Eingaben über API und CLI geprüft und normalisiert, ungültige nie gespeichert.
- Testbilder für die Sternmessung nicht zu dicht packen: Nachbarsterne im 15×15-Messfenster blähen die FWHM auf.
