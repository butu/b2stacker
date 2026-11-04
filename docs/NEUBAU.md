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

## Neuer Server (`astrostack/web`) – fertig, Oberfläche offen

- Start: `python -m astrostack web [--no-browser] [--port N]` (Port ab 8775, läuft neben der alten App auf
  8765) oder `gui` mit `B2_NEW_UI=1`. Ohne die Variable startet `gui` wie bisher die alte App.
- Nur Standardbibliothek, `ThreadingHTTPServer` auf 127.0.0.1, eine `Core` je Server; beim Start
  `core.refresh_all()`. Die Datenbank-Verbindung eines Anfrage-Threads wird danach geschlossen (`db.release()`).
- Sicherheit wie in der alten App: nur `Host` 127.0.0.1/localhost/[::1], jeder POST mit `X-Astrostack: 1`,
  kein GET liefert den MCP-Schlüssel. Fehler: `{"error": "…"}` – 400 Eingabe (`ValueError`), 404 unbekannte
  Route, unbekanntes Projekt/Stack/Auftrag/Bild, 500 intern (mit Protokolleintrag).
- `static/index.html` ist die neue Oberfläche (Teil 1, siehe unten).

| Methode | Pfad | Core |
|---|---|---|
| GET | `/api/bootstrap` | Version, Einstellungen ohne Schlüssel, `pixinsight_ok`, `workdir`, `first_run`, `install`, `tasks` (Snapshot mit `last_event`), `projects` |
| GET/POST | `/api/projects` | `projects()` / `create_project {name, folders[], description}` |
| GET | `/api/projects/<id>` | `project` (id oder Name) |
| POST | `/api/projects/<id>/open` · `rename {name}` · `describe {text}` · `folders {folders[]}` · `folders/<fid>/remove` · `refresh` | |
| GET | `/api/projects/<id>/frames?type=&status=&offset=&limit=` | `frames`, je Frame zusätzlich `thumb_url` (mit `?v=`) |
| POST | `/api/projects/<id>/decide {file_ids[], decision, reason}` | `decide` |
| GET/POST | `/api/projects/<id>/plan` | `plan` / `edit_plan {options, runs, reset}` |
| POST | `/api/projects/<id>/stack {run_ids?}` | `stack` |
| GET | `/api/projects/<id>/stacks` | `stacks` |
| POST | `/api/projects/<id>/stacks/<sid>/note {text}` · `…/delete` | `note` / `delete_stack` |
| GET | `/api/history?project=&limit=` · `/api/tasks` | `history` / `tasks.snapshot()` |
| POST | `/api/tasks/<id>/cancel` | `tasks.cancel` |
| GET | `/api/thumb/<file_id>[?v=]` | JPEG aus dem `.npy` (`system/previews/thumbs-jpg`), ETag; mit passendem `v` dauerhaft cachebar |
| GET | `/api/frame/<file_id>/preview` | große Vorschau aus dem Original (`system/previews/frames`), ETag |
| POST | `/api/settings` · `/api/backup` · `/api/clear-temp` · `/api/quit` | Prüfung wie alte App; PixInsight/Analysen gelten sofort. Neues Arbeitsverzeichnis: gilt sofort, solange das alte leer ist (Ersteinrichtung), sonst `restart_needed` |
| POST | `/api/pick-folder {initial}` · `/api/pick-file {initial, kind}` · `/api/check-pixinsight {path}` | Dialoge und Prüfung der alten App (im Fenster nativ); kein Dialog → 400 mit Klartext |
| POST | `/api/mcp-info` · `/api/mcp-token` · `/api/focus` | MCP-Adresse, Schlüssel, Schnipsel (nur per POST) · neuer Schlüssel · Fenster nach vorne |
| GET | `/api/events` | Server-Sent Events |
| POST | `/mcp` | MCP über HTTP (Schlüssel + Origin) |

**SSE** (`/api/events`): Ereignisse aus `core.tasks.hub` mit `id:` und `event:` (`task`, `projects`, `project`, …).
Ablauf für den Client: `/api/bootstrap` laden, dann `/api/events?last=<tasks.last_event>` öffnen; der Browser
schickt beim Wiederverbinden selbst `Last-Event-ID`. Ohne Nummer geht es ab jetzt los. Ist die Lücke nicht mehr
im Puffer (500 Ereignisse) oder stammt die Nummer aus einem früheren Programmlauf, kommt `event: reset` →
Bootstrap neu laden. Heartbeat (`: ping`) alle 15 s; ein getrennter Client oder das Server-Ende beendet den
Strom innerhalb ~1 s.

**Offen:** Die MCP-Werkzeuge (`mcp.Tools`) sprechen noch die API der alten App (`appclient`); `/mcp` ist hier
nur gleich eingehängt, Werkzeugaufrufe scheitern daher am neuen Server. Werkzeuge auf `Core` umstellen ist ein
eigenes Paket.

