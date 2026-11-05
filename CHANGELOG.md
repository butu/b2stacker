# Änderungen

Neueste Version oben. Der Abschnitt der aktuellen Version erscheint auch im Update-Dialog von b2stacker.

## 0.13.1 – 05.11.2026

- **„Was auffällt“ zeigt bei Flats den echten Grund.** Bisher stand dort je Nacht eine rote Zeile „Flats vom … (7 Tage entfernt)“. Rot war aber die Helligkeit der Flats („Flats zu hell“), nicht der Abstand. Jetzt gibt es eine Zeile je Flat-Satz mit dem Grund und den Nächten, für die er gilt. Mehr als 14 Tage Abstand erscheinen nur als Info und verschwinden mit „Passt, Setup unverändert“.
- Claude sieht im Projektüberblick ebenfalls den Grund (`findings`) statt nur den Abstand.
- **Rig-Hinweise laufen auf echten Daten.** 0.13.0 meldete „array type dtype('float16') not supported“: Die Vorschaubilder sind float16, damit rechnet die Dithering-Erkennung nicht. Sie werden jetzt vorher umgewandelt; ein Test rechnet mit echten Thumbnails statt einer Attrappe. Eine Fehlermeldung an dieser Stelle hat jetzt ein Warnsymbol statt eines grünen Hakens.
- **Keine leeren Ansichten mehr durch „NaN“.** Fehlt ein Messwert, schrieb der Server „NaN“ ins JSON, und der Browser verwarf die ganze Antwort. Der Server liefert jetzt überall „leer“ statt NaN.
- **Ältere Stacks heißen wie ihr Ordner** (z. B. „2026-11-05 RGB 8h30“ statt „RGB“). Selbst vergebene Namen bleiben.

## 0.13.0 – 05.11.2026

**Dein Feedback zu 0.12.2 – alles in einem Update.**

**Behoben**
- **Auswahlfelder** übernehmen wieder jede Auswahl (die Liste lag unter einer unsichtbaren Fläche). Ein Test klickt
  jetzt jedes Auswahlfeld einmal durch.
- **Frame- und Stack-Auswahl** sehen wieder richtig aus: dezenter Rahmen, Vorschaubild und Haken bleiben sichtbar;
  die Stack-Karte schrumpft nicht mehr.
- **Stacks groß ansehen und vergleichen:** Ein Klick aufs Vorschaubild öffnet den Stack groß; dort „Vergleichen …“
  und ein zweites Bild wählen – Stacks, bearbeitete Bilder aus der Galerie oder beides gemischt.
- **„Neu prüfen“** holt fehlende Messwerte und Vorschaubilder wirklich nach und sagt danach, was es getan hat. Nach
  einem Update mit neuen Messwerten passiert das beim Start von selbst.
- **Vorschaubilder für Flats, Bias, Darks und Dark-Flats.**
- **Rig-Hinweise** sagen genau, was fehlt und was zu tun ist, statt „zu wenige Messwerte“.
- **Flat-Bewertung:** Abstand bis 14 Tage ist kein Thema; darüber nur ein Hinweis (egal ob davor oder danach), den
  du mit „Passt, Setup unverändert“ fürs Projekt bestätigen kannst. Rot sind Flats nur noch bei echten Problemen –
  und das Kästchen sagt, welches (bei vdB 131: „Flats zu hell“, 48k ADU).
- **Nach dem Update** laufen keine Prozesse der alten Version mehr weiter. Wer Claude verbunden hat: die Claude-App
  danach einmal neu starten.
- **Handy:** eingebauter Verbindungstest (Einstellungen → Handy → „Verbindung testen“): Adresse am Handy öffnen, und
  b2stacker zeigt live, ob die Anfrage ankommt. Statt des Netzwerknamens stehen Adapter und IP da; eine zusätzliche
  Firewall (z. B. vom Virenschutz) wird erkannt.

**Bedienung**
- **Neuer Reiter „Stacking“** statt „Plan“ und „Stacks“: alle Stacks als Kacheln (auch fehlgeschlagene), vorne
  „Neuer Stack“ – führt zum Plan und von dort zum Start.
- **Stack-Einstellungen:** je Zeile „Automatisch (an)“ oder ein fester Wert. Feste Werte sind markiert und bleiben,
  auch wenn sich die Automatik (z. B. durch „nur die besten Frames“) ändert.
