# Architektur

## Grundsätze

- **Qualität vor Bequemlichkeit.** Stacking-Motor ist PixInsight (WBPP bzw. eigene Prozess-Pipeline).
- **Keine KI im Normalbetrieb.** Alle Regeln sind deterministisch und in Code/Config abgebildet.
- **KI-bedienbar.** Jede Funktion ist über die CLI (`--json`) erreichbar; später zusätzlich als MCP-Server.
- **Quelle egal.** NINA, ASIAIR, Ekos, SharpCap, APT … werden über einen Header-Übersetzer vereinheitlicht.
- **Filter egal.** Breitband, Dualband an Farbkamera (Ha/OIII, SII/OIII), Mono-Filter (L, R, G, B, Ha, SII, OIII).
- **Nachvollziehbar.** Jeder Vorschlag hat eine Begründung; jeder Lauf ein Log und eine Statusdatei.

## Schichten

```
┌──────────────────────────────────────────────────────────────┐
│ Oberfläche: Browser-UI (localhost)  ·  CLI  ·  (später MCP)   │
├──────────────────────────────────────────────────────────────┤
│ Projekt: Scan → Analyse → Prüfung → Plan → Job → Ergebnis     │
├───────────────┬───────────────┬──────────────┬───────────────┤
│ fitsmeta      │ analyze       │ calibcheck   │ plan          │
│ (Header-      │ (Frame-Metrik,│ (Flats/Bias/ │ (Einstellungen│
│  Übersetzer)  │  Rig-Diagnose)│  Darks)      │  + Begründung)│
├───────────────┴───────────────┴──────────────┴───────────────┤
│ Motoren: PixInsight WBPP · PixInsight-Pipeline (PJSR) · (Siril)│
└──────────────────────────────────────────────────────────────┘
```

## Module

| Modul | Aufgabe |
|---|---|
| `fitsmeta.py` | FITS-Header lesen und in ein einheitliches `FrameMeta` übersetzen (Typ, Filter, Belichtung, Gain, Offset, Auslesemodus, Temperatur, Bayer, Fokus, Rotator, Zeit, Ort, Ziel). Erkennt Quelle (NINA/ASIAIR/…). |
| `filters.py` | Filter-Katalog: Name/Alias → Typ (`broadband`, `dualband_ha_oiii`, `dualband_sii_oiii`, `mono_<x>`) und Verarbeitungsart. |
| `discover.py` | Ordner rekursiv scannen, `FrameMeta` erzeugen, gruppieren: Lights nach Ziel/Filter/Nacht, Kalibriersätze nach Typ/Filter/Datum/Einstellungen. Ignoriert `_rejected`, `_alt` usw. |
| `calibcheck.py` | Flats pro Frame prüfen (Kanal-Helligkeit, Knick ~48k ADU, Farbabfall Mitte→Rand, Streuung), Auslesemodus/Gain/Offset-Abgleich, beste Zuordnung Flat↔Light (Filter, Fokus, Rotator, Datum). |
| `analyze.py` | Frame-Metriken (FWHM, Exzentrizität, Sterne, Hintergrund, Rauschen, Mond …). |
| `plan.py` | Stacking-Plan: Läufe, Motor, Einstellungen mit Begründung, Speicher-/RAM-Bedarf. |
| `report/` | Bewertung (`assess`), Rig-Diagnose (`diag`: Sternform 3×3, Richtungskohärenz, Fokus, Dithering, Taupunkt), Himmelsbahnen (`sky`), HTML-Bericht (selbstenthaltend, Chart.js eingebettet). |
| `project.py` | Ablauf je Ziel, Projektliste, Plan-Anpassungen (`plan_edits.json`), manuelle Frame-Entscheidungen (`overrides.json`). |
| `app/` | Oberfläche: HTTP-Server nur mit Standardbibliothek (127.0.0.1) + eine HTML-Seite. |
| `pjsr/split.js` | Farb-Master in Kanäle aufteilen (z. B. Ha = R, OIII = (G+B)/2). |
| `engines/pixinsight.py` | WBPP-Lauf: Argumente, Dateilisten (`dir=`/`file=`), Kalibrier-Header-Korrektur, Platz/RAM/Instanz-Prüfung, Wachhalten, Fortschritt aus Zwischenordnern, Absturz-Erkennung, Aufteilen, Aufräumen. |
| `xisf.py` | XISF lesen/schreiben (unkomprimiert, Float32). |
| `jobs.py` | Hintergrund-Aufträge (immer nur einer), Status + Log, gespiegelt nach `<workdir>/jobs/`. |
| `system.py` | RAM/Disk prüfen, Energiesparmodus verhindern (Windows), hängende PixInsight-Prozesse erkennen. |
| `cli.py` | Kommandozeile (`--json` für KI). |

