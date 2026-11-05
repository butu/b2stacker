# b2stacker (AstroStack)

Rohdaten sichten, bewerten und mit **PixInsight** stacken – automatisch, nachvollziehbar,
per Klick oder per KI. Originale werden nie verändert.

📖 **Dokumentation**

| | |
|---|---|
| [Anleitung](docs/ANLEITUNG.md) | Bedienung Schritt für Schritt: Projekte, Frames, Stacken, Einstellungen, Selbsttest, NAS |
| [Neubau](docs/NEUBAU.md) | Aufbau der neuen App: Datenbank, Aufträge, Ablauf, Übergabe an PixInsight, Oberfläche |
| [Architektur](docs/ARCHITEKTUR.md) | Module und PixInsight-Anbindung der alten App (bis 0.10) |
| [Erkenntnisse](docs/ERKENNTNISSE.md) | Erfahrungen aus echten Aufnahmen und WBPP-Eigenheiten (datiert) |
| [Änderungen](CHANGELOG.md) | Was sich in welcher Version geändert hat |
| [Agent guide](AGENTS.md) | Kurzreferenz für KI-Agenten und Entwickler |

## Starten (Windows)

Unter [Releases](https://github.com/butu/b2stacker/releases/latest) **`b2stacker.exe`** herunterladen und
doppelklicken (SmartScreen: „Weitere Informationen“ → „Trotzdem ausführen“). Die Startseite bietet an, b2stacker
nach `%LOCALAPPDATA%\b2stacker\` zu **installieren** – dann gibt es eine Startmenü-Verknüpfung und Updates
funktionieren zuverlässig. b2stacker öffnet sich als eigenes Programmfenster (WebView2); ein zweiter Doppelklick holt
das offene Fenster nach vorne.

**Updates:** oben rechts erscheint „Update x.y.z“ → „Jetzt aktualisieren und neu starten“. Ein kleines Fenster zeigt
den Austausch; schlägt er fehl, startet die bisherige Version.

Beim ersten Start fragt b2stacker nach dem **Arbeitsverzeichnis** (große, schnelle Platte – nicht OneDrive) und nach
PixInsight. Die alte Oberfläche bis 0.10 startet noch mit `B2_OLD_UI=1`.

## Entwicklung unter Linux ohne PixInsight

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m astrostack web --no-browser        # neue Oberfläche, http://127.0.0.1:8775/
```

Tests: `python -m pytest -q` (JavaScript-Syntaxprüfung braucht Node.js, Browser-Tests Playwright). Die Tests
erzeugen eigene FITS-Dateien und ein nachgebautes PixInsight als echten Prozess. Auf jedem Branch laufen sie
zusätzlich unter Windows (`.github/workflows/tests-windows.yml`). Echtes WBPP prüft nur der Selbsttest in der App.

## Ablauf in Kürze

1. **Projekt anlegen** – Ordner wählen (NINA, ASIAIR, Ekos, SharpCap …). Art, Filter und Nacht kommen aus dem Header.
2. **Automatisch im Hintergrund** – neue Dateien finden, Header lesen, vermessen, bewerten, Kalibrierung zuordnen,
   Plan mit Begründung je WBPP-Einstellung. Ein Watcher sieht neue Nächte von selbst.
3. **Frames sichten** – Kachelansicht ohne Blättern, Großansicht mit Sternen und Zoom, Tastatur (V/N/A/Entf).
4. **Stacken** – ein Klick; PixInsight + WBPP im Hintergrund, ein Ordner je Stack im Projektordner, Master werden
   projektübergreifend wiederverwendet (Kalibrier-Bibliothek).
5. **Stacks vergleichen** – Qualitätswerte, Notizen, Überblend-Vergleich mit Ausrichtung (auch gedreht).
6. **Galerie und Verlauf** – bearbeitete Bilder zum Projekt, Verlauf in Klartext.

Rohdaten werden nur gelesen; einzige Ausnahme ist das Löschen vorgemerkter Frames nach Bestätigung. Details:
[Anleitung](docs/ANLEITUNG.md), Aufbau: [Neubau](docs/NEUBAU.md).

## Bedienung durch KI (MCP)

**Einstellungen → KI-Bedienung (MCP)** zeigt Adresse (`http://127.0.0.1:8775/mcp`) und Schlüssel, dazu fertige
Einträge für OpenCode, Claude Code und andere Werkzeuge. Für die Claude-Desktop-App leitet `b2stacker.exe mcp` an das
laufende Programm weiter. Details: [Anleitung, Abschnitt 12](docs/ANLEITUNG.md#12-bedienung-durch-claude-und-andere-ki-werkzeuge).

## Kommandozeile (auch für KI-Bedienung)

Alle Befehle antworten mit `--json` maschinenlesbar. Die exe nimmt dieselben Befehle an
(`b2stacker.exe --json plan "vdB 15"`) – Ausgaben gehen an die aufrufende Konsole bzw. Umleitung.

| Befehl | Zweck |
|---|---|
| `b2stacker.exe` (ohne Argumente) | Programmfenster |
| `astrostack web [--no-browser]` / `gui` | neue Oberfläche im Browser bzw. nur Server |
| `astrostack neu projects\|project\|refresh\|frames\|stack\|stacks\|history` | Befehle der neuen App |
| `astrostack scan <ordner…>` | Frames finden und gruppieren |
| `astrostack check-calib <ordner…>` | Flats/Bias/Darks prüfen und zuordnen |
| `astrostack report <ordner…> [--target <ziel>]` | vermessen, Bericht + Plan erzeugen |
| `astrostack plan <projekt> [--set option=wert] [--disable Lauf] [--reference Lauf=master:RGB]` | Plan zeigen/ändern |
| `astrostack override <projekt> in\|out\|auto <datei…>` | Frames manuell aufnehmen/ausschließen |
| `astrostack frames <projekt> [--status problems]` | Frames mit Bewertung, Messwerten und Gründen |
| `astrostack reanalyze <projekt>` | Projekt neu analysieren |
| `astrostack stack <projekt> [--only Lauf]` | mit PixInsight stacken (läuft b2stacker, dort eingereiht) |
| `astrostack selftest [--folder X]` | Selbsttest mit echtem PixInsight |
| `astrostack mcp [--install]` | MCP-Server für Claude bzw. in der Claude-Desktop-App eintragen |
| `astrostack status` | letzte Aufträge und Läufe |
| `astrostack logs [--kind error] [--project X] [--trace]` | Protokoll-Ereignisse |
| `astrostack settings [--set schluessel=wert]` | Einstellungen |

`scan`, `check-calib`, `report`, `plan`, `override`, `frames`, `reanalyze`, `stack`, `selftest` und `status` gehören
noch zur alten App (0.10) und sprechen mit ihr; für die neue App gibt es `astrostack neu …` und MCP.

## Entwicklung

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
pytest
pip install pyinstaller
pyinstaller packaging/astrostack.spec --noconfirm   # → dist/b2stacker.exe
```

Jeder Push auf `main` baut die exe auf GitHub (Windows), testet sie und veröffentlicht sie als Release `v<Version>` (Version in `astrostack/__init__.py`; gleiche Version → Datei wird ersetzt).

Chart.js (MIT) liegt eingebettet unter `astrostack/report/vendor/`, damit der Bericht auch ohne
Internet funktioniert.