- **Gruppieren nach** Nacht, Filter, Status, Aussortiergrund, Belichtungszeit oder Quellordner; Gruppen lassen sich
  auf- und zuklappen (auch die Monate auf der Startseite).
- **Stack-Namen** wie der Ergebnisordner, z. B. „2026-11-05 RGB 8h30 · beste 50 %“.
- **„Stacks übernehmen …“** ist ins Projekt-Menü (⋯) gewandert.
- **Nächte:** sortierbare Spalten (zweiter Klick dreht um), klare Beschriftung „verwendet / aufgenommen“ mit
  Erklärung, größere Punkte im Diagramm, Kamera und Bildmaßstab bei den Rig-Hinweisen.
- **Tipps** nur noch, wenn sie konkret sind (z. B. eine unscharfe Nacht aussortieren, wie viele Stunden ein Filter
  noch braucht).
- **Plan zeigt die Master-Wiederverwendung:** welcher Master-Bias/-Flat/-Dark wiederverwendet wird – oder warum nicht.
- Kurze Prüfungen blitzen nicht mehr auf; der Hinweis erscheint erst nach einer Sekunde.

**Neu**
- **Stacks in der Galerie** (als Stack gekennzeichnet) – zum direkten Vergleich mit dem fertigen Bild.
- **Verlauf verlinkt:** „Gestackt …“ öffnet die Stacks, „neue Lights“ die Frames, neue Bilder die Galerie.
- **„Flats anzeigen“** je Nacht in der Nachtübersicht – mit Vorschaubildern und Sprung zu genau diesen Flats.
- **Vorhandene Stacks erkennen:** Liegen masterLight-Dateien in deinen Ordnern, bietet „Stacking“ an, sie zu
  übernehmen.

## 0.12.3 – 05.11.2026

- **Rig-Hinweise** in der Übersicht erscheinen jetzt auch mit echten Daten (statt „konnten nicht berechnet
  werden“): Nachführung, Optik, Fokus, Taupunkt, Kühlung, Offset, Dithering.

## 0.12.2 – 05.11.2026

**Fehler wie beim RGB-Stack von vdB 131 an der Wurzel verhindert.** Damals ging eine Einstellung als Text statt als
Zahl an PixInsight – erst nach Minuten scheiterte das Ausrichten, und am Ende stand nur „kein Master“.

- **Jede Einstellung wird vor dem Start geprüft:** b2stacker kennt alle WBPP-Einstellungen, die es setzt, samt Art
  (Zahl, ja/nein, Text) und gültigem Bereich – abgeglichen mit dem echten WBPP 3.2.0. Was nicht passt, wird
  eindeutig umgewandelt oder stoppt den Lauf sofort mit Klartext, bevor PixInsight startet.
- **Startprüfung in PixInsight:** Bevor WBPP rechnet, vergleicht b2stacker jede Einstellung mit den Voreinstellungen
  der installierten WBPP-Version. Ein Fehler fällt nach Sekunden auf statt nach etlichen Minuten.
- **Werte im Plan:** Von Hand eingegebene Sternerkennungs-Werte werden beim Speichern geprüft (Komma oder Punkt
  egal) und als Zahlen gespeichert; Unsinn wird mit Hinweis abgelehnt.
- **Selbsttest** stackt jetzt auch mit von Hand gesetzter Sternerkennung und Rejection – genau dem Weg, an dem
  vdB 131 gescheitert ist.

## 0.12.1 – 05.11.2026

- **Handy kam nicht durch:** b2stacker prüft jetzt die Windows-Firewall. Sperrt sie (z. B. weil der Hinweis beim
  ersten Start weggeklickt wurde) oder ist das WLAN als „Öffentlich“ eingestuft, steht das unter Einstellungen →
  Handy, und **„Freigeben“** richtet die Freigabe ein – nur für Geräte aus dem Heimnetz und aus dem VPN. Windows
  fragt dabei einmal nach Administratorrechten.
- Handy über VPN: Auch Adressen von Tailscale & Co. (100.64.x–100.127.x) werden angenommen; gekoppelt werden muss
  das Handy weiterhin. Abgewiesene Adressen stehen in den Einstellungen, statt dass die Verbindung still abbricht.

## 0.12.0 – 05.11.2026

**Was in 0.11 gefehlt hat, ist zurück – und mehr.**

