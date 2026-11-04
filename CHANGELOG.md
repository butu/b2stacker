# Änderungen

Neueste Version oben. Der Abschnitt der aktuellen Version erscheint auch im Update-Dialog von b2stacker.

## 0.10.2 – 04.11.2026

**Für die Weiterentwicklung:** Claude kann jetzt eigene PixInsight-Skripte aus dem Arbeitsverzeichnis ausführen
(MCP-Werkzeug `run_pixinsight_script`). Damit testet Claude die neue Übergabe an WBPP direkt auf deinem Rechner,
ohne dass du Dateien doppelklicken musst.

- Läuft als normaler Auftrag in der Warteschlange: sichtbar im Fenster, abbrechbar, nie gleichzeitig mit dem Stacken.
- Nur Skripte (`.js`, `.scp`) im Arbeitsverzeichnis; Rohdaten und die PixInsight-Installation bleiben unberührt.
- Die Ausgabe von PixInsight steht im Protokoll des Auftrags und vollständig unter `logs/pixinsight-scripts/`.

## 0.10.1 – 04.11.2026

**Mehr Bildqualität bei vielen Frames.** WBPP hat Gruppen ab 200 Lights (ältere Versionen ab 150) bisher still auf
„Fast Integration“ umgeschaltet – mit schwächerer Ausrichtung und Pixel-Rejection. b2stacker schaltet diese
Automatik jetzt ab, jeder Lauf wird in voller Qualität gestackt.

## 0.10.0 – 04.11.2026

**Claude kann b2stacker jetzt bedienen.** b2stacker hat einen MCP-Server: Du kannst Claude z. B. bitten, die neuen
Nächte anzusehen, unscharfe Frames auszusortieren und RGB zu stacken.

**Neu**
- **Mit Claude verbinden** (Einstellungen → KI-Bedienung): trägt b2stacker in der Claude-Desktop-App ein, mit
  Sicherung der Konfiguration; andere Einträge bleiben unverändert. Danach die Claude-App neu starten.
- Claude sieht dieselben Projekte, Frames, Messwerte, Aussortiergründe, Rig-Hinweise und Pläne wie du und kann
  Ordner durchsuchen, Projekte anlegen und analysieren, Frames verwenden/weglassen, den Plan ändern, Stacking
  einreihen, den Fortschritt verfolgen, Protokolle lesen und Ergebnisse in PixInsight öffnen.
- Alles läuft sichtbar im b2stacker-Fenster und in derselben Warteschlange; Aufträge von Claude tragen
  „· per Claude“ und lassen sich wie gewohnt abbrechen. Läuft b2stacker nicht, startet Claude es.
- Bilddateien kann Claude nicht löschen, verschieben oder verändern; alle Änderungen sind umkehrbar. Jeder Aufruf
  steht im Protokoll.
- Kommandozeile: neue Befehle `frames`, `reanalyze` und `selftest`. `stack` reiht in die laufende App ein, statt ein
  zweites PixInsight zu starten.
- Stacken nur ausgewählter Läufe, ohne den gespeicherten Plan zu ändern; viele Frame-Entscheidungen auf einmal
  mit nur einer Neuberechnung des Plans.

**Behoben**
- Eine Datei mit mehreren Bildebenen (bereits debayert oder bearbeitet) galt als gutes Light und ließ dann das
  Stacken des ganzen Laufs scheitern. Sie wird jetzt bei der Analyse mit Hinweis aussortiert, auch wenn sie mit einer
  älteren Version bereits vermessen wurde.
- Startet b2stacker sich selbst neu (nach einem Update oder wenn Claude es öffnet), läuft die neue Instanz jetzt
  wirklich eigenständig. Bisher teilte sie sich den Temp-Ordner der alten Instanz.

## 0.9.3 – 04.11.2026

Härtung: b2stacker wurde mit absichtlich kaputten, ungewöhnlichen und großen Datensätzen, mit unsinnigen Eingaben
über Oberfläche und Kommandozeile sowie mit Mutationstests durchgeprüft. Ergebnis: weniger Abstürze, bessere
Frame-Auswahl, weniger unnötige Arbeit.

**Mehr Bildqualität**
- Satelliten- und Flugzeugspuren gelten nicht mehr als „unscharf“. Bisher wurde ein Frame mit Spur aussortiert,
  weil die Spur die Sternmessung verfälschte (z. B. FWHM 10 px statt 2,8 px). Jetzt wird die Spur aus der Messung
  herausgerechnet, der Frame bleibt im Stack und die Rejection entfernt die Spur. Nachführfehler werden weiterhin
  erkannt. Bereits analysierte Projekte: Nur Frames, die bisher als unscharf galten, werden einmalig nachgemessen.
