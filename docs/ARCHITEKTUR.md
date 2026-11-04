# Architektur

## Grundsätze

- **Qualität vor Bequemlichkeit.** Stacking-Motor ist PixInsight (WBPP bzw. eigene Prozess-Pipeline).
- **Keine KI im Normalbetrieb.** Alle Regeln sind deterministisch und in Code/Config abgebildet.
- **KI-bedienbar.** Jede Funktion ist über die CLI (`--json`) und als MCP-Server (`b2stacker.exe mcp`) erreichbar.
- **Quelle egal.** NINA, ASIAIR, Ekos, SharpCap, APT … werden über einen Header-Übersetzer vereinheitlicht.
- **Filter egal.** Breitband, Dualband an Farbkamera (Ha/OIII, SII/OIII), Mono-Filter (L, R, G, B, Ha, SII, OIII).
- **Nachvollziehbar.** Jeder Vorschlag hat eine Begründung; jeder Lauf ein Log und eine Statusdatei.

## Schichten

```
┌──────────────────────────────────────────────────────────────┐
│ Oberfläche: Programmfenster/Browser · CLI · MCP (KI)          │
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
| `fitsmeta.py` | FITS-Header lesen und in ein einheitliches `FrameMeta` übersetzen (Typ, Filter, Belichtung, Gain, Offset, Auslesemodus, Temperatur, Bayer, Fokus, Rotator, Zeit, Ort, Ziel). Erkennt Quelle (NINA/ASIAIR/…) und Master (auch Astro Pixel Processor `MB-`/`MD-`/`MF-`/`MDF-`). Fehlt der Standort im Header, gilt der Ersatz-Standort aus den Einstellungen (`site_lat`/`site_lon`/`site_elev`, `FrameMeta.site_fallback`). |
| `filters.py` | Filter-Katalog: Name/Alias → Typ (`broadband`, `dualband_ha_oiii`, `dualband_sii_oiii`, `mono_<x>`) und Verarbeitungsart. |
| `discover.py` | Ordner rekursiv scannen, `FrameMeta` erzeugen, gruppieren: Lights nach Ziel/Filter/Nacht, Kalibriersätze nach Typ/Filter/Datum/Einstellungen. Ignoriert `_rejected`, `_alt` usw. Objekt ohne `OBJECT`: erst Dateiname (`Light_<Objekt>_…`), dann Ordner – Datums-, Filter-, Typ- und Sammelordner zählen nicht. |
| `calibcheck.py` | Flats pro Frame prüfen (Kanal-Helligkeit, Knick ~48k ADU, Farbabfall Mitte→Rand, Streuung), Auslesemodus/Gain/Offset-Abgleich, beste Zuordnung Flat↔Light (Filter, Fokus, Rotator, Datum). |
| `analyze.py` | Frame-Metriken (FWHM, Exzentrizität, Sterne, Hintergrund, Rauschen, Mond …). |
| `plan.py` | Stacking-Plan: Läufe, Motor, Einstellungen mit Begründung, Speicher-/RAM-Bedarf. |
| `rotation.py` | Kamera-Drehung je Nacht (Rotator oder Sternfeld) → Flat-Gruppen. |
| `selection.py` | Verbindliche Frame-Auswahl je Projekt (Automatik + manuelle Entscheidungen). |
| `catalog.py` | Gemeinsamer Cache für Header, Messwerte, Flat-Statistik (`cache/catalog/`). |
| `previews.py`, `assets.py` | Vorschauen von Rohbildern; verknüpfte bearbeitete Bilder (Originale bleiben, wo sie sind). |
| `staging.py` | **Einzige Stelle**, an der Dateien für PixInsight bereitgestellt werden (siehe unten). |
| `wbpplog.py` | WBPP-Protokoll auswerten: Registrierung je Frame, Fehlerursachen in Klartext (`diagnose`), Alarm vs. Hinweis (`classify`), Unterschritte des Kalibrierens (`CalibTracker`). |
| `starmap.py` | Erkannte Sterne/Hotpixel eines Frames fürs Overlay im Frame-Browser (`/api/frame-stars`), gecacht in `analysis/stars/`. |
| `stackqa.py` | Fertige Stacks: Farbsäume, Kanäle aufteilen, Qualität messen, Vorschau. |
| `hotpix.py` | Hotpixel-Entfernung, wenn keine Darks da sind. |
| `selftest.py` | Selbsttest auf dem echten Rechner: synthetischer Datensatz (Komma im Ordner, 2 Kamerastellungen) → Analyse → echtes WBPP → `selbsttest/bericht-*.json`. |
| `logbook.py`, `steps.py` | Hintergrund-Protokoll (`logs/events-*.jsonl`) und Ablaufschritte mit Erklärung. |
| `updater.py` | Selbst-Aktualisierung der exe über GitHub-Releases. |
| `report/` | Bewertung (`assess`), Rig-Diagnose (`diag`: Sternform 3×3, Richtungskohärenz, Fokus, Dithering, Taupunkt), Himmelsbahnen (`sky`), HTML-Bericht (selbstenthaltend, Chart.js eingebettet). |
| `project.py` | Ablauf je Ziel, Projektliste, Plan-Anpassungen (`plan_edits.json`), manuelle Frame-Entscheidungen (`overrides.json`). |
| `app/` | Oberfläche: HTTP-Server nur mit Standardbibliothek (127.0.0.1) + eine HTML-Seite. |
| `pjsr/split.js` | Farb-Master in Kanäle aufteilen (z. B. Ha = R, OIII = (G+B)/2). |
| `engines/pixinsight.py` | WBPP-Lauf: Argumente, Dateilisten (`dir=`/`file=`), Kalibrier-Header-Korrektur, Platz/RAM/Instanz-Prüfung, Wachhalten, Fortschritt aus Zwischenordnern, Absturz-Erkennung, Aufteilen, Aufräumen. |
| `xisf.py` | XISF lesen/schreiben (unkomprimiert, Float32). |
| `jobs.py` | Hintergrund-Aufträge in einer Warteschlange (einer läuft, weitere warten, jeder abbrechbar), eindeutiger Endzustand (Fertig/Fehlgeschlagen/Abgebrochen), gespiegelt nach `<workdir>/jobs/`. |
| `system.py` | RAM/Disk prüfen, Energiesparmodus verhindern (Windows), hängende PixInsight-Prozesse erkennen. |
| `cli.py` | Kommandozeile (`--json` für KI). |
| `appclient.py` | Verbindung zur laufenden App (sucht Ports 8765–8784, startet sie bei Bedarf losgelöst). CLI (`stack`, `reanalyze`, `selftest`) und MCP reihen Aufträge darüber ein – eine Warteschlange, nie zwei PixInsight. Die CLI nutzt die App nur bei gleichem Arbeitsverzeichnis. |
| `piscript.py` | Eigenes PixInsight-Skript aus dem Arbeitsverzeichnis im Automatik-Modus ausführen (Auftrag `pi_script`, MCP `run_pixinsight_script`): Ausgabe zeilenweise ins Protokoll und nach `logs/pixinsight-scripts/`, Abbruch, Zeitlimit. `.js` braucht `#engine v8` als erste Zeile. |
| `mcp.py` | MCP-Server (JSON-RPC 2.0 über stdin/stdout, ohne Zusatzpakete): 18 Werkzeuge mit kompakten Antworten; jeder Aufruf wird beantwortet (auch bei internen Fehlern) und als `mcp_call` protokolliert. Aufträge per KI tragen „· per Claude“ (Header `X-B2-Client: mcp`). |
| `claude_link.py` | Eintrag `mcpServers.b2stacker` in `claude_desktop_config.json` (normale und Store-Installation): nur dieser Schlüssel, mit Sicherung, unlesbare Dateien bleiben unangetastet. |

