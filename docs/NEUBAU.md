# Neubau (Branch `next`)

Verbindlich ist das Konzept „b2stacker – Konzept & Anforderungen“ (Stand 04.11.2026) samt Tab
„Umsetzungsplan“. Diese Datei hält fest, wie der Code dorthin umgebaut wird. Stand: 04.11.2026.

## Entscheidungen

- Neubau im selben Repo auf `next`. Ein Push auf `next` baut kein Release; `main` bleibt bis zum Tor 1.0
  die Version 0.10.x, die im Alltag läuft.
- Keine Datenübernahme aus 0.10 (JSON-Projekte, `catalog/`), keine Rückwärtskompatibilität.
- Rechenmodule mit ihren Tests werden übernommen, so viel wie möglich.
- Cloud-Dateien (OneDrive „nur online“) werden normal gelesen und voll vermessen; das Herunterladen
  erledigt OneDrive selbst.
- Der Branch `feature/siril-engine` wird nicht weiterverfolgt. Gestackt wird mit PixInsight + WBPP.

## Schichten

```
Oberfläche (Fenster/Browser) · CLI · MCP          ← alle drei auf derselben Dienst-Schicht
Dienste: Projekte, Ablauf (Schritte 1–5), Stacks, History
Aufträge: Warteschlange, Fortschritt, Restzeit, Live-Push
Datenbank: astrostack/db (SQLite, einzige Datenquelle)
Rechenmodule (übernommen): fitsmeta, filters, analyze, calibcheck, rotation, starmap, hotpix,
                           wbpplog, stackqa, xisf, imageio, report/
Motor: PixInsight + WBPP (JSON-Auftrag, Startskript, Event-Script)
```

## Datenbank (`astrostack/db`) – fertig

- Datei `<workdir>/system/b2stacker.db`, WAL-Modus, Fremdschlüssel an, eine Verbindung je Thread.
- Schema in `db/schema.py`, Version in `PRAGMA user_version`. Änderungen nur als **neue** Migration am Ende
  von `MIGRATIONS`; ausgelieferte Migrationen werden nie geändert. Jede Migration ist atomar.
- Eine Datenbank einer neueren Version wird nicht geöffnet (`DatabaseTooNew`).
- Sicherung beim Öffnen nach `backup_dir` (letzte 10): immer vor einer Migration, sonst nur bei geändertem
  Inhalt. `db.backup()` für den Menüpunkt, `restore_backup()` zum Zurückspielen.
- Dateien sind global (`files`, kanonischer Pfad), Projekte verweisen über `project_files` darauf, damit ein
  gemeinsamer Flat-Ordner nur einmal gelesen und vermessen wird.

## Arbeitsverzeichnis

```
<workdir>/
  projects/<projektname>/          Ergebnisse; ein Ordner je Stack, masters/ für Master-Kalibrierdateien
  temp/                            ein gemeinsamer Temp-Ordner, vor und nach jedem Stack geleert
  system/                          b2stacker.db, backup/, previews/, logs/
```

## Aufträge (`astrostack/tasks.py`) – fertig

- Spuren `stack`, `analysis`, `misc` laufen parallel, je Spur ein Auftrag; nie zwei PixInsight.
- Gleicher `key` wird nicht doppelt eingereiht (automatische Auslöser), das geöffnete Projekt hat Vorrang.
- Fortschritt mit Schritt, Zähler und geglätteter Restzeit; höchstens ~5 Meldungen je Sekunde.
- `EventHub` hält die letzten Ereignisse für Wiederverbindungen (SSE `Last-Event-ID`); `snapshot()` liefert
  den Gesamtstand. Ersetzt `jobs.py`, sobald der neue Server steht.

## Stand 04.11.2026, Nacht

- `flow/` Schritte 1–5 (Entdecken, Header, Vermessen, Bewerten + Kalibrier-Zuordnung, Plan) – fertig, getestet.
- `handover.py` + `pjsr/b2launch.js` (Weg B) – **mit echtem PixInsight geprüft** (siehe ERKENNTNISSE.md).
- `stacking.py` – Stack-Ordner, Stack-Datensatz, temp/ leeren; mit nachgebautem PixInsight getestet.
- `core.py` – Dienst-Schicht für Oberfläche/CLI/MCP. Server und Oberfläche darauf: offen.
- Getrenntes Debayern funktioniert in WBPP 3.2.0 → Dualband künftig je Kanal registrieren (Plan anpassen).

## Reihenfolge

Siehe Prioliste im Umsetzungsplan: Datenbank → Aufträge mit Live-Rückmeldung → Ordner & Watcher →
Ablauf (Schritte 1–5) → Stacking → Oberfläche → Stacks/History. Jedes Paket ist erst fertig mit
Oberfläche, CLI, MCP und Test.