## Neue Oberfläche, Teil 1 – fertig (05.11.2026)

Start: `python -m astrostack web` (Browser) oder mit `B2_NEW_UI=1` der normale Start – Doppelklick auf die exe öffnet
dann die neue Oberfläche im Programmfenster (`web/window.py`, pywebview wie die alte App). Ohne die Variable bleibt
alles wie bisher.

- Eine Datei `web/static/index.html`, ohne Bibliotheken und ohne Build. Gestaltung, Symbole und Bauteile wie in
  der alten App. Hash-Routen: `#/` Startseite, `#/p/<id>/<tab>` Projekt (`overview`, `frames`, `plan`, `stacks`,
  `history`).
- Rückmeldung: `/api/bootstrap` zeigt sofort den Stand aus der Datenbank, `/api/events` hält ihn aktuell
  (Wiederverbindung mit `Last-Event-ID`, bei `reset` Bootstrap neu). Kopfzeile: Auftragsanzeige mit Popover
  (laufend/wartend mit Abbrechen, zuletzt beendet). Im Projekt und auf den Kacheln kleine Live-Hinweise
  („3 neue Dateien werden eingelesen · noch etwa 2 min“), keine blockierenden Überlagerungen.
- Ersteinrichtung (keine Einstellungsdatei oder PixInsight fehlt, noch keine Projekte): PixInsight finden und
  prüfen, Arbeitsverzeichnis wählen, erstes Projekt anlegen. Einstellungen mit Wartung (Sichern, Temp leeren)
  und MCP-Schlüssel/Schnipseln.
- Startseite: Kacheln mit Platzhalter-Bild, großer Gesamtbelichtung, Balken je Filter, „+N Frames seit dem
  letzten Stack“, zuletzt geöffnet/gestackt, „Quelle offline“, „arbeitet“. Sortieren, Suchen, nach Monat
  gruppieren (ein Projekt in jedem Monat mit Aufnahmen). Neues Projekt: Name, Beschreibung, Ordner.
- Projekt: Name/Beschreibung direkt bearbeiten, Quellordner (hinzufügen, entfernen, offline), Übersicht mit
  Bereitschaft und „Was auffällt“.
- Frames: alle Frames in einem virtualisierten Raster (nur sichtbare Kacheln im DOM, Laden in Blöcken zu 5000),
  Filter nach Art und Status, Mehrfachauswahl (Kästchen, Shift, Strg) und Sammel-Entscheidung, Großansicht mit
  Messwerten, Tasten ←/→, V/N (entscheiden und weiter), A, Esc; am Ende „letztes Bild“, keine Schleife.
- Plan: Karte je Lauf (an/aus mit Grund, Flats je Nacht mit Urteil, Bias/Darks, Probleme), Einstellungen mit
  Grund, „angepasst“ und „zurück auf Automatik“, Platz/RAM, großer Knopf „Stacken“ mit Grund, warum es noch
  nicht geht, und Live-Fortschritt.
- Tests: `tests/test_web_ui.py` (Playwright, übersprungen ohne Playwright), Hilfen dort (`App`, `fake_project`)
  auch für Bildschirmfotos nutzbar.

**Haken für Teil 2:** `TABS.stacks` / `TABS.history` (jetzt einfache Platzhalter mit den vorhandenen Daten),
`coverUrl(p)` nimmt `cover_url`/`image_url` aus `/api/projects`, sobald der Server sie liefert (Projektbild,
letzter Stack). Watcher-Ereignisse kommen automatisch als `task`/`project` an. Sternmarkierungen in der
Großansicht brauchen Sternpositionen vom Server (gibt es noch nicht).

## Stand 04.11.2026, Nacht

- `flow/` Schritte 1–5 (Entdecken, Header, Vermessen, Bewerten + Kalibrier-Zuordnung, Plan) – fertig, getestet.
- `handover.py` + `pjsr/b2launch.js` (Weg B) – **mit echtem PixInsight geprüft** (siehe ERKENNTNISSE.md).
- `stacking.py` – Stack-Ordner, Stack-Datensatz, temp/ leeren; mit nachgebautem PixInsight getestet.
- `core.py` – Dienst-Schicht für Oberfläche/CLI/MCP. Server darauf: fertig (`web/`), Oberfläche: offen.
- Getrenntes Debayern funktioniert in WBPP 3.2.0 → Dualband künftig je Kanal registrieren (Plan anpassen).

## Reihenfolge

Siehe Prioliste im Umsetzungsplan: Datenbank → Aufträge mit Live-Rückmeldung → Ordner & Watcher →
Ablauf (Schritte 1–5) → Stacking → Oberfläche → Stacks/History. Jedes Paket ist erst fertig mit
Oberfläche, CLI, MCP und Test.