## Motoren

### PixInsight WBPP (`engines/pixinsight.py`)
- Aufruf: `PixInsight.exe -n --automation-mode -r="WBPP.js,automationMode=true,<param>=<wert>,…,dir=…,file=…" --force-exit`
- Parameter: siehe WBPP `BPP-engine.js` (`importParameters`). Gespeicherte GUI-Einstellungen werden im Automation-Modus **nicht** geladen.
- `dir=` sucht **rekursiv** → wird nur genutzt, wenn genau alle Bilder im Ordner gewählt sind, sonst `file=` je Datei.
  Werte dürfen kein Komma und kein `=` enthalten (PixInsight trennt `-r` am Komma, ohne Ausweg). Siehe „Dateibereitstellung“.
- WBPP erkennt den Frame-Typ nur am Header: Kalibrierframes mit falschem `IMAGETYP` (NINA-Snapshot-Bias) werden als
  korrigierte Kopie in `cache/calib_fixed/<typ>_<hash>/` übergeben.
- Drizzle ist im Automationsmodus nicht global setzbar (pro Gruppe) → bei Bedarf von Hand in WBPP.
- **Bekannter Fehler (WBPP 2.7.8):** `debayerOutputMethod=separated` ist im Automation-Modus defekt (registriert die CFA-Frames).
  Daher Dualband an Farbkamera: als Farbbild stacken (`debayerOutputMethod=0`), danach `pjsr/split.js`
  (Ha/SII = R, OIII = (G+B)/2).
