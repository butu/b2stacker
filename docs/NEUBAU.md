# Neubau (Branch `next`)

Verbindlich ist das Konzept „b2stacker – Konzept & Anforderungen“ (Stand 04.11.2026) samt Tab
„Umsetzungsplan“. Diese Datei hält fest, wie der Code dorthin umgebaut wird. Stand: 05.11.2026.

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
- Migration 2 (Teil 2): `assets.hidden`, `assets.mtime_ns`, Index `assets_stack`.
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
| GET | `/api/history?project=&limit=[&raw=1]` · `/api/tasks` | `timeline` (Klartext, zusammengefasst; `raw=1`: rohe Ereignisse) / `tasks.snapshot()` |
| POST | `/api/tasks/<id>/cancel` | `tasks.cancel` |
| GET | `/api/thumb/<file_id>[?v=]` | JPEG aus dem `.npy` (`system/previews/thumbs-jpg`), ETag; mit passendem `v` dauerhaft cachebar |
| GET | `/api/frame/<file_id>/preview` | große Vorschau aus dem Original (`system/previews/frames`), ETag |
| GET | `/api/projects/<id>/gallery` · `/api/projects/<id>/compare?a=&b=` | Galerie · Vergleich zweier Stacks (Teil 2) |
| GET | `/api/stacks/<sid>/preview` · `/api/assets/<aid>/thumb` · `…/view` · `/api/projects/<id>/compare/<a>/<b>/a\|b` | Bilder (JPEG, ETag, mit `?v=` dauerhaft cachebar) |
| POST | `/api/projects/<id>/gallery {paths[]}` · `…/gallery/<aid>/remove` · `…/cover {asset_id\|null}` | Galerie, Projektbild |
| GET/POST | `/api/backups` · `/api/backups/restore {name}` · `/api/pick-files {initial}` | Sicherungen, Wiederherstellen, mehrere Bilder wählen |
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

Ab Teil 2 hängt an `/mcp` das Werkzeug-Set auf `Core` (`web/mcp_tools.py`, siehe unten); die alte App behält
`astrostack/mcp.py`.

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

**Haken für Teil 2 (erledigt, siehe unten):** `TABS.stacks` / `TABS.history` (jetzt einfache Platzhalter mit den vorhandenen Daten),
`coverUrl(p)` nimmt `cover_url`/`image_url` aus `/api/projects`, sobald der Server sie liefert (Projektbild,
letzter Stack). Watcher-Ereignisse kommen automatisch als `task`/`project` an. Sternmarkierungen in der
Großansicht brauchen Sternpositionen vom Server (gibt es noch nicht).

## Neue Oberfläche, Teil 2 – fertig (05.11.2026)

Hash-Routen neu: `#/p/<id>/stacks`, `#/p/<id>/gallery`, `#/p/<id>/history`, `#/verlauf` (allgemeiner Verlauf, Knopf
„Verlauf“ in der Kopfzeile). Keine Platzhalter mehr.

**Stacks** (`stackview.py`, Tab „Stacks“)
- Alle Stacks bleiben, neueste zuerst: Vorschau, Lauf, Datum, Lights, Stunden, Nächte, Größe, Status (fehlgeschlagen
  mit Grund), „Datei fehlt“, Notiz (direkt bearbeiten), „Als Projektbild“, Löschen mit Rückfrage. Löschen entfernt nur
  Dateien mit `assets.owned = 1` (und `protokoll/`); fremde Dateien im Stack-Ordner und der Ordner bleiben. Ist eine
  Datei unter Windows geöffnet, bleibt der Stack und es kommt ein Klartext-Fehler.
- Qualität: nach jedem erfolgreichen Lauf misst `Core.stack` das Light-Master (`stackqa.kpis`: FWHM in ″ wenn
  Brennweite/Pixelgröße bekannt, sonst px, Rauschen, Sterne, Exzentrizität, Verlauf) → `stacks.quality_json` mit
  `version`. Dazu `system/previews/stacks/stack-<id>-<hash>.npy` (lineare Kopie, ≤ 1200 px, float16) und `.jpg`
  (Vorschau). Ältere Stacks ohne (aktuelle) Werte misst `Core.stacks()` einmal im Hintergrund nach (Spur `misc`,
  `key=quality:<pid>`). Unlesbares Master → `error` in `quality_json`, keine Endlosschleife.
