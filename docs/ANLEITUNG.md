# b2stacker – Anleitung

b2stacker sammelt deine Astro-Aufnahmen je Objekt, prüft jeden Frame, ordnet Flats, Bias und Darks zu, schlägt
die WBPP-Einstellungen vor und stackt mit einem Klick in PixInsight. Alles außer dem Stacken läuft von selbst im
Hintergrund. Technische Details stehen in [NEUBAU.md](NEUBAU.md), Erfahrungen mit PixInsight in
[ERKENNTNISSE.md](ERKENNTNISSE.md).

**Inhalt**

1. [Installieren und erster Start](#1-installieren-und-erster-start)
2. [Projekte](#2-projekte)
3. [Was automatisch passiert](#3-was-automatisch-passiert)
4. [Frames sichten](#4-frames-sichten)
5. [Frames löschen](#5-frames-löschen)
6. [Plan und Stacken](#6-plan-und-stacken)
7. [Stacks vergleichen](#7-stacks-vergleichen)
8. [Galerie](#8-galerie)
9. [Verlauf](#9-verlauf)
10. [Kalibrier-Bibliothek](#10-kalibrier-bibliothek)
11. [Einstellungen, Sicherung, Selbsttest](#11-einstellungen-sicherung-selbsttest)
12. [Bedienung durch Claude und andere KI-Werkzeuge](#12-bedienung-durch-claude-und-andere-ki-werkzeuge)
    · [Am Handy](#am-handy)
13. [Was b2stacker mit deinen Daten macht – und was nicht](#13-was-b2stacker-mit-deinen-daten-macht--und-was-nicht)
14. [Wenn etwas schiefgeht](#14-wenn-etwas-schiefgeht)

---

## 1. Installieren und erster Start

1. Unter [Releases](https://github.com/butu/b2stacker/releases/latest) **`b2stacker.exe`** herunterladen und
   doppelklicken. SmartScreen-Warnung: „Weitere Informationen“ → „Trotzdem ausführen“.
2. Läuft b2stacker aus dem Download-Ordner, bietet die Startseite **Installieren** an. b2stacker liegt dann immer
   unter `%LOCALAPPDATA%\b2stacker\b2stacker.exe`, bekommt eine Verknüpfung im Startmenü, und Updates
   funktionieren zuverlässig. Die heruntergeladene Datei kann danach weg.
3. Beim ersten Start fragt b2stacker nach dem **Arbeitsverzeichnis** (Datenbank, Vorschauen, Ergebnisse – eine
   große, schnelle Platte, nicht OneDrive) und nach **PixInsight** (meist
   `C:\Program Files\PixInsight\bin\PixInsight.exe`; ein Häkchen zeigt, dass auch WBPP gefunden wurde).
4. Neue Versionen meldet oben rechts ein Knopf **Update x.y.z**. „Jetzt aktualisieren“ lädt die neue Version,
   ein kleines Fenster zeigt den Austausch, und b2stacker startet neu. Geht etwas schief, bleibt die alte Version.

Die alte Oberfläche (bis 0.10) startet noch mit der Umgebungsvariable `B2_OLD_UI=1`. Projekte der alten Version
werden nicht übernommen; ihre Ordner im Arbeitsverzeichnis bleiben unangetastet.

## 2. Projekte

Die **Startseite** zeigt alle Projekte als Kacheln mit Belichtungsstunden je Filter, letztem Stack und Hinweisen
(„Quelle offline“, „3 neue Nächte seit dem letzten Stack“). Suchen nach Name oder Objekt, sortieren, nach Monat
gruppieren.

**Neues Projekt** → Name, optional eine Beschreibung, und ein oder mehrere Ordner mit Aufnahmen. Lights, Flats,
Bias und Darks dürfen gemischt in Unterordnern liegen: b2stacker erkennt die Art **am Header**, nie am Ordnernamen.
Ordner mit `_rejected`, `_verworfen` oder `_alt` im Namen gelten als aussortiert. Ordner lassen sich später in der
Übersicht ergänzen oder entfernen; die Dateien selbst bleiben, wo sie sind.

Ein Projekt hat die Reiter **Übersicht**, **Frames**, **Plan**, **Stacks**, **Galerie** und **Verlauf**.
Name und Beschreibung änderst du mit dem Stift neben dem Namen.

## 3. Was automatisch passiert

Beim Programmstart, beim Öffnen eines Projekts, nach dem Hinzufügen eines Ordners und wenn der **Watcher** neue
Dateien sieht (Einstellung „Neue Dateien suchen alle … s“), läuft im Hintergrund:

1. **Entdecken** – welche Bilddateien liegen in den Ordnern (nur Name, Größe, Datum).
2. **Header lesen** – Art, Filter, Kamera, Gain, Belichtung, Nacht.
3. **Vermessen** – Sterne, FWHM, Sternform, Hintergrund, Rauschen, Vorschaubild. Jede Datei wird genau einmal
   gelesen; unveränderte Dateien nie wieder.
4. **Bewerten** – unscharfe, verwackelte oder bewölkte Frames werden mit Grund in Klartext aussortiert; Flats,
   Bias und Darks werden über den Header zugeordnet.
5. **Plan** – je Filter ein Lauf mit allen WBPP-Einstellungen und einer Begründung für jede.

Oben rechts zeigt ein kleiner Hinweis, was gerade läuft, mit Restzeit; ein Klick zeigt alle Aufträge und lässt
sie abbrechen. Der Watcher wartet, bis ein Ordner sich zwei Runden lang nicht mehr ändert – er stört also nicht,
während NINA noch aufnimmt. Ein ausgeschaltetes NAS wird übersprungen („Quelle offline“), nichts geht verloren.

## 4. Frames sichten

Der Reiter **Frames** zeigt alle Frames einer Art (Lights, Flats, Bias, Darks) in einer durchgehenden Kachelansicht,
auch bei tausenden Frames ohne Blättern. Filter: Alle, Verwendet, Aussortiert, Zum Löschen, Manuell. Mehrere
auswählen: Kästchen anklicken, **Shift**+Klick für einen Bereich, **Strg**+Klick einzeln. Dann unten
Verwenden / Nicht verwenden / Automatik / Zum Löschen vormerken.

**Großansicht** (Klick auf eine Kachel):

| Taste | Wirkung |
|---|---|
| ← → | blättern (bleibt am letzten Bild stehen) |
| V / N | verwenden / nicht verwenden, dann weiter |
| A | zurück auf Automatik |
| Entf | zum Löschen vormerken, dann weiter |
| S | erkannte Sterne einblenden |
| Mausrad, + / −, 0 | Zoom, 0 = einpassen; Doppelklick wechselt zwischen eingepasst und 1:1 |
| Esc | schließen |

**Sterne einblenden** zeigt b2stackers eigene Sternerkennung als Ellipsen: Größe = FWHM, Form und Richtung =
Exzentrizität; grün = rund, gelb = leicht oval, rot = länglich. Darunter: Anzahl, FWHM, Sternform. Die Erkennung
dauert beim ersten Mal ein paar Sekunden und wird gemerkt.

Am Handy: wischen nach links/rechts blättert, nach oben = verwenden, nach unten = nicht verwenden, zwei Finger
zoomen.

Deine Entscheidungen bleiben bei jeder neuen Prüfung erhalten. „Automatik“ gibt einen Frame wieder an die
Bewertung zurück.

## 5. Frames löschen

Löschen ist die **einzige** Aktion, bei der b2stacker deine Rohdaten verändert – und nur so:

1. Frames **zum Löschen vormerken** (Auswahl unten oder **Entf** in der Großansicht). Vorgemerkte Frames werden
   nicht gestackt, aber noch nicht gelöscht. „Zum Löschen vormerken“ ist etwas anderes als „nicht verwenden“.
2. Über den roten Hinweis **Liste ansehen und löschen …** siehst du alle vorgemerkten Frames mit Größe. Einzelne
   kannst du dort noch **behalten**.
3. Der Knopf nennt Anzahl und Größe („12 Frames, 1,4 GB löschen“). Erst dieser Klick löscht.

- Auf lokalen Platten landen die Dateien im **Papierkorb**.
- Auf einem NAS oder USB-Stick gibt es keinen Papierkorb: Diese Dateien sind markiert, und gelöscht wird erst, wenn
  du „Ja, endgültig löschen“ ankreuzt.
- Ein Frame, den ein anderes Projekt noch verwendet, wird nicht gelöscht.
- Jede Löschung steht im Verlauf (mit Dateinamen) und im Protokoll.
- Claude und andere KI-Werkzeuge können **nie** löschen oder vormerken. Am Handy lässt sich nur vormerken.

## 6. Plan und Stacken

Der Reiter **Plan** zeigt einen **Lauf** je Filter (bei unterschiedlichem Gain oder Binning getrennt), mit Lights,
Stunden, Nächten und den zugeordneten Flats (je Nacht), Bias und Darks samt Bewertung. Jede WBPP-Einstellung
(Rejection, Gewichtung, Local Normalization, Drizzle, Hotpixel, Verzerrungskorrektur, Spuren entfernen,
Zuschneiden …) steht mit Begründung da. Du kannst sie je Lauf oder für alle ändern; deine Änderung ist markiert und
bleibt, auch wenn neue Frames dazukommen. „Zurück auf Automatik“ hebt sie wieder auf.

**Stacken** startet PixInsight mit WBPP für alle eingeschalteten Läufe. Der Fortschritt steht oben rechts. Das
Ergebnis landet im Projektordner, ein Ordner je Stack, z. B. `2026-11-04 Ha 12h30` (Master, WBPP-Protokoll). Weitere
Läufe werden auf das erste Ergebnis ausgerichtet, damit alle Filter pixelgenau übereinanderliegen. Master-Bias,
-Darks und -Flats werden aufgehoben und beim nächsten Mal wiederverwendet (siehe Kalibrier-Bibliothek).

Vor dem Start prüft b2stacker den freien Arbeitsspeicher (Einstellung „Mind. freier RAM“). Während des Stackens
bleibt der PC wach. Ist „Ergebnis nach dem Stacken in PixInsight öffnen“ an, öffnet PixInsight das Ergebnis.

## 7. Stacks vergleichen

Der Reiter **Stacks** zeigt alle Stacks, neueste zuerst, mit Vorschau, Lights, Stunden, Größe, Qualitätswerten
(Schärfe, Rauschen, Sterne, Sternform, Verlauf) und einer **Notiz**. Je Stack: **In PixInsight öffnen**,
**Als Projektbild**, **Löschen** (löscht nur, was b2stacker erzeugt hat – eigene Dateien im Ordner bleiben).

**Vergleichen:** zwei Stacks anhaken oder „Neuesten vergleichen“. Beide Bilder liegen übereinander, mit gleicher
Streckung; der Schieber zeigt links A, rechts B. b2stacker richtet B auf A aus – auch wenn die Kamera gedreht wurde.
Rechts stehen die Werte mit „besser/schlechter“. Mausrad oder Doppelklick zoomt; ziehen neben dem Schieber
verschiebt das Bild.

## 8. Galerie

Bearbeitete Bilder (JPEG, PNG, TIFF, PSD, XISF, FITS) gehören zum Projekt: **Bilder hinzufügen …** oder
**Ordner hinzufügen …**, oder einfach in den Projektordner legen – der Watcher findet sie. Die Dateien bleiben, wo sie
sind. Ein Bild kann **Projektbild** der Startseiten-Kachel werden. „Aus der Galerie entfernen“ blendet es nur aus.

## 9. Verlauf

Der **Verlauf** (je Projekt oder oben rechts für alle Projekte) erzählt in Klartext, was passiert ist: „20 neue
Lights · Ha · 2,0 h, Nacht vom 02.11.2026“, „Gestackt: Ha 12h30“, Entscheidungen von Hand, Planänderungen,
Löschungen. Gleichartige Einträge werden zusammengefasst.

## 10. Kalibrier-Bibliothek

Oben rechts **Kalibrierung** zeigt alle Master, die b2stacker beim Stacken aufgehoben hat, über alle Projekte: Art,
Anzahl Frames, Filter, Belichtung, Gain, Offset, Temperatur, Nächte und in welchen Stacks sie verwendet wurden.

Braucht ein Stack – in irgendeinem Projekt – **genau dieselben Kalibrier-Rohframes**, nimmt b2stacker den fertigen
Master, statt ihn neu zu rechnen. Kommt ein Frame dazu oder fällt einer weg, entsteht ein neuer Master. Gespart wird
das Erzeugen der Master; Kalibrieren und Registrieren der Lights bleibt bei jedem Stack.

Entfernen löscht nur den gespeicherten Master (eine von b2stacker erzeugte Datei), nie die Rohframes.

## 11. Einstellungen, Sicherung, Selbsttest

Zahnrad oben rechts:

| Einstellung | Bedeutung |
|---|---|
| Arbeitsverzeichnis | Datenbank, Vorschauen, Temp-Ordner, Ergebnisse. |
| PixInsight-Programm | Pfad zu `PixInsight.exe`. |
| Gleichzeitige Vermessungen | Mehr = schneller, je ~1 GB Arbeitsspeicher. |
| Neue Dateien suchen alle (s) | So oft schaut der Watcher in deine Ordner. |
| Mind. freier RAM (GB) | Darunter startet kein Stacken. |
| Ersatz-Standort | Breite, Länge, Höhe – wenn die Aufnahmen keinen Standort enthalten (Nacht, Mondstand). |
| PC wach halten | Kein Energiesparmodus während des Stackens. |
| Ergebnis öffnen | Nach dem Stacken in PixInsight öffnen. |

**Wartung:** Datenbank jetzt sichern, Temp-Dateien löschen, ein **zusätzlicher Sicherungsort** (z. B. NAS) und die
Liste der Sicherungen. **Wiederherstellen** spielt eine Sicherung sofort zurück (vorher wird der jetzige Stand
gesichert). Bilddateien und Stacks auf der Platte bleiben dabei, wie sie sind.

**Selbsttest mit PixInsight:** legt in einem Ordner deiner Wahl (für den NAS-Test einen Ordner auf dem NAS) einen
kleinen Testdatensatz an (`b2stacker-selbsttest, mit Komma`: zwei Nächte, gedrehte Kamera, Flats je Nacht, Bias),
liest ihn ein und stackt ihn zweimal mit PixInsight – das zweite Mal mit den gespeicherten Mastern. Das Testprojekt
erscheint nicht in der Projektliste. Der Bericht liegt unter `<Arbeitsverzeichnis>/selbsttest/bericht-neu-*.json`;
die letzten Ergebnisse stehen in den Einstellungen.

## 12. Bedienung durch Claude und andere KI-Werkzeuge

b2stacker lässt sich von KI-Werkzeugen bedienen (MCP), z. B. „Schau dir die neuen M-31-Nächte an, sortier
unscharfe Frames aus und stack Ha“. Unter **Einstellungen → KI-Bedienung (MCP)** stehen die Adresse
(`http://127.0.0.1:8775/mcp`) und der Schlüssel, dazu fertige Einträge für **OpenCode**, **Claude Code** und andere
Werkzeuge zum Kopieren. Die Adresse ist nur auf diesem Rechner erreichbar; den Schlüssel wie ein Passwort behandeln
(„Neu erzeugen“ macht den alten ungültig). Für die Claude-Desktop-App gibt es weiterhin `b2stacker.exe mcp`, das an
das laufende Programm weiterleitet.

**Was die KI kann:** Projekte, Frames mit Messwerten und Gründen, Plan, Stacks mit Qualität, Verlauf und
Kalibrier-Bibliothek ansehen; Projekte anlegen, Ordner hinzufügen, Frames verwenden/weglassen, den Plan ändern,
stacken, Aufträge verfolgen und abbrechen, Notizen setzen, Stacks vergleichen, Ergebnisse in PixInsight öffnen,
Einstellungen ändern, den Selbsttest starten und eigene PixInsight-Skripte zur Diagnose ausführen. Aufträge der KI
tragen „· per Claude“ im Titel.

**Was die KI nicht kann:** Bilddateien löschen, vormerken, verschieben oder verändern.

### Am Handy

**Einstellungen → Handy (Heimnetz)** einschalten. Es erscheint ein QR-Code: mit der Kamera des Handys scannen, und
b2stacker öffnet sich im Browser des Handys (Tipp: „Zum Startbildschirm hinzufügen“). Fragt Windows beim ersten Mal
nach der Firewall, „Private Netzwerke“ erlauben. Der Computer muss laufen und b2stacker offen sein.

- Erreichbar nur im eigenen Netz (private Adressen) und nur für gekoppelte Geräte – wer den QR-Code nicht gescannt
  hat, sieht nichts. **Alle Handys abmelden** erzeugt einen neuen QR-Code.
- Am Handy geht: Projekte und Frames ansehen, wischen zum Aussortieren, Plan ändern, Stacken, Stacks vergleichen,
  Galerie, Verlauf. Frames zum Löschen vormerken geht, **gelöscht wird nur am Computer**. Einstellungen, Sicherung,
  Update und Beenden gibt es am Handy nicht.

## 13. Was b2stacker mit deinen Daten macht – und was nicht

- Rohdaten werden nur **gelesen**. Einzige Ausnahme: das Löschen vorgemerkter Frames nach deiner Bestätigung
  (Abschnitt 5).
- Alles, was b2stacker erzeugt, liegt im Arbeitsverzeichnis: `projects/<Projekt>/` (ein Ordner je Stack,
  `masters/`), `temp/` (vor und nach jedem Stack geleert, darf jederzeit gelöscht werden) und `system/`
  (Datenbank, Sicherungen, Vorschauen, Protokolle).
- Fehlt eine Datei, heißt sie „Datei fehlt“ – Entscheidungen bleiben erhalten, falls sie wiederkommt.
- Eigene Dateien im Projektordner (z. B. `final_final.tif` in einem Stack-Ordner) bleiben immer unangetastet.

## 14. Wenn etwas schiefgeht

- Ein fehlgeschlagener Stack steht mit Grund in der Stack-Liste und im Verlauf; die anderen Läufe laufen weiter.
  Das WBPP-Protokoll liegt im Stack-Ordner unter `protokoll/`.
- Der **Selbsttest** zeigt, ob PixInsight, WBPP und das NAS mitspielen.
- Protokolle: `<Arbeitsverzeichnis>/logs/` (`events-JJJJ-MM.jsonl`). Für Fehlermeldungen an Entwickler oder
  KI-Werkzeuge: `b2stacker.exe logs --kind error --trace`.