**Behoben**
- **RGB-Stack schlug fehl („WBPP hat kein Master erzeugt“):** b2stacker hat die Werte der Sternerkennung als Text
  statt als Zahl an PixInsight übergeben, deshalb hat das Ausrichten jeden Frame abgelehnt. Jetzt gehen alle
  Einstellungen als Zahlen an WBPP, und bei so einem Fehler steht die richtige Ursache dabei.
- **Update blieb bei der alten Version:** Lief noch ein Prozess der vorletzten Version (z. B. die Claude-Verbindung),
  konnte die alte Datei nicht ersetzt werden. Das Update weicht jetzt aus, räumt Altversionen beim Start auf und
  sagt, falls doch etwas schiefgeht, warum.

**Übersicht**
- **Nachtübersicht:** je Nacht und Filter Frames, Belichtung, effektive Zeit, FWHM, Mond, Objekthöhe und eine Note
  mit Begründung und Empfehlung. Klick zeigt die Himmelsbahn (Objekt, Mond, Dämmerung, Aufnahmen), „Frames“ springt
  genau zu dieser Nacht.
- **Rig-Hinweise:** Schärfe, Nachführung, Fokus (auch Temperatur-Zusammenhang), Dithering, Kühlung, Taupunkt,
  Offset und Belichtung – jeweils was auffällt, warum und was zu tun ist.
- **Ausführlicher Bericht** wie in 0.10, auch als HTML-Datei speicherbar.

**Stacks**
- **Ablauf beim Stacken:** welche Phasen erledigt sind, was gerade läuft und was als Nächstes kommt, alle
  WBPP-Schritte mit Dauer, grobe Restzeit und das Live-Protokoll von PixInsight.
- **Spickzettel** je Ergebnis: Reihenfolge der Bearbeitung in PixInsight, passend zu RGB oder Ha/OIII.
- **Vorhandene Stacks übernehmen:** Projektordner aus 0.10, WBPP-Ausgabe oder einzelne Master – mit Vorschau,
  Qualität und Vergleich. Die Dateien bleiben, wo sie sind.
- Erklärungen zu den Qualitätswerten (mit Richtwerten) direkt an jedem Wert.

**Frames und Großansicht**
- Großansicht sofort in voller Größe (vorbereitet und vorausgeladen), Sterne ohne Wartezeit, Klick zoomt auf
  100 %, viel mehr Infos (Temperatur, Koordinaten, Gain/Offset, Kamera, Teleskop, Höhe, Mond, HFR).
- Sortieren nach Aufnahmezeit, FWHM, Sternform, Sternen, Rauschen, Hintergrund, Belichtung; filtern nach Filter und
  Nacht; nach Nacht gruppieren; Größenregler für die Vorschaubilder (wird gemerkt).
- Hilfe: wie die Frames bewertet werden und was du tun kannst.

**Plan**
- Je Lauf aufklappbar: **Kalibrierung** (Flats je Nacht, Bias/Darks – auch von Hand) mit Erklärung, wie b2stacker
  wählt, und **Optionen**: beste X %, Referenzbild, Lauf umbenennen, jeder Wert der Sternerkennung einzeln.
- Alte Flats sind nur noch ein Hinweis mit Begründung, kein Ausschluss.
- Platzanzeige verständlich: „braucht etwa 42 GB · frei auf G: 54 GB von 1,8 TB“.

**Galerie und Kalibrierung**
- Galerie: alle Bild-Infos – Pixelmaße, Bittiefe, Bildmaßstab und -feld, Sterne, FWHM, Rundheit, Rauschen,
  Hintergrund und der komplette FITS-Header bzw. EXIF.
- Kalibrier-Bibliothek: neben den Mastern auch alle Rohframes (Flats, Bias, Darks) aus allen Projekten, gruppiert
  danach, wie sie zusammenpassen.

**Sonst**
- Auswahlboxen im Stil der App, Zoom der ganzen Oberfläche (Strg + Plus/Minus/0 oder Mausrad).
- Einstellungen: „Mit Claude verbinden“ ist zurück.
- Für Claude (MCP): neue Werkzeuge `insights` (Nächte, Rig-Hinweise) und `import_stacks`.

## 0.11.2 – 05.11.2026