- Startseite: `cover_url` = gewähltes Projektbild (Galerie-Bild oder Stack), sonst die Vorschau des neuesten Stacks.
- `Core.rename` zieht gespeicherte Pfade im Projektordner mit (`assets`, `masters`) – vorher zeigten sie nach dem
  Umbenennen ins Leere.

**Vergleich** (Überlagerung, „Neuesten vergleichen“ oder zwei Stacks ankreuzen)
- Zwei Vorschauen übereinander mit Schieber (Maus/Touch, ←/→, Shift für feine Schritte), A/B tauschen, Werte
  nebeneinander mit „besser/schlechter“ (weniger ist besser bei FWHM, Rauschen, Exzentrizität, Verlauf; mehr bei
  Sternen; unter 2 % gilt als gleich), Notizen beider Stacks direkt bearbeitbar.
- Ausrichten: Phasenkorrelation (numpy-FFT) auf Sternbild (Hintergrund abgezogen, Hanning-Fenster), Subpixel per
  Parabel; B wird verschoben und beide werden auf den gemeinsamen Bereich zugeschnitten. **Drehung wird nicht
  ausgeglichen** (bei gedrehten Sessions liegen die Sterne versetzt) – dafür braucht es später Stern-Matching
  (Dreiecke) wie in `rotation.py`/`starmap.py`.
- Gleicher Strich: B wird linear an A angeglichen (Median und 99,5-%-Perzentil je Kanal, das Rauschverhältnis bleibt),
  dann ein gemeinsamer STF-Strich aus A. Ergebnis in `system/previews/compare/` (Name aus beiden Vorschau-Versionen).

**Galerie** (`gallery.py`, Tab „Galerie“)
- Mehrere Dateien (`/api/pick-files`: im Fenster nativ, sonst tkinter-Hilfsprozess `_pick-files`) oder ganze Ordner
  (rekursiv, höchstens 2000 Bilder je Hinzufügen) oder Pfad einfügen. Dateien bleiben, wo sie sind (`owned = 0`).
- Formate: JPEG, PNG, TIFF (auch 16 bit), PSD (Pillow liest das zusammengesetzte Bild; kaputtes PSD → Symbol
  „keine Vorschau“), XISF und FITS über die eigenen Leser mit automatischem Strich. Vorschau 640 px, Großansicht
  1800 px, Cache `system/previews/assets/<id>-<version>.jpg` (Version aus Pfad, Größe, Zeitstempel).
- In einem Stack-Ordner → gehört zum Stack, sonst zum Projekt. „Aus der Galerie entfernen“ setzt `assets.hidden`
  (Migration 2); die Datei bleibt und der Watcher nimmt sie nicht wieder auf. Fehlende Dateien: „Datei fehlt“.

**Watcher** (`watcher.py`, im Server-Thread, Einstellung `watch_interval_s`, Standard 45 s)
- Nur Standardbibliothek: Fingerabdruck je Ordnerbaum aus Ordner-Zeitstempel und Eintragszahl (keine Bilddatei wird
  geöffnet oder einzeln abgefragt). Quellordner geändert → `core.refresh` (zusammengelegt), Projektordner geändert →
  `gallery.scan_project_dir` (nicht während gestackt wird; `masters/`, `protokoll/` ausgenommen).
- Entprellt: erst wenn sich ein Ordner zwei Durchgänge lang nicht mehr ändert (nicht mitten im Kopieren, nicht bei
  jedem Bild, das NINA schreibt). Erster Durchgang merkt sich nur den Stand (`refresh_all` beim Start prüft ohnehin).
- Offline-Quellen: jede Prüfung mit 2 s Frist in einem kleinen Thread-Pool; wer hängt, wird 4 Durchgänge
  übersprungen. Wieder erreichbar → Prüfung. `WebServer.stop()` beendet den Thread sauber.
- Stacking legt seine Master per Upsert als `owned = 1` an – falls der Watcher sie vorher als fremdes Bild sah.

**Verlauf** (`history.py`, Tab „Verlauf“ und `#/verlauf`)
- Aus `events`, neueste zuerst, in Klartext mit Symbol und Ton, gruppiert nach Heute/Gestern/Woche/Monat.
  Zusammengefasst je Projekt: neue Dateien (12 h Lücke → eine Nacht), Frame-Entscheidungen, Planänderungen, die
  Läufe eines Stack-Klicks, Galerie-Zugänge.
