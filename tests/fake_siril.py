"""Nachgebaute siril-cli für Tests – als **eigener Prozess** mit echter Kommandozeile.

Nachgebildet (grob, Siril 1.4):
* ``siril-cli --version`` → ``siril 1.4.4`` (Versionsprüfung)
* ``siril-cli -d <dir> -s <skript.ssf>``: liest das Skript Zeile für Zeile
  - ``cd <dir>`` wechselt das Verzeichnis, ``log <text>`` schreibt ``log: <text>``
  - ``stack <quelle> … -out=<name>`` schreibt ein float32-RGB-FITS (NAXIS3=3) als ``<name>.fit``
  - alle anderen Befehle (convert, calibrate, register …) werden nur protokolliert
* Fortschritt auf stdout im Siril-Stil: ``log: …`` und ``progress: n/m``.

Gerüst für Phase 1; die Befehlstreue wird in Phase 2 erweitert.
"""
from __future__ import annotations

import os
import shlex
import sys

import numpy as np

VERSION = "siril 1.4.4"


def _write_fits(path: str, header: dict) -> None:
    from astropy.io import fits
    y, x = np.mgrid[0:120, 0:160]
    rng = np.random.default_rng(1)
    chans = [0.05 + 0.01 * rng.standard_normal((120, 160)) + 0.1 * np.exp(-((x - 80) ** 2 + (y - 60) ** 2) / 50.0)
             for _ in range(3)]
    hdu = fits.PrimaryHDU(data=np.clip(np.array(chans, dtype=np.float32), 0, 1))
    for k, v in header.items():
        hdu.header[k] = v
    hdu.writeto(path, overwrite=True)


def run_script(workdir: str, script: str) -> int:
    os.chdir(workdir)
    lines = [ln.strip() for ln in open(script, encoding="utf-8") if ln.strip() and not ln.startswith("#")]
    total = len(lines)
    for i, line in enumerate(lines, 1):
        cmd, *args = shlex.split(line)
        if cmd == "cd":
            os.chdir(os.path.join(workdir, args[0]) if not os.path.isabs(args[0]) else args[0])
        elif cmd == "log":
            print("log: " + " ".join(args), flush=True)
        elif cmd == "stack":
            out = next((a.split("=", 1)[1] for a in args if a.startswith("-out=")), "result")
            _write_fits(os.path.join(os.getcwd(), out + ".fit"),
                        {"OBJECT": "fake", "FILTER": "RGB", "IMAGETYP": "Master Light Frame"})
            print("log: stacked -> %s.fit" % out, flush=True)
        else:
            print("log: %s (nur protokolliert)" % cmd, flush=True)
        print("progress: %d/%d" % (i, total), flush=True)
    return 0


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if "-s" not in argv:
        print("usage: siril-cli -d <dir> -s <script>", file=sys.stderr)
        return 2
    workdir = argv[argv.index("-d") + 1] if "-d" in argv else os.getcwd()
    return run_script(workdir, argv[argv.index("-s") + 1])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
