# b2stacker – Anleitung

Diese Anleitung erklärt die Bedienung von b2stacker Schritt für Schritt. Technische Details stehen in
[ARCHITEKTUR.md](ARCHITEKTUR.md), Erfahrungen aus echten Aufnahmen in [ERKENNTNISSE.md](ERKENNTNISSE.md).

**Inhalt**

1. [Erster Start](#1-erster-start)
2. [Projekt anlegen](#2-projekt-anlegen)
3. [Übersicht und Bericht](#3-übersicht-und-bericht)
4. [Frames sichten](#4-frames-sichten)
5. [Stacken: Läufe, Kalibrierung, Optionen](#5-stacken-läufe-kalibrierung-optionen)
6. [Aufträge und Warteschlange](#6-aufträge-und-warteschlange)
7. [Wenn etwas schiefgeht](#7-wenn-etwas-schiefgeht)
8. [Einstellungen](#8-einstellungen)
9. [Selbsttest](#9-selbsttest)
10. [Netzlaufwerk (NAS) und Windows-Entwicklermodus](#10-netzlaufwerk-nas-und-windows-entwicklermodus)
11. [Was b2stacker mit deinen Daten macht – und was nicht](#11-was-b2stacker-mit-deinen-daten-macht--und-was-nicht)

---

## 1. Erster Start

1. Unter [Releases](https://github.com/butu/b2stacker/releases/latest) **`b2stacker.exe`** herunterladen und
   doppelklicken. Keine Installation nötig. SmartScreen-Warnung: „Weitere Informationen“ → „Trotzdem ausführen“.
2. Beim ersten Start öffnen sich die **Einstellungen**:
   - **Arbeitsverzeichnis** – hier landen Zwischendateien, Masters, Stacks und Protokolle. Eine große, schnelle
     lokale Platte wählen, nicht OneDrive.
   - **PixInsight-Programm** – meist `C:\Program Files\PixInsight\bin\PixInsight.exe`. Ein grünes Häkchen zeigt,
     dass PixInsight und das WBPP-Skript gefunden wurden.
    - **Stacking mit** – PixInsight (Standard) oder Siril. Mit Siril brauchst du PixInsight nicht; siehe
      [Stacking mit Siril](#stacking-mit-siril).
3. Neue Versionen meldet oben rechts ein grüner Knopf „Update x.y.z“.

## 2. Projekt anlegen

**Neues Projekt** → einen oder mehrere Ordner wählen. Ein Titel ist optional; ohne Titel heißt das Projekt nach
dem erkannten Objekt (oder „Neues Projekt“).

- Lights, Flats, Bias, Darks und Dark-Flats werden **an den FITS-Daten** erkannt, nicht am Ordnernamen.
  Fertige Master (PixInsight, Astro Pixel Processor `MB-`/`MD-`/`MF-`…) werden als Master übernommen.
- Das Objekt kommt aus `OBJECT` im Header. Fehlt es, liest b2stacker den Dateinamen (z. B. ASIAIR
  `Light_IC434_180.0s_…`) und dann die Ordnernamen. Datumsordner (`25.-27.12.2024`), Filterordner (`Dual`, `RGB`)
  und Typordner (`Lights`) gelten dabei **nicht** als Objekt.
- Schreibweisen desselben Objekts („Sh2-82“, „Sh 2-82“) gelten als ein Ziel. Findet b2stacker trotzdem mehrere
  Ziele, fragt es nach: zusammenlegen, getrennt, zu einem bestehenden Projekt oder weglassen. Eine Zusammenlegung
  bleibt bei jeder späteren Neu-Analyse erhalten.
- Ordner mit `_rejected`, `_verworfen` oder `_alt` im Namen gelten als aussortiert.
- Quellordner lassen sich später ergänzen (z. B. ein reiner Bias-Ordner). Danach **Neu analysieren**.

## 3. Übersicht und Bericht

Die **Übersicht** zeigt Nächte, Belichtungszeit je Filter, Hinweise zu Fokus, Nachführung, Dithering, Flats
und Taupunkt, alle bisherigen Stacks mit Qualitätskennzahlen sowie verknüpfte bearbeitete Bilder
(„Bearbeitete Bilder hinzufügen“). Der **ausführliche Bericht** (Diagramme und Messwerte aller Lights) öffnet sich als Vollbild-Blatt aus der Übersicht.

## 4. Frames sichten

Im Reiter **Frames** steht jedes Bild als Kachel; ein Häkchen heißt „verwenden“. Filter (auffällig, verworfen …)
und Sortierungen (Nacht, Sterne, HFR, FWHM, Bewertung) helfen beim Aussortieren.

Großansicht: Pfeiltasten blättern, **V** = verwenden, **N** = nicht verwenden (beide springen weiter),
**A** = zurück auf Automatik, Leertaste schaltet um, **P** = als Projektbild verwenden, Esc schließt.

**Sterne einblenden** (rechts unter „Einblenden“, nur bei Lights):

- **S – Sterne:** grüne Ellipsen an den Sternen, die b2stacker erkennt (Größe = FWHM, Form und Richtung = Exzentrizität).
- **X – Würde PixInsight verwerfen:** dieselben Sterne in Orange, wenn sie die StarAlignment-Grenze
  `maxStarDistortion` nicht schaffen (Näherung). Sind fast alle Sterne orange, richtet PixInsight sich womöglich an
  Hotpixeln aus.
- **H – Hotpixel:** rote Punkte.

Darunter steht eine Zeile wie „137 Sterne · FWHM 7,7 px · 112 würden verworfen · 4 600 Hotpixel“. Die Erkennung
läuft beim ersten Einblenden (ein paar Sekunden) und wird danach gemerkt. Deine Entscheidungen überleben jede Neu-Analyse.

## 5. Stacken: Läufe, Kalibrierung, Optionen

Der Reiter **Stacken** zeigt den Plan. b2stacker stackt jeden Filter in einem eigenen **Lauf**.

- **Lauf ein/aus** – Schalter rechts. **Umbenennen** – Stift neben dem Namen (z. B. „IR“ → „RGB“);
  der Name erscheint auch im Dateinamen des Stacks.
- **Kalibrierung** – zeigt Flats, Bias, Darks, Dark-Flats und lässt sie ändern (auch eigener Ordner oder
  „keine“). Wurde die Kamera zwischen den Nächten gedreht, bekommt jede Stellung eigene Flats.
- **Optionen** (je Lauf):
  - **Nur die besten Frames** – Schieberegler von 100 % bis 5 %. Nach links lässt du schwächere Lights weg.
    Gereiht wird nach Signal/Rauschen und Sternschärfe aus der Analyse. Während du schiebst, zeigt b2stacker
    sofort, wie viele Lights und **wie viele Stunden** dann integriert werden (mindestens 3 Lights bleiben).
    Deine Auswahl im Reiter „Frames“ ändert sich dadurch nicht.
  - **Referenz** – Frame, auf den ausgerichtet wird. „Master von …“ hält mehrere Filter deckungsgleich.
  - **Sternerkennung angepasst** – erscheint, wenn die Sterne groß oder leicht oval sind (z. B. lange Brennweite).
    b2stacker leitet dann aus der Analyse passende Werte für PixInsights StarAlignment ab (`maxStarDistortion`,
    `minStructureSize`, Hotpixel- und Rauschfilter), damit an echten Sternen statt an Hotpixeln ausgerichtet wird.
     Mit Siril wirkt derselbe Befund auf die Sternerkennung von Siril.
  - Platzbedarf und Light-Ordner des Laufs.
- **Empfohlene Einstellungen** gelten für alle Läufe (Hotpixel, Local Normalization, Gewichtung …).
  „Warum diese Einstellungen?“ erklärt jede Wahl.
- Unten zeigt ein Kasten, ob alles bereit ist (Platz, Arbeitsspeicher, PixInsight bzw. Siril) – dann **Stacken**.

Ein Klick auf ein Ergebnis öffnet den **Ergebnis-Betrachter**: blättern mit den Pfeiltasten, rechts Aufnahme,
Kalibrierung, Qualitätskennzahlen und Hinweise des Stacks, dazu „In PixInsight öffnen“ (bzw. „In Siril öffnen“), „Ordner öffnen“,
„Als Projektbild“ (**P**), „Kennzahlen messen“ und „Umbenennen“. Bearbeitete Bilder sind in derselben Reihe.

Fertige Stacks liegen in `<Arbeitsverzeichnis>/projects/<Projekt>/final/` als
`Ziel_Filter_Datum_Stunden.xisf`. Dualband an der Farbkamera wird danach in Ha + OIII bzw. SII + OIII geteilt
(mit PixInsight; im Siril-Modus bleibt das Ergebnis vorerst ein Farbbild – siehe [Stacking mit Siril](#stacking-mit-siril)).

## 6. Aufträge und Warteschlange

Analyse, Vorschauen und Stacking sind **Aufträge**. Es läuft immer einer; weitere warten in der Warteschlange
und lassen sich dort abbrechen. Das Symbol oben rechts zeigt den laufenden Auftrag mit Projekt, Schritt und
Restzeit. Während ein Auftrag läuft, kannst du weiter Frames sichten und andere Projekte bearbeiten.

Beim Kalibrieren zeigt die Karte mit PixInsight die Unterschritte aus dem Protokoll, z. B. „✓ Master-Bias erstellt ·
✓ Flats kalibriert (60/60) · ▸ Master-Flat erstellen · Lights kalibrieren (0/28)“. Gibt es noch keine
Schätzgrundlage, steht bei der Restzeit „wird geschätzt …“.

Hinweise aus PixInsights Vorab-Prüfung (etwa „lange Pfade“ in Windows aus) erscheinen grau als **Hinweis**, nicht als
Fehler. b2stacker schätzt selbst, wie lang die Pfade dieses Laufs werden, und stoppt nur dann vor dem Start, wenn sie
wirklich zu lang wären.

Jeder Auftrag endet eindeutig mit **Fertig**, **Fehlgeschlagen** oder **Abgebrochen**. Ein Ergebnis, das b2stacker
selbst als unbrauchbar erkennt (z. B. an Hotpixeln statt an Sternen ausgerichtet), wird **nie** als „Fertig“
ausgeliefert: b2stacker rechnet einmal mit Hotpixel-Entfernung neu, und scheitert auch das, endet der Lauf als
fehlgeschlagen mit Erklärung.

## 7. Wenn etwas schiefgeht

Scheitert ein Stacking, zeigt b2stacker einen roten Kasten mit:

- **dem Schritt**, an dem es gescheitert ist (z. B. „Bias integrieren“),
- **der Ursache** aus dem Engine-Protokoll in Klartext (z. B. „nur 1 Bias – mindestens 3 nötig“),
- Knöpfen **Kalibrierdaten ändern**, **WBPP-Protokoll öffnen** (mit Siril: **Protokoll öffnen**) und **Erneut versuchen**.

Offensichtliche Probleme fängt b2stacker schon **vor** dem Start von PixInsight ab, etwa zu wenige
Roh-Kalibrierbilder ohne Master.

Alles wird mitgeschrieben in `<Arbeitsverzeichnis>/logs/` (`events-JJJJ-MM.jsonl`, `b2stacker.log`) und je
Lauf in `projects/<Projekt>/runs/<Zeit>/` (exakter PixInsight-Aufruf, WBPP-Protokolle). Für Fehlermeldungen
an Entwickler oder KI-Agenten: `b2stacker.exe logs --kind error --trace`.

## 8. Einstellungen

Menü (☰) → **Einstellungen**:

| Einstellung | Bedeutung |
|---|---|
| Arbeitsverzeichnis | Zwischendateien, Berichte, Stacks, Historie. Beim Wechsel bietet b2stacker an, Projekte mitzunehmen. |
| Stacking mit | PixInsight (WBPP) oder Siril – siehe [Stacking mit Siril](#stacking-mit-siril). |
| PixInsight-Programm | Pfad zu `PixInsight.exe`. |
| Siril-Programm | Leer = automatisch (PATH, Standardordner, Flatpak `org.siril.Siril`). Sonst Pfad zur `siril-cli` bzw. `flatpak:org.siril.Siril`. Mindestversion 1.4.0. |
| Gleichzeitige Vermessungen | Mehr = schnellere Analyse, je ~1 GB Arbeitsspeicher. |
| Mindestens freier Arbeitsspeicher | Darunter startet kein Stacking (schützt vor Abstürzen). |
| Zeitzone | Für Nächte und Himmelsdiagramme. |
| **Ersatz-Standort** | Breite, Länge (Grad, Osten positiv) und optional Höhe in m. Wird verwendet, wenn die FITS-Daten **keinen Standort** enthalten – für Mondstand, Objekthöhe und die Zuordnung zur Nacht. Steht ein Standort im Header, gilt immer dieser. Wirkt ab der nächsten Analyse. |
| PC wach halten | Kein Energiesparmodus während eines Stackings. |
| Zwischendateien löschen | Spart viel Platz; Masters, Stacks und Protokolle bleiben. |
| Ergebnisse öffnen | Fertige Stacks am Ende in der gewählten Engine (PixInsight oder Siril) öffnen. |

### Stacking mit Siril

**Schalter:** Einstellungen → **Stacking mit** → *Siril*. Danach stackt b2stacker ausschließlich mit Siril:
Stacken, Selbsttest und „In Siril öffnen“ nutzen Siril, PixInsight wird nicht gestartet. Es gibt keinen stillen
Rückfall auf PixInsight; fehlt Siril, bricht der Lauf mit einer klaren Meldung ab.

**Erkennung** (Siril ab Version 1.4.0; geprüft mit 1.4.4). Das Feld „Siril-Programm“ bleibt leer für die
automatische Suche in dieser Reihenfolge:

- `siril-cli` im PATH,
- Standardordner: Linux `/usr/bin/siril-cli` bzw. `/usr/local/bin/siril-cli`, Windows
  `C:\Program Files\Siril\bin\siril-cli.exe`, macOS `/Applications/Siril.app/Contents/MacOS/siril-cli`,
- Flatpak `org.siril.Siril` (nur Linux, wenn installiert).

Alternativ einen Pfad eintragen: die `siril-cli` selbst, das Siril-Programm (die CLI muss daneben liegen)
oder den Installationsordner. Für Flatpak `flatpak:org.siril.Siril` eintragen. Die Einstellungen zeigen, was
gefunden wurde (z. B. „Siril 1.4.4 gefunden“). Bei Flatpak gibt b2stacker Siril beim Start die nötigen Ordner frei;
Originale werden nie verändert.

**Gleich wie bei PixInsight:** Analyse, Flat-Prüfung, Stacking-Plan, Kalibrierung (Bias, Flats, Darks,
Flat-Gruppen bei gedrehter Kamera), Sternschärfe-Bewertung, Referenz, Kennzahlen des fertigen Stacks,
Ablage unter `projects/<Projekt>/final/`. Erkennt b2stacker einen unbrauchbaren Stack (Ausrichtung an
Hotpixeln), wird der Lauf wie bei PixInsight einmal mit stärkerer Hotpixel-Korrektur wiederholt.

**Anders als bei PixInsight:**

- **Hotpixel:** Mit Darks übernimmt Siril die Korrektur. Ohne Darks entfernt b2stacker die Hotpixel vor dem
  Stacken selbst; die CosmeticCorrection von PixInsight entfällt.
- **Dualband an der Farbkamera:** Siril stackt das Farbbild; die Aufteilung in Ha/OIII bzw. SII/OIII findet im
  Siril-Modus derzeit nicht statt.
- **Protokoll:** Jeder Lauf schreibt sein Siril-Skript (`siril.ssf`) in `runs/<Zeit>/<Lauf>/`. Es gibt kein
  WBPP-Protokoll und keine PixInsight-Fensterprüfung.
- **Öffnen:** „In Siril öffnen“ startet Siril mit den fertigen Stacks.

## 9. Selbsttest

Menü → **Selbsttest …** prüft, ob b2stacker auf **deinem** Rechner mit deiner Engine (PixInsight oder Siril) funktioniert.
Du wählst einen Ordner – am besten auf dem Laufwerk, auf dem auch deine Aufnahmen liegen (z. B. das NAS).
b2stacker legt dort einen kleinen Testdatensatz an (`b2stacker-selbsttest, mit Komma`: zwei Nächte, gedrehte
Kamera, Flats je Nacht, Bias), analysiert und stackt ihn mit der eingestellten Engine und prüft dabei:

- PixInsight und WBPP gefunden, Version (mit Siril: Siril gefunden, Version),
- Datei-Verknüpfungen erlaubt (Entwicklermodus), lange Pfade,
- Netzlaufwerk, Komma im Ordnernamen, Flat-Gruppen, Hotpixel-Korrektur.

Das Testprojekt liegt getrennt unter `<Arbeitsverzeichnis>/selbsttest/` und erscheint nicht in der Projektliste.
Dort liegt auch der Bericht `bericht-<Zeit>.json` – den kannst du bei Problemen mitschicken. Er nennt die
Engine (`engine`) und mit Siril die Version (`environment.siril_version`).

## 10. Netzlaufwerk (NAS) und Windows-Entwicklermodus

PixInsight bekommt deine Bilder in der Regel **direkt mit ihren Originalpfaden**. In drei Fällen legt
b2stacker im Arbeitsverzeichnis stattdessen Verknüpfungen an:

1. ein **Komma** im Pfad (z. B. `Bias, Darks, Flats`) – PixInsight trennt seine Argumente am Komma,
2. sehr viele Dateien – die Windows-Befehlszeile ist auf ca. 32 000 Zeichen begrenzt,
3. **Flat-Gruppen** bei gedrehter Kamera – WBPP ordnet Flats über ein Kennwort im Pfad (`ASFLAT_1`) zu.

Verknüpfungen brauchen keinen Platz. Auf einer lokalen Platte klappt das immer. Für Dateien auf einem
**Netzlaufwerk** braucht Windows dafür den **Entwicklermodus** (Einstellungen → System → Für Entwickler →
Entwicklermodus). Ist er aus, kopiert b2stacker die betroffenen Dateien **einmal** nach
`<Arbeitsverzeichnis>/cache/staged/` und verwendet die Kopie bei späteren Läufen wieder. Der Selbsttest zeigt,
ob der Entwicklermodus aktiv ist.

## 11. Was b2stacker mit deinen Daten macht – und was nicht

- Originale werden **nie verändert, verschoben, umbenannt oder gelöscht**. Auch „Projekt entfernen“ oder
  „Stack entfernen“ löscht keine Bilddateien.
- Alles, was b2stacker erzeugt, liegt im Arbeitsverzeichnis: Analyse, Vorschauen, Verknüpfungen, Masters, Stacks.
- Ein gemeinsamer Katalog (`cache/catalog/`) merkt sich Header und Messwerte unveränderter Dateien, damit eine
  erneute Analyse schnell geht.
