# b2stacker (AstroStack)

Rohdaten sichten, bewerten und mit **PixInsight** stacken – automatisch, nachvollziehbar,
per Klick oder per KI. Originale werden nie verändert.

📖 **Dokumentation**

| | |
|---|---|
| [Anleitung](docs/ANLEITUNG.md) | Bedienung Schritt für Schritt: Projekte, Frames, Stacken, Einstellungen, Selbsttest, NAS |
| [Architektur](docs/ARCHITEKTUR.md) | Module, PixInsight-Anbindung, Dateibereitstellung, Ordnerstruktur, Tests |
| [Erkenntnisse](docs/ERKENNTNISSE.md) | Erfahrungen aus echten Aufnahmen und WBPP-Eigenheiten (datiert) |
| [Agent guide](AGENTS.md) | Kurzreferenz für KI-Agenten und Entwickler |

## Starten (Windows)

**Einfach:** Unter [Releases](https://github.com/butu/b2stacker/releases/latest) die Datei
**`b2stacker.exe`** herunterladen und doppelklicken. Keine Installation, kein Python nötig.
b2stacker öffnet sich als eigenes Programmfenster (WebView2, in Windows 10/11 enthalten) – kein Browser,
keine Konsole. Fenster schließen beendet das Programm (bei laufendem Stacking mit Rückfrage).
Ein zweiter Doppelklick holt das offene Fenster nach vorne.

**Updates:** Gibt es eine neue Version, erscheint oben rechts ein grüner Knopf „Update x.y.z“. Klick → Änderungen
ansehen → „Jetzt aktualisieren & neu starten“. b2stacker lädt die neue exe, schließt sich, tauscht die Datei aus und
startet neu (nicht während eines Stackings). Die exe muss dafür in einem Ordner mit Schreibrechten liegen.

Windows SmartScreen kann beim ersten Start warnen („Unbekannter Herausgeber“):
„Weitere Informationen“ → „Trotzdem ausführen“.

Beim ersten Start: in den Einstellungen das **Arbeitsverzeichnis** wählen
(große, schnelle Platte – nicht OneDrive) und den Pfad zu PixInsight prüfen.

**Aus dem Quellcode:** Python 3.10+ installieren, Repository klonen, `AstroStack starten.cmd` doppelklicken.

## Entwicklung unter Linux ohne PixInsight

Projektverwaltung, Bildanalyse und Browser-Oberfläche funktionieren ohne PixInsight.
Nur das eigentliche Stacking braucht eine PixInsight-Installation.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m astrostack gui --no-browser --port 18765
```

Danach `http://127.0.0.1:18765/` öffnen und in den Einstellungen ein Arbeitsverzeichnis
wählen. Der Linux-Ordnerdialog nutzt **Zenity** oder **Kdialog**, ohne diese Programme
ein vorhandenes Tkinter. Ist keines verfügbar, nennt die Fehlermeldung die benötigten
Dialogprogramme; Ordnerpfade lassen sich zusätzlich direkt eintippen.

Tests laufen mit `python -m pytest -q`; die JavaScript-Syntaxprüfungen benötigen
zusätzlich Node.js. Die Tests erzeugen eigene FITS-Dateien. Ein nachgebautes PixInsight
(`tests/fake_pixinsight.py`) läuft als eigener Prozess mit der echten Kommandozeile und bildet die
WBPP-Eigenheiten nach (Komma-Trennung, Längengrenze, Flat-Gruppen, Mindestanzahl Kalibrierbilder).
Auf jedem Branch laufen die Tests zusätzlich unter Windows, auch über eine echte Netzwerkfreigabe
(`.github/workflows/tests-windows.yml`). Echtes WBPP prüft nur der Selbsttest in der App.

## Ablauf in Kürze

1. **Neues Projekt** – Ordner wählen (NINA, ASIAIR, Ekos, SharpCap, APT …). Lights und Kalibrierdaten werden an
   den FITS-Daten erkannt, egal wie die Ordner heißen; fertige Master (auch Astro Pixel Processor) werden übernommen.
2. **Analyse und Bericht** – jeder Frame wird vermessen (FWHM, Sterne, Rauschen, Mond, Höhe …), dazu Rig-Diagnose
   und Kalibrier-Prüfung.
3. **Frames sichten** – verwenden oder weglassen, mit Filtern, Sortierung und Tastatur (V/N/A).
4. **Stacken** – Plan je Filter-Lauf mit Begründung: Kalibrierung, Referenz, **nur die besten X % Frames** per
   Schieberegler (Stunden werden sofort neu gerechnet), Platz- und RAM-Prüfung. PixInsight läuft unsichtbar im
   Hintergrund; Aufträge laufen in einer Warteschlange.
5. **Ergebnisse** – in `<Arbeitsverzeichnis>/projects/<Ziel>/final/`, mit Qualitätskennzahlen, Vorschau und
   Spickzettel für die Bearbeitung.

Scheitert etwas, nennt b2stacker Schritt und Ursache in Klartext. Mit **Menü → Selbsttest** lässt sich prüfen, ob
alles auf dem eigenen Rechner (inkl. NAS) funktioniert. Details: [Anleitung](docs/ANLEITUNG.md).

## Protokoll (Fehlersuche, auch für KI-Agenten)

Alles Wichtige wird im Hintergrund mitgeschrieben: `<Arbeitsverzeichnis>/logs/` – `events-JJJJ-MM.jsonl`
(ein Ereignis pro Zeile: Analyse, Stacking-Schritte mit Dauer, PixInsight-Exit-Codes, Fehler mit Traceback) und
`b2stacker.log`. Je Stacking-Lauf zusätzlich `projects/<Ziel>/runs/<Zeit>/` mit exaktem PixInsight-Aufruf und den
WBPP-Protokollen. Schnellzugriff: `astrostack logs --kind error --trace`.

Projekte lassen sich umbenennen, beschreiben und jederzeit (auch nach Monaten, mit neuer Programmversion)
neu analysieren und erneut stacken.

Projekte und Stacks lassen sich aus der Verwaltung entfernen; dabei werden **keine Bilddateien
gelöscht oder verschoben**. Stack-Namen sind reine Anzeigenamen und ändern keine Dateinamen.
Die Seitenleiste zeigt die Belichtungsstunden aller Lights, auch der ausgeschlossenen.

Während Scan und Analyse entsteht unter `<Arbeitsverzeichnis>/cache/catalog/` ein gemeinsamer
Katalog für Dateiheader, Light-Messwerte und Flat-Statistiken. Unveränderte Dateien werden auch
in anderen Projekten wiederverwendet. Ein schneller Dateistatus-Vergleich vermeidet erneutes
Hashen; bei neuen oder geänderten Dateien prüft ein SHA256-Hash den Inhalt. Die Bewertung und
die Zuordnung von Kalibrierbildern werden für jedes Projekt neu berechnet.

## Kommandozeile (auch für KI-Bedienung)

Alle Befehle antworten mit `--json` maschinenlesbar. Die exe nimmt dieselben Befehle an
(`b2stacker.exe --json plan "vdB 15"`) – Ausgaben gehen an die aufrufende Konsole bzw. Umleitung.

| Befehl | Zweck |
|---|---|
| `b2stacker.exe` (ohne Argumente) | Programmfenster |
| `astrostack gui [--no-browser]` | Oberfläche im Browser bzw. nur Server |
| `astrostack scan <ordner…>` | Frames finden und gruppieren |
| `astrostack check-calib <ordner…>` | Flats/Bias/Darks prüfen und zuordnen |
| `astrostack report <ordner…> [--target <ziel>]` | vermessen, Bericht + Plan erzeugen |
| `astrostack plan <projekt> [--set option=wert] [--disable Lauf] [--reference Lauf=master:RGB]` | Plan zeigen/ändern |
| `astrostack override <projekt> in\|out\|auto <datei…>` | Frames manuell aufnehmen/ausschließen |
| `astrostack stack <projekt> [--only Lauf]` | mit PixInsight stacken |
| `astrostack status` | letzte Aufträge und Läufe |
| `astrostack logs [--kind error] [--project X] [--trace]` | Protokoll-Ereignisse |
| `astrostack settings [--set schluessel=wert]` | Einstellungen |

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