- Schlägt ein Stack fehl, steht die Ursache in Klartext dabei (aus dem WBPP-Protokoll), und das Protokoll bleibt im
  Projektordner erhalten („Protokoll öffnen“ in der Stack-Liste).
- QR-Code fürs Handy wird nicht mehr abgeschnitten.
- Großansicht: Die Seitenleiste ragt nicht mehr über den Rand; die Wisch-Hinweise erscheinen nur noch am Handy.

## 0.11.1 – 05.11.2026

**b2stacker am Handy.** Einstellungen → Handy (Heimnetz) einschalten, den QR-Code mit dem Handy scannen – fertig.
Dann kannst du im ganzen Haus Frames sichten, wischend aussortieren, den Plan ändern, stacken und Stacks vergleichen.

- Nur im eigenen Netz und nur für gekoppelte Geräte; „Alle Handys abmelden“ macht einen neuen QR-Code.
- Rohdaten löschen, Einstellungen, Sicherung und Update gehen nur am Computer.
- Windows fragt beim ersten Einschalten nach der Firewall: „Private Netzwerke“ erlauben.

## 0.11.0 – 05.11.2026

**Die neue b2stacker-Oberfläche.** b2stacker ist von Grund auf neu gebaut: Alles außer dem Stacken passiert jetzt
von selbst im Hintergrund, und gestackt wird mit einem Klick. Deine bisherigen Projekte aus 0.10 werden nicht
übernommen – lege sie einfach neu an, das Einlesen geht schnell. Die alten Ordner bleiben unangetastet.

**Neu**
- **Installieren:** Läuft b2stacker aus dem Download-Ordner, bietet die Startseite an, es nach
  `%LOCALAPPDATA%\b2stacker` zu installieren – mit Startmenü-Verknüpfung. Updates zeigen den Austausch in einem
  kleinen Fenster; geht etwas schief, startet die bisherige Version.
- **Automatisch:** Neue Dateien finden, Header lesen, vermessen, bewerten, Flats/Bias/Darks zuordnen und den Plan
  rechnen – beim Start, beim Öffnen eines Projekts und sobald neue Nächte in deinen Ordnern landen (Watcher).
  Jede Datei wird nur einmal gelesen. Ein ausgeschaltetes NAS wird übersprungen.
- **Startseite** mit Kacheln (Stunden je Filter, letzter Stack, neue Nächte), Suche, Sortierung, Monatsgruppen.
- **Frames sichten** ohne Blättern, auch bei tausenden Frames. Großansicht mit Sternen (S), Zoom (Mausrad,
  Doppelklick 1:1) und Tastatur (V/N/A). Am Handy: wischen.
- **Frames löschen:** vormerken, Liste prüfen, bestätigen – in den Papierkorb, auf dem NAS nur nach ausdrücklicher
  Bestätigung. Die KI kann nie löschen.
- **Plan:** jede WBPP-Einstellung mit Begründung; deine Änderungen bleiben, auch wenn neue Frames dazukommen.
- **Stacken** über die neue Übergabe an WBPP: ein Ordner je Stack im Projektordner, alle Filter pixelgenau
  übereinander, Master werden aufgehoben und wiederverwendet.
- **Stacks:** Liste mit Qualitätswerten, Notizen, Vorschau, „In PixInsight öffnen“; **Vergleich** zweier Stacks
  übereinander mit Schieber und Zoom – auch wenn die Kamera gedreht war.
- **Kalibrier-Bibliothek:** alle aufgehobenen Master-Bias, -Darks und -Flats über alle Projekte; braucht ein Stack
  dieselben Kalibrierframes, wird der fertige Master genommen.
- **Galerie** für bearbeitete Bilder, **Projektbild**, **Verlauf** in Klartext.
- **Sicherung** der Datenbank mit zusätzlichem Sicherungsort und Wiederherstellen ohne Neustart.
- **Selbsttest** mit PixInsight (auch für das NAS): stackt einen Testdatensatz zweimal, das zweite Mal mit den
  gespeicherten Mastern.
- **KI-Bedienung:** In den Einstellungen stehen MCP-Adresse und Schlüssel samt fertiger Einträge für OpenCode,
  Claude Code und andere Werkzeuge. Die Claude-Desktop-App bekommt ihre Werkzeuge von der laufenden App.

Die alte Oberfläche startet noch mit der Umgebungsvariable `B2_OLD_UI=1`.

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
