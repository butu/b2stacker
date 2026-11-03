"""Nachgebaute siril-cli für Tests – als **eigener Prozess** mit echter Kommandozeile.

Interpretiert die Befehle, die engines/siril.py erzeugt (Verhalten grob nach Siril 1.4.4):
* ``siril-cli --version`` → ``siril 1.4.4``
* ``siril-cli -d <dir> -s <skript.ssf>`` (Skriptpfad absolut, wie in der Engine)
* ``requires``, ``setext``, ``cd``, ``setfindstar`` (no-op), ``convert <name> -out=<dir>``, ``merge <seq>… <out>``, ``setref``, ``unselect``,
  ``calibrate <seq>_ [-bias= -dark= -flat= -cc= -cfa -equalize_cfa -debayer] -prefix=P``,
  ``register <seq>_``, ``seqapplyreg <seq>_ -prefix=P``, ``stack <seq>_ … -out=N``.
* Ausgabe wie Siril: ``log: Running command: <cmd>``, ``progress: …, NN.NN%``; bei Fehler
  ``log: Error in line N ('cmd'): …`` + ``log: Script execution failed.`` und Exit 1.
  stderr-Rauschen (``Error in g_unlink()``) kommt auch bei Erfolg.

Umgebungsvariablen für Quirks: ``FAKESIRIL_FAIL_REGISTER=1`` → zu wenige Sternpaare,
``FAKESIRIL_NO_OUTPUT=1`` → Exit 0 ohne Ergebnisdatei.
"""
from __future__ import annotations

import glob
import os
import shlex
import sys

import numpy as np

VERSION = "siril 1.4.4"


def _seq_files(seq: str) -> list[str]:
    """Sequenz „name_“ = Dateien name_00001.fit … im CWD, sortiert."""
    base = seq.rstrip("_")
    return sorted(f for f in glob.glob(base + "_*.fit") if f[len(base) + 1:len(base) + 6].isdigit())


def _bad_frames() -> set[int]:
    """FAKESIRIL_BAD_FRAMES="2,5": 1-basierte Frames, die Siril nicht registriert (Sequenz-Zeilen wie echt)."""
    return {int(x) for x in os.environ.get("FAKESIRIL_BAD_FRAMES", "").split(",") if x.strip()}


def _write_seq(seq: str, n: int, ref: int = 0) -> None:
    """Sequenzdatei „<seq>.seq“ (seq mit Unterstrich, z. B. pp_light_) nach dem Muster der echten Siril-1.4-Ausgabe."""
    bad = _bad_frames()
    L = ["#Siril sequence file (fake)", "S '%s' 1 %d %d 5 %d 6 0 0 0" % (seq, n, n - len(bad), ref), "L %d" % (ref + 1)]
    L += ["I %d %d" % (i, 0 if i in bad else 1) for i in range(1, n + 1)]
    for i in range(1, n + 1):
        if i in bad:
            L.append("R0 0 0 0 0 0 0 H 0 0 0 0 0 0 0 0 0")
        else:
            L.append("R0 4.6 4.6 0.77 0 0.02 38 H 1 0 0 0 1 0 0 0 1")
    open(seq + ".seq", "w", encoding="utf-8").write("\n".join(L) + "\n")


def _selected(seq: str) -> set[int]:
    """1-basierte Positionen der ausgewählten Frames (I-Zeilen); ohne Sequenzdatei alle."""
    p = seq + ".seq"
    if not os.path.exists(p):
        return set()
    return {int(ln.split()[1]) for ln in open(p, encoding="utf-8") if ln.startswith("I ") and ln.split()[2] == "1"}


_REF: dict[str, int] = {}       # setref-Position (1-basiert) je Sequenz – nur für diesen Lauf


def _set_selection(seq: str, a: int, b: int, value: int) -> None:
    """unselect/select: I-Zeilen a..b (1-basiert) auf value setzen."""
    p = seq + ".seq"
    lines = open(p, encoding="utf-8").read().splitlines()
    out = []
    for ln in lines:
        f = ln.split()
        if f and f[0] == "I" and a <= int(f[1]) <= b:
            ln = "I %s %d" % (f[1], value)
        out.append(ln)
    open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")


def _opt(args: list[str], key: str, default=None):
    return next((a.split("=", 1)[1] for a in args if a.startswith(key + "=")), default)


def _fail(lineno: int, cmd: str, msg: str) -> int:
    print("log: Error in line %d ('%s'): %s" % (lineno, cmd, msg), flush=True)
    print("log: Script execution failed.", flush=True)
    print("progress: Script execution failed., 100.00%", flush=True)
    return 1


def _read(path: str):
    from astropy.io import fits
    with fits.open(path) as h:
        return np.asarray(h[0].data, dtype=np.float32), h[0].header.copy()


