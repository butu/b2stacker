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

Windows SmartScreen kann beim ersten Start warnen („Unbekannter Herausgeber“):
„Weitere Informationen“ → „Trotzdem ausführen“.

Beim ersten Start: in den Einstellungen das **Arbeitsverzeichnis** wählen
(große, schnelle Platte – nicht OneDrive) und den Pfad zu PixInsight prüfen.

**Aus dem Quellcode:** Python 3.10+ installieren, Repository klonen, `AstroStack starten.cmd` doppelklicken.

## Ablauf

Die Startseite zeigt alle Projekte als Kacheln mit Vorschau des neuesten Stacks (Suche und Sortierung links).

1. **Neues Projekt** – einen oder mehrere Ordner wählen (NINA, ASIAIR, Ekos, SharpCap, APT …).
   Lights, Flats, Bias und Darks werden anhand der FITS-Header gefunden, egal wie die Ordner heißen.
   Ordner mit `_rejected`, `_verworfen`, `_alt` im Namen gelten als aussortiert.
2. **Bericht** – jeder Frame wird vermessen (FWHM, Exzentrizität, Sterne, Hintergrund, Rauschen,
   Mond, Höhe), dazu Rig-Diagnose (Fokus, Nachführung, Dithering, Verkippung, Taupunkt) und
   Kalibrier-Prüfung (Flat-Helligkeit, Farbabfall, Auslesemodus, fehlende Flats).
   In der Galerie lassen sich Frames per Klick aus- oder einschließen.
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
