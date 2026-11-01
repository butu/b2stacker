# AstroStack

Rohdaten sichten, bewerten und mit **PixInsight** stacken – automatisch, nachvollziehbar,
per Klick oder per KI.

> Status: **Stufe 1 benutzbar** – Analyse, Bericht, Plan, Oberfläche und Stacking mit WBPP.
> Siehe [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md).

## Starten (Windows)

Voraussetzungen: Python 3.10+ und PixInsight.

1. Repository herunterladen (oder `git clone https://github.com/butu/b2stacker.git`).
2. **`AstroStack starten.cmd`** doppelklicken. Beim ersten Mal wird das Programm installiert,
   danach öffnet sich die Oberfläche im Browser.
3. In den Einstellungen das **Arbeitsverzeichnis** wählen (große, schnelle Platte – nicht OneDrive)
   und den Pfad zu PixInsight prüfen.

Das Fenster mit der schwarzen Konsole muss offen bleiben, solange man das Programm benutzt.

## Ablauf

1. **Neue Analyse** – einen oder mehrere Ordner wählen (NINA, ASIAIR, Ekos, SharpCap, APT …).
   Lights, Flats, Bias und Darks werden anhand der FITS-Header gefunden, egal wie die Ordner heißen.
   Ordner mit `_rejected`, `_verworfen`, `_alt` im Namen gelten als aussortiert.
2. **Bericht** – jeder Frame wird vermessen (FWHM, Exzentrizität, Sterne, Hintergrund, Rauschen,
   Mond, Höhe), dazu Rig-Diagnose (Fokus, Nachführung, Dithering, Verkippung, Taupunkt) und
   Kalibrier-Prüfung (Flat-Helligkeit, Farbabfall, Auslesemodus, fehlende Flats).
   In der Galerie lassen sich Frames per Klick aus- oder einschließen.
3. **Stacking-Plan** – Vorschlag mit Begründung, Läufe ein/aus, Referenz, Optionen,
   Speicher- und RAM-Anzeige.
4. **Los – stacken** – PixInsight läuft unsichtbar im Hintergrund, Fortschritt live, PC bleibt wach.
   Vorher wird geprüft: Platz, Arbeitsspeicher, ob PixInsight schon offen ist.
5. **Ergebnisse** – in `<Arbeitsverzeichnis>/projects/<Ziel>/final/`. Dualband an Farbkamera
   (HaO3, L-eXtreme, SII/OIII …) wird automatisch in Ha + OIII bzw. SII + OIII aufgeteilt.
   Ältere Ergebnisse werden nach `final/_alt/` verschoben, nie überschrieben.

## Kommandozeile (auch für KI-Bedienung)

Alle Befehle antworten mit `--json` maschinenlesbar.

| Befehl | Zweck |
|---|---|
| `astrostack` / `astrostack gui` | Oberfläche öffnen |
| `astrostack scan <ordner…>` | Frames finden und gruppieren |
| `astrostack check-calib <ordner…>` | Flats/Bias/Darks prüfen und zuordnen |
| `astrostack report <ordner…> [--target <ziel>]` | vermessen, Bericht + Plan erzeugen |
| `astrostack plan <projekt> [--set option=wert] [--disable Lauf] [--reference Lauf=master:RGB]` | Plan zeigen/ändern |
| `astrostack override <projekt> in\|out\|auto <datei…>` | Frames manuell aufnehmen/ausschließen |
| `astrostack stack <projekt> [--only Lauf]` | mit PixInsight stacken |
| `astrostack status` | letzte Aufträge und Läufe |
| `astrostack settings [--set schluessel=wert]` | Einstellungen |

## Entwicklung

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
pytest
```

Chart.js (MIT) liegt eingebettet unter `astrostack/report/vendor/`, damit der Bericht auch ohne
Internet funktioniert.