def _xisf(path: str):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from astrostack import xisf
    return xisf.read_image(path)


def _write(path: str, data, header=None) -> None:
    from astropy.io import fits
    hdr = fits.Header()
    for k, v in (header.items() if isinstance(header, dict) else (header or fits.Header()).items()):
        hdr[k] = v
    fits.PrimaryHDU(data=np.asarray(data, dtype=np.float32), header=hdr).writeto(path, overwrite=True)


def run_script(workdir: str, script: str) -> int:
    os.chdir(workdir)
    lines = [ln.strip() for ln in open(script, encoding="utf-8") if ln.strip()]
    print("Error in g_unlink()", file=sys.stderr, flush=True)     # harmloses Rauschen wie bei Siril
    total = len(lines)
    for i, line in enumerate(lines, 1):
        cmd, *args = shlex.split(line)
        print("log: Running command: %s" % cmd, flush=True)
        if cmd in ("requires", "setext", "setfindstar", "set"):
            pass
        elif cmd == "cd":
            os.chdir(args[0])
        elif cmd == "convert":
            files = sorted(glob.glob(args[0] + "_*.fit") + glob.glob(args[0] + "_*.xisf"))
            if not files:
                return _fail(i, cmd, "no image found")
            out = _opt(args, "-out")
            os.makedirs(out, exist_ok=True)
            for f in files:
                if f.endswith(".xisf"):                        # Siril liest XISF und schreibt FITS (setext fit)
                    _write(os.path.join(out, os.path.basename(f)[:-5] + ".fit"), _xisf(f), {})
                else:
                    os.symlink(os.path.abspath(f), os.path.join(out, os.path.basename(f)))
        elif cmd == "calibrate":
            seq = args[0]
            debayer = "-debayer" in args
            pre = _opt(args, "-prefix", "pp_")
            for f in _seq_files(seq):
                data, hdr = _read(f)
                if debayer:
                    data = np.stack([data, data, data])
                _write(pre + os.path.basename(f), data, hdr)
        elif cmd == "register":
            files = _seq_files(args[0])
            if os.environ.get("FAKESIRIL_FAIL_REGISTER") == "1" or len(files) < 2:
                return _fail(i, cmd, "not enough star pairs")
            _write_seq(args[0], len(files), _REF.get(args[0], 1) - 1)
            print("log: Registration of %d images done" % len(files), flush=True)
        elif cmd == "seqapplyreg":
            pre = _opt(args, "-prefix", "r_")
            for f in _seq_files(args[0]):
                data, hdr = _read(f)
                _write(pre + os.path.basename(f), data, hdr)
            _write_seq(pre + args[0], len(_seq_files(args[0])), _REF.get(args[0], 1) - 1)
        elif cmd == "setref":
            _REF[args[0]] = int(args[1])
        elif cmd == "unselect":
            _set_selection(args[0], int(args[1]), int(args[2]), 0)
        elif cmd == "merge":                                   # Sequenzen hintereinander → eine neue Sequenz
            out, frames = args[-1], []
            for src in args[:-1]:
                frames += _seq_files(src)
            for k, f in enumerate(frames, 1):
                data, hdr = _read(f)
                _write("%s_%05d.fit" % (out, k), data, hdr)
            _write_seq(out + "_", len(frames))
        elif cmd == "stack":
            weights = [a.split("=", 1)[1] for a in args if a.startswith("-weight=")]
            if any(w not in ("wfwhm", "nbstars", "noise") for w in weights):   # Siril 1.4.4 kennt nur diese
                return _fail(i, cmd, "Unknown argument to -weight=%s, aborting." % weights[0])
            sel = _selected(args[0])
            files = [f for i, f in enumerate(_seq_files(args[0]), 1) if not sel or i in sel]
            if not files:
                return _fail(i, cmd, "sequence not found")
            shapes = {_read(f)[0].shape for f in files}
            if len(shapes) > 1:
                return _fail(i, cmd, "images have different dimensions")
            if os.environ.get("FAKESIRIL_NO_OUTPUT") != "1":
                mean = np.mean([_read(f)[0] for f in files], axis=0)
                _write(_opt(args, "-out", "result") + ".fit", mean, {"STACKCNT": len(files), "IMAGETYP": "Master Light"})
                if "-rejmaps" in args:
                    for stem in ("low", "high"):
                        _write("%s_%s_rejmap.fit" % (_opt(args, "-out", "result"), stem), np.zeros_like(mean), {})
        else:
            return _fail(i, cmd, "unknown command name")
        for k in range(int(os.environ.get("FAKESIRIL_CHATTY", "0"))):   # viele Ausgabezeilen wie bei großen Stapeln
            print("log: chatty output %d for %s" % (k, cmd), flush=True)
        print("progress: %s, %.2f%%" % (cmd, 100.0 * i / total), flush=True)
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
