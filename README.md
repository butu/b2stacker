# AstroStack

Rohdaten sichten, bewerten und mit **PixInsight** stacken – automatisch, nachvollziehbar,
per Klick oder per KI.

> Status: **Stufe 1 benutzbar** – Analyse, Bericht, Plan, Oberfläche und Stacking mit WBPP.
> Siehe [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md).

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
zusätzlich Node.js. Die Tests erzeugen eigene FITS-Dateien und simulieren PixInsight-Aufrufe.
Sie prüfen kein echtes WBPP-Stacking und keinen Windows-Programm-Build.

## Ablauf

Die Startseite zeigt alle Projekte als Kacheln mit Vorschau des neuesten Stacks (Suche und Sortierung links).

1. **Neues Projekt** – einen oder mehrere Ordner wählen (NINA, ASIAIR, Ekos, SharpCap, APT …), optional ein Titel.
   Lights, Flats, Bias und Darks (auch fertige Master) werden anhand der FITS-Header gefunden, egal wie die
   Ordner heißen. Schreibweisen desselben Objekts („Sh2-82“, „Sh 2-82“, „SH2_82“) gelten als ein Ziel; werden
   mehrere Ziele gefunden, fragt das Programm nach (zusammenlegen, getrennt, bestehendes Projekt, weglassen) –
   Ziele mit fast gleichen Koordinaten werden als „vermutlich dasselbe Feld“ markiert.
   Ordner mit `_rejected`, `_verworfen`, `_alt` im Namen gelten als aussortiert.
   Ein Planungsprojekt lässt sich auch ohne Lights und Quellordner anlegen. Vorhandene
   PSD-, TIFF- und JPEG-Dateien können verknüpft werden; Dateien direkt im Projekt- oder
   Quellordner und dessen `final/` werden ebenfalls angezeigt. Die Originale bleiben an ihrem Ort.
2. **Bericht** – jeder Frame wird vermessen (FWHM, Exzentrizität, Sterne, Hintergrund, Rauschen,
   Mond, Höhe), dazu Rig-Diagnose (Fokus, Nachführung, Dithering, Verkippung, Taupunkt) und
   Kalibrier-Prüfung (Flat-Helligkeit, Farbabfall, Auslesemodus, fehlende Flats).
   Im Reiter **Frames** stehen auch Darks, Flats, Bias und Dark-Flats. Ein Häkchen bedeutet
   „verwenden“. Filter für auffällige oder fehlerhafte Bilder und Sortierungen nach Nacht,
   Sternanzahl, Sternform, HFR, FWHM und Bewertung helfen beim Sichten. In der Großansicht:
   Pfeiltasten zum Blättern, Leertaste zum An-/Abwählen, Esc zum Schließen.
   Nach Änderungen an Quellordnern oder Frame-Auswahl ist vor dem Stacking eine Neuanalyse nötig.
3. **Stacking-Plan** – Vorschlag mit Begründung, Läufe ein/aus, Referenz, Optionen,
   Speicher- und RAM-Anzeige.
4. **Los – stacken** – PixInsight läuft unsichtbar im Hintergrund, der PC bleibt wach. Der Ablauf wird Schritt
   für Schritt angezeigt – mit Erklärung, was gerade passiert und wozu, und geschätzter Restzeit.
   Vorher wird geprüft: Platz, Arbeitsspeicher, ob PixInsight schon offen ist.
5. **Nachbearbeitung des Stacks** – Farbsäume (R/B-Versatz) werden an den Sternen gemessen und korrigiert,
   Dualband (HaO3, L-eXtreme, SII/OIII …) wird in Ha + OIII bzw. SII + OIII geteilt.
6. **Ergebnisse** – in `<Arbeitsverzeichnis>/projects/<Ziel>/final/` als `Ziel_Filter_Datum_Stunden.xisf`.
   Jeder Stack bekommt Qualitätskennzahlen (Rauschen, Schärfe, Sterne, Sternform, Farbversatz, Verlauf) und
   eine Vorschau. Die **Übersicht** zeigt alle Stacks eines Projekts und ihren Verlauf als Diagramm.
7. **Spickzettel Bearbeitung** – empfohlene Reihenfolge in PixInsight (DBE, SPCC, BXT, NXT …) mit Begründung.

Flats/Bias/Darks lassen sich im Plan selbst wählen (auch eigene Ordner). Wurde die Kamera zwischen Nächten
gedreht, bekommt jede Nachtgruppe automatisch ihre eigenen Flats.

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