- Doppelte Dateien (gleicher Inhalt in zwei Quellordnern, z. B. Original und Backup-Kopie) werden erkannt, nur
  einmal vermessen und nicht doppelt gestackt.
- Filter wie „Ha/OIII 7nm“, „OIII/Ha“ oder „SII 6nm + OIII 6nm“ werden als Dualband erkannt (bisher teils als
  reines OIII bzw. SII). Triband-Filter fragt b2stacker nach, statt zu raten.
- Gleicher Filter mit anderem Gain, Binning oder Bildformat ergibt eigene Läufe wie „RGB (Gain 0)“ statt zwei
  gleichnamiger Läufe, die sich die Frames teilten.

**Keine Abstürze mehr bei komischen Daten**
- Ein Frame mit unlesbarem oder fehlendem Aufnahmedatum ließ die ganze Analyse abstürzen. Die Nacht kommt jetzt
  aus dem Dateinamen (NINA, ASIAIR) oder dem Datumsordner.
- Winzige, leere (nur schwarz/weiß), NaN- oder abgeschnittene Dateien enden als klarer Hinweis am Frame statt mit
  Absturz oder unsinnigen Messwerten.
- Negative Belichtungszeit im Header wird ignoriert, statt den Frame als Bias einzusortieren.
- Eine beschädigte Einstellungsdatei verhindert den Start nicht mehr: Sie wird gesichert, es gelten
  Standardwerte, beim Start erscheint ein Hinweis.
- Projektnamen wie „CON“ oder sehr lange Namen funktionieren unter Windows.

**Verständlichere Meldungen**
- Lesefehler auf Deutsch und in Klartext („Datei unvollständig – Kopie abgebrochen?“ statt englischer
  Fehlermeldungen von astropy).
- Läufe heißen wie der Filter („Ha/OIII 7nm“ statt „Ha_OIII 7nm“).
- Frames: Die Zählzeile nennt jede Zahl nur noch einmal; „1 Vorschau fehlt“ statt „1 Vorschauen fehlen“.
- Leere Projekte zeigen „Noch keine Frames“ statt „Filter unbekannt“.
- Sind alle Läufe ausgeschaltet, sagt b2stacker genau das, statt „Keine gültige Analyse“.
- Läufe mit weniger als 3 Lights bekommen im Plan einen Hinweis (keine Rejection möglich).

**Schneller, weniger unnötige Arbeit**
- Unveränderte defekte Dateien werden nicht bei jeder Analyse erneut gelesen.
- Projektkacheln fragen nur noch nach einem Bild, wenn es eines gibt (keine Fehlabrufe); Projektbilder werden
  zwischengespeichert.
- Die Restzeit erscheint früher, sobald genug Frames vermessen sind.

**Eingaben werden geprüft (Oberfläche, Kommandozeile, KI-Bedienung)**
- Einstellungen: ungültige Werte (z. B. 0 gleichzeitige Vermessungen, Text statt Zahl, „false“ als Text) werden
  mit Hinweis abgelehnt statt gespeichert und erst bei der nächsten Analyse bemerkt.
- Plan-Änderungen: unbekannte Läufe und Optionen, ungültige Werte und Experten-Parameter mit Komma oder „=“ (die
  den PixInsight-Aufruf zerlegen würden) werden abgelehnt. Bisher blieb z. B. eine als Text „false“ gesendete
  Option eingeschaltet.
- Manuelle Frame-Entscheidungen nur noch für Bilddateien; Quellordner als einzelner Text werden nicht mehr in
  einzelne Buchstaben zerlegt.
- Kommandozeile: `astrostack plan <Projekt>` stürzte in der Textausgabe immer ab – behoben. Fehlende „=“,
  unbekannte Läufe (`--only`, `--disable`) und falsche Einstellungswerte ergeben eine klare Meldung statt eines
  Tracebacks; `settings --set site_lat=48,5` speichert eine Zahl.

**Für Entwickler**
- Rund 120 neue Regressionstests (`tests/test_hardening.py`), abgesichert mit Mutationstests: Die Tests erkennen
  absichtlich eingebaute Fehler in den gehärteten Funktionen zu 90–96 %.
- Release-Text auf GitHub und im Update-Dialog kommt aus diesem Changelog.
