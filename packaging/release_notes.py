"""Release-Text aus CHANGELOG.md: Abschnitt der aktuellen Version + fester Hinweis zum Start.
Der Text erscheint auf GitHub und im Update-Dialog der App („Änderungen ansehen“)."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOOTER = """
---
**b2stacker.exe herunterladen und doppelklicken** – b2stacker öffnet sich als eigenes Programmfenster.
Keine Installation, kein Python nötig. Voraussetzung: PixInsight.

Windows SmartScreen kann beim ersten Start warnen („Unbekannter Herausgeber“):
„Weitere Informationen“ → „Trotzdem ausführen“.
"""


def section(version: str, text: str) -> str | None:
    """Inhalt unter ``## <version>`` bis zur nächsten ``## ``-Überschrift."""
    m = re.search(r"^## \[?%s\]?[^\n]*\n(.*?)(?=^## |\Z)" % re.escape(version), text, re.S | re.M)
    return m.group(1).strip() if m else None


def main(version: str, out: str, sha: str = "") -> None:
    text = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    body = section(version, text)
    if not body:
        sys.exit("CHANGELOG.md hat keinen Abschnitt für Version %s" % version)
    extra = ("\nGebaut aus %s – geprüft: Tests, Selbsttest, Analyse mit Testdaten, Oberfläche.\n" % sha) if sha else ""
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body + "\n" + FOOTER + extra)


if __name__ == "__main__":
    main(*sys.argv[1:])