- Referenz: erster Breitband-Lauf wählt den besten Frame, weitere Läufe richten auf dessen Master aus → alle Masters deckungsgleich.

- Vor dem Start (`_preflight_calibration`): weniger als 3 Roh-Bias/-Darks/-Flats ohne Master → Abbruch mit Klartext,
  PixInsight startet gar nicht erst. Nach dem Lauf ohne Master: `wbpplog.diagnose` liefert Schritt + Ursache.
- Ist die Hotpixel-Korrektur in PixInsight einmal gescheitert, merkt sich das `cache/pixinsight-cosmetic-broken.json`
  und nutzt beim nächsten Mal den eigenen Weg (`hotpix.py`).
- „Nur die besten X %“ je Lauf (`project.apply_top_percent`): kürzt nur den Plan dieses Auftrags, nicht die Auswahl.
  Die Oberfläche rechnet die Stunden mit `rank_exp` (Belichtung je Light in Güte-Reihenfolge) live nach.

- **Versuche und Wiederholung:** Jeder Versuch eines Laufs bekommt einen leeren Ausgabeordner; ein früherer Versuch
  wandert nach `<Lauf>.versuch-<n>/` (Protokolle bleiben). Der Master wird nur akzeptiert, wenn er nach dem Start
  **dieses** Versuchs entstanden ist (`find_master(since=…)`); verworfene Ergebnisse werden in allen Varianten
  (`_autocrop`, Drizzle, Kanäle) umbenannt. Lock-Prüfung (`_check_registration`) und FWHM-Plausibilität
  (`fwhm_implausible`: Stack-FWHM < 50 % der Einzelbilder) führen zu genau einer Wiederholung, danach zu `failed`.
- **Parametergrenzen:** `check_wbpp_params` begrenzt alle von b2stacker gesetzten StarAlignment-Werte auf ihren
  gültigen Bereich (`WBPP_RANGES`, z. B. `hotPixelFilterRadius` 0–2), bevor PixInsight startet.
- **Sternerkennung:** `plan.star_detection` leitet aus `plan.light_stats` (FWHM, Exzentrizität der gewählten Lights)
  StarAlignment-Parameter ab; sie stehen je Lauf in `plan.json` (`star_detection`) und gehen vor eigenen
  `wbpp`-Überschreibungen in die Kommandozeile.
- **Lange Pfade:** `estimate_max_path` schätzt den längsten Zwischenpfad; ohne LongPaths und über 256 Zeichen stoppt
  der Lauf vor dem Start. WBPPs Vorab-Diagnose ist nur ein Hinweis.

### Dateibereitstellung (`staging.py`)
PixInsight bekommt Originalpfade direkt, solange es geht. Umgelegt wird nur, wenn
1. ein Pfad ein Komma oder `"` enthält,
2. die Befehlszeile zu lang wird (`MAX_CMDLINE`, Windows ≈ 32 767 Zeichen),
3. Flat-Gruppen nötig sind: WBPP ordnet Lights und Flats über `ASFLAT_<n>` **im Pfad** zu
   (`_grp/ASFLAT_<n>/L` und `/F` im Lauf-Ordner). Gruppen werden auf die gewählten Lights beschränkt,
   Gruppen mit gleichen Flats zusammengelegt.