## Motoren

### PixInsight WBPP (`engines/pixinsight.py`)
- Aufruf: `PixInsight.exe -n --automation-mode -r="WBPP.js,automationMode=true,<param>=<wert>,…,dir=…,file=…" --force-exit`
- Parameter: siehe WBPP `BPP-engine.js` (`importParameters`). Gespeicherte GUI-Einstellungen werden im Automation-Modus **nicht** geladen.
- `dir=` sucht **rekursiv** → wird nur genutzt, wenn genau alle Bilder im Ordner gewählt sind, sonst `file=` je Datei.
  Werte dürfen kein Komma und kein `=` enthalten. Bei sehr langen Befehlszeilen werden Lights in einen Arbeitsordner verlinkt.
- WBPP erkennt den Frame-Typ nur am Header: Kalibrierframes mit falschem `IMAGETYP` (NINA-Snapshot-Bias) werden als
  korrigierte Kopie in `cache/calib_fixed/<typ>_<hash>/` übergeben.
- Drizzle ist im Automationsmodus nicht global setzbar (pro Gruppe) → bei Bedarf von Hand in WBPP.
- **Bekannter Fehler (WBPP 2.7.8):** `debayerOutputMethod=separated` ist im Automation-Modus defekt (registriert die CFA-Frames).
  Daher Dualband an Farbkamera: als Farbbild stacken (`debayerOutputMethod=0`), danach `pjsr/split.js`
  (Ha/SII = R, OIII = (G+B)/2).
- Referenz: erster Breitband-Lauf wählt den besten Frame, weitere Läufe richten auf dessen Master aus → alle Masters deckungsgleich.

### PixInsight-Pipeline – später
- Für Dualband an Farbkamera ohne Interpolation: ImageCalibration → Kanal-Extraktion direkt aus CFA → StarAlignment je Kanal →
  LocalNormalization → ImageIntegration (PSF Signal Weight). Erst sinnvoll, wenn nachgewiesen besser als WBPP + Aufteilen.

### Siril – optional, später

## Daten-/Ordnerstruktur (Arbeitsverzeichnis)

```
<workdir>/
  cache/calib_fixed/     Kalibrierframes mit korrigiertem IMAGETYP
  projects/<ziel>/
    inventory.json       Ergebnis des Scans
    calibration.json     Kalibrier-Prüfung und Zuordnung
    sources.json         gewählte Quellordner (für „Neu analysieren“)
    analysis/            Metriken (metrics.csv), Thumbnails
    report.html          Bericht
    plan.json            Stacking-Plan
    plan_edits.json      Anpassungen des Benutzers (überleben eine Neu-Analyse)
    overrides.json       manuell aus-/eingeschlossene Frames
    runs/<zeit>/         je Lauf: plan.json, status.json, astrostack.log, <Lauf>/master, <Lauf>/logs
    final/               Ergebnisse; ältere Versionen in final/_alt/
  jobs/                  Status aller Aufträge (für CLI/KI)
```
Einstellungen: `%APPDATA%\AstroStack\settings.json` (bzw. `~/.config/astrostack/`).

## Robustheit (Lehren aus vdB 131)
- Immer nur **eine** PixInsight-Instanz gleichzeitig; hängende Instanzen erkennen und melden.
- **Freien Arbeitsspeicher** vor dem Start prüfen (Out-of-memory führte zu Abstürzen bei ImageIntegration).
- **Energiesparmodus verhindern**, solange ein Job läuft.
- Nach Absturz: Zwischendateien aufräumen, dann sauber neu starten (WBPP-Cache gilt nur innerhalb einer Instanz).
- **Speicherbedarf** schätzen (≈0,75 GB pro 26-MP-Farbframe) und vor dem Start prüfen.