- `files_discovered` trägt jetzt `groups` (Art, Filter, Nacht, Anzahl, Stunden): `flow.discover` liefert `new_ids`
  und die Ereignis-ID, `flow.headers.summarize_new` ergänzt nach dem Header-Lesen. Text z. B. „20 neue Lights · Ha ·
  2,0 h“, Zusatz „Nacht vom 02.11.2026 (vor 2 Nächten) · dazu 4 Flats“. `stack_done` hat Lauf, Lights, Stunden;
  `stack_deleted` den Namen. Neu: `assets_added`, `cover_set`, `backup_restored`.

**Datenbank-Sicherung** (Einstellungen → Wartung)
- Zusätzlicher Sicherungsort `backup_dir`: jede Sicherung bleibt in `system/backup` und wird dorthin kopiert (je
  Ort die letzten 10); nicht erreichbar → nur Protokolleintrag. Liste mit Datum, Größe, Ort, Anlass.
- Wiederherstellen sofort, ohne Neustart (`core.restore`): während gestackt wird abgelehnt; sonst Aufträge abbrechen,
  Watcher anhalten, andere Anfragen bekommen kurz 503, gewählte Sicherung an einen sicheren Ort kopieren, jetzigen
  Stand als `vor-wiederherstellen` sichern, alle Verbindungen schließen, mit der SQLite-Backup-API zurückspielen,
  neue `Core` öffnen. Beschädigte Sicherung → der bisherige Stand wird wieder geöffnet, Klartext-Fehler.
  (Ein Neustart mit Marker-Datei wäre die Alternative gewesen; der Wechsel im laufenden Server nutzt denselben Weg
  wie das neue Arbeitsverzeichnis und ist getestet.)

**MCP auf Core** (`web/mcp_tools.py`, `/mcp` des neuen Servers)
- Werkzeuge: `status`, `list_projects`, `project`, `frames`, `stacks`, `compare_stacks`, `history`/`logs`, `job`
  (mit `wait_s`), `create_project`, `add_folders`, `analyze`, `set_frames` (IDs, Pfade oder eindeutige Namen;
  use/skip/auto), `change_plan`, `stack`, `cancel_job`, `stack_note`, `run_pixinsight_script` (Auftrag in der Spur
  `stack`, `piscript.check` erzwingt das Arbeitsverzeichnis). `selftest`, `scan_folders`, `settings`,
  `open_in_pixinsight` gibt es nur in der alten App.
- Gleiche Regeln: kein Werkzeug löscht oder verändert Bilddateien (es gibt kein Lösch-Werkzeug), Aufträge über MCP
  heißen „… · per Claude“.

**Kommandozeile** `python -m astrostack neu projects|project|refresh|frames|stack|stacks|history` (mit `--json` vor
`neu`). Spricht mit dem laufenden neuen Server (Ports 8775–8779, gleiches Arbeitsverzeichnis; `B2_WEB_URL` setzt die
Adresse fest), sonst mit `Core` direkt und wartet dann auf das Ende des Auftrags. Alte Befehle unverändert.

**Tests:** `tests/test_web_part2.py` (Core/Server/MCP/CLI/Watcher mit kurzem Intervall, echte XISF-Masters über
einen Stack-Runner-Wrapper), `tests/test_web_ui.py` (Playwright: Stacks, Vergleich, Galerie, Verlauf, Sicherung;
Hilfe `fake_stack` legt Stacks mit echten Mastern direkt an).

### Was fehlt, bevor die neue Oberfläche Standard werden kann

- Sternmarkierungen in der Großansicht (Sternpositionen vom Server) und Zoom/1:1 im Vergleich.
- Ausrichten mit Drehung (Stern-Dreiecke) im Vergleich; Vergleich von Mono gegen Farbe nur als Luminanz.
- Handy-Oberfläche (nur bis ~900 px Breite geprüft; unter 640 px funktioniert es, ist aber nicht gestaltet).
- Frames löschen bzw. „zum Löschen vormerken“ (Status `delete` ist im Schema, aber ohne Oberfläche und Ablauf).
- Kalibrier-Bibliothek über Projekte hinweg (Master-Darks/Bias wiederverwenden, Darks mit Temperatur/Gain verwalten).
- Selbsttest mit echtem PixInsight, „In PixInsight öffnen“, Einstellungen per MCP: nur in der alten App.
- Update-Hinweis und Installation (alte App) für den neuen Start; `B2_NEW_UI=1` wird dann zum Standard.
- Anleitung (`docs/ANLEITUNG.md`) beschreibt noch die alte App.
- Die Galerie zeigt keine Unterordner-Struktur (bewusst: keine Ordneransicht); sehr große Bildmengen (> 2000 je
  Hinzufügen) werden gekappt.

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