Reihenfolge je Datei: Symlink (Windows: Entwicklermodus nötig) → Hardlink (nur gleiches Laufwerk) →
einmalige Kopie in `cache/staged/<hash>/` (Schlüssel: Pfad + Größe + Änderungszeit), danach wiederverwendet.
Ordner-Junctions nur für lokale Laufwerke (auf UNC/Netzlaufwerk liefern sie WinError 123).
Am Ende prüft `prepare()`, dass kein unsicherer Pfad mehr übrig ist (`StagingError`). Originale werden nie
verändert; `remove_link_tree()` entfernt nur Verknüpfungen, nie Inhalte.

### PixInsight-Pipeline – später
- Für Dualband an Farbkamera ohne Interpolation: ImageCalibration → Kanal-Extraktion direkt aus CFA → StarAlignment je Kanal →
  LocalNormalization → ImageIntegration (PSF Signal Weight). Erst sinnvoll, wenn nachgewiesen besser als WBPP + Aufteilen.

### Siril – optional, später

## Daten-/Ordnerstruktur (Arbeitsverzeichnis)

```
<workdir>/
  cache/calib_fixed/     Kalibrierframes mit korrigiertem IMAGETYP
  cache/staged/          einmalige Kopien von Netzlaufwerk-Dateien (nur ohne Symlink-Recht)
  cache/catalog/         gemeinsamer Header-/Messwert-Katalog
  logs/                  events-JJJJ-MM.jsonl, b2stacker.log
  selbsttest/            Testprojekt und bericht-<Zeit>.json des Selbsttests
  projects/<ziel>/
    inventory.json       Ergebnis des Scans
    calibration.json     Kalibrier-Prüfung und Zuordnung
    sources.json         gewählte Quellordner (für „Neu analysieren“)
    analysis/            Metriken (metrics.csv), Thumbnails
    report.html          Bericht
    plan.json            Stacking-Plan
    plan_edits.json      Anpassungen des Benutzers (überleben eine Neu-Analyse)
    overrides.json       manuell aus-/eingeschlossene Frames
    runs/<zeit>/         je Lauf: plan.json, status.json, astrostack.log, _grp/ (Verknüpfungen), <Lauf>/master, <Lauf>/logs
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

## Tests und Rückkopplung
- `pytest` erzeugt eigene FITS-Daten. `tests/fake_pixinsight.py` ist ein nachgebautes PixInsight/WBPP, das als
  **eigener Prozess** mit der echten Kommandozeile läuft: Komma-Trennung, Längengrenze (WinError 206),
  Bildtyp nur aus dem Header, `ASFLAT`-Gruppen, Integrationsabbruch bei 1–2 Roh-Kalibrierbildern, lange Pfade.
  Es schreibt `fakepi-input.json`, damit Tests prüfen können, was angekommen ist.
- `tests/test_stack_scenarios.py` deckt die Fälle aus echten Fehlerberichten ab (Komma-Ordner, Flat-Gruppen,
  Netzlaufwerk ohne Symlink-Recht, APP-Master, ASIAIR-Ordner ohne `OBJECT` …), lokal und über UNC.
- `.github/workflows/tests-windows.yml` läuft auf jedem Branch unter Windows, inklusive `\\localhost\C$` als
  echte Netzwerkfreigabe. `build-exe.yml` baut und veröffentlicht auf `main`.
- Echtes WBPP prüft nur der **Selbsttest** in der App; sein Bericht liegt in `<workdir>/selbsttest/`.
- `tests/test_mcp.py`: MCP-Protokoll, echter Unterprozess über Pipes, offizieller MCP-Client (falls installiert) und
  der komplette Ablauf anlegen → analysieren → Frames → Plan → Stacken (nachgebautes PixInsight). Tests sprechen nie
  mit einer echten laufenden App (`B2_APP_URL` in `conftest.py`). `packaging/mcp_smoke.py` prüft im Release-Bau die
  fertige exe genauso, wie die Claude-App sie startet.
