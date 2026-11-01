# AstroStack

Rohdaten sichten, bewerten und mit **PixInsight** stacken – automatisch, nachvollziehbar,
per Klick oder per KI.

> Status: **Stufe 1 in Arbeit.** Siehe [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md).

## Was es tun soll

1. **Ordner wählen** – einen oder mehrere (NINA, ASIAIR, Ekos, SharpCap, APT …).
   Lights, Flats, Bias und Darks werden anhand der FITS-Header gefunden, egal wie die Ordner heißen.
2. **Prüfen** – jeder Frame wird vermessen (FWHM/HFR, Exzentrizität, Sterne, Hintergrund,
   Rauschen, Mond, Höhe), Rig-Diagnose (Fokus, Nachführung, Dithering, Tilt, Taupunkt …),
   Kalibrier-Prüfung (Flat-Helligkeit, Farbabfall, Auslesemodus, fehlende Flats).
3. **Plan** – Vorschlag für die Stacking-Einstellungen mit Begründung, Speicher- und RAM-Ampel.
   Ausschlüsse und Einstellungen lassen sich anpassen.
4. **Start** – PixInsight stackt; Fortschritt wird live angezeigt.
5. **Ergebnis** – Masters (bei Dualband auch Ha/OIII bzw. SII/OIII getrennt), Vorschau,
   Qualitätskontrolle (Farbring, Deckungsgleichheit), Öffnen in PixInsight.

Im Normalbetrieb braucht das Programm **keine KI**. Über die Kommandozeile (und später MCP)
ist es aber vollständig per KI bedienbar.

## Schnellstart (Entwicklung)

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
astrostack scan "F:\OneDrive\NINA\vdB 131" "F:\OneDrive\NINA\FLAT"
```

## Kommandos (Stufe 1)

| Befehl | Zweck |
|---|---|
| `astrostack scan <ordner…>` | Frames finden, Header vereinheitlichen, gruppieren, Übersicht ausgeben |
| `astrostack check-calib <ordner…>` | Flats/Bias/Darks prüfen und Lights zuordnen |
| `astrostack analyze <ordner…> --target <name>` | Frames vermessen, Rig-Diagnose |
| `astrostack report --target <name>` | HTML-Bericht inkl. Stacking-Plan |
| `astrostack run <plan.json>` | Stacken mit PixInsight |
| `astrostack status` | Laufende/fertige Jobs |

Alle Befehle können mit `--json` maschinenlesbar antworten (für die KI-Bedienung).
