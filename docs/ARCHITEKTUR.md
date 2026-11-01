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
| `rigcheck.py` | Sternform im Bildfeld (3×3), Richtungskohärenz, Fokus/Autofokus, Dithering, Taupunkt, Kühlung. |
| `plan.py` | Stacking-Plan: Läufe, Motor, Einstellungen mit Begründung, Speicher-/RAM-Bedarf. |
| `report/` | HTML-Bericht (selbstenthaltend, Chart.js). |
| `engines/` | Motoren. Gemeinsame Schnittstelle: `prepare(plan) → run() → progress events → result`. |
| `xisf.py` | XISF lesen/schreiben (unkomprimiert, Float32). |
| `jobs.py` | Warteschlange, Status, Logs, Wiederaufnahme, Aufräumen. |
| `system.py` | RAM/Disk prüfen, Energiesparmodus verhindern (Windows), hängende PixInsight-Prozesse erkennen. |
| `cli.py` | Kommandozeile (`--json` für KI). |

## Motoren

### PixInsight WBPP (`engines/pixinsight_wbpp.py`)
- Aufruf: `PixInsight.exe -n --automation-mode -r="WBPP.js,automationMode=true,<param>=<wert>,…,dir=…" --force-exit`
- Parameter: siehe WBPP `BPP-engine.js` (`importParameters`). Gespeicherte GUI-Einstellungen werden im Automation-Modus **nicht** geladen.
- Für Breitband und Mono-Filter.
- **Bekannter Fehler (WBPP 2.7.8):** `debayerOutputMethod=separated` ist im Automation-Modus defekt → nicht verwenden.

### PixInsight-Pipeline (`engines/pixinsight_pipeline.py` + `pjsr/pipeline.js`) – geplant
- Für Dualband an Farbkamera in maximaler Qualität: ImageCalibration → Kanal-Extraktion direkt aus CFA (ohne Interpolation) → StarAlignment je Kanal → LocalNormalization → ImageIntegration (PSF Signal Weight).

### Siril – optional, später

## Daten-/Ordnerstruktur (Arbeitsverzeichnis)

```
<workdir>/
  settings.json          Einstellungen
  calib/                 aufbereitete Kalibrierdaten (z. B. Bias mit korrigiertem IMAGETYP)
  projects/<ziel>/
    inventory.json       Ergebnis des Scans
    analysis/            Metriken, Thumbnails, Bericht
    plan.json            Stacking-Plan (bearbeitbar)
    runs/<run-id>/       Arbeitsdaten eines Laufs (werden nach Erfolg aufgeräumt)
    final/               Masters, Ha/OIII, Vorschauen
  jobs/                  Warteschlange, Status, Logs
```

## Robustheit (Lehren aus vdB 131)
- Immer nur **eine** PixInsight-Instanz gleichzeitig; hängende Instanzen erkennen und melden.
- **Freien Arbeitsspeicher** vor dem Start prüfen (Out-of-memory führte zu Abstürzen bei ImageIntegration).
- **Energiesparmodus verhindern**, solange ein Job läuft.
- Nach Absturz: Zwischendateien aufräumen, dann sauber neu starten (WBPP-Cache gilt nur innerhalb einer Instanz).
- **Speicherbedarf** schätzen (≈0,75 GB pro 26-MP-Farbframe) und vor dem Start prüfen.
