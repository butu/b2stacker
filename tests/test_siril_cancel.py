"""Abbruch: siril-cli und ihre Kindprozesse (z. B. Flatpak-Sandbox) müssen wirklich enden."""
import os
import sys
import time

import pytest

from astrostack.engines import siril

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Prozessgruppen nur unter POSIX")


def _alive(pid: int) -> bool:
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().split(") ")[-1].split()[0] != "Z"       # Zombie gilt als beendet
    except OSError:
        return False


@posix_only
def test_kill_ends_child_processes_too(tmp_path):
    pidfile = tmp_path / "child.pid"
    wrapper = tmp_path / "siril-cli"
    # Hauptprozess startet ein Kind (wie flatpak/bwrap) und wartet darauf
    wrapper.write_text('#!/bin/sh\nsleep 300 &\necho $! > "%s"\nwait\n' % pidfile)
    wrapper.chmod(0o755)
    inst = siril.Install(cli=(str(wrapper),), gui=None, label="test")
    runner = siril.SirilRunner(inst, str(tmp_path / "s.ssf"), str(tmp_path), [str(tmp_path)])
    runner.start()
    deadline = time.time() + 10
    while not (pidfile.exists() and pidfile.read_text().strip()) and time.time() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text().strip())
    assert _alive(child)
    runner.kill()
    runner.finish()
    deadline = time.time() + 5
    while _alive(child) and time.time() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "Kindprozess läuft nach dem Abbruch weiter"
    assert runner.poll() is not None


@posix_only
def test_kill_after_exit_is_harmless(tmp_path):
    wrapper = tmp_path / "siril-cli"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    runner = siril.SirilRunner(siril.Install(cli=(str(wrapper),), gui=None, label="t"), "s.ssf", str(tmp_path),
                               [str(tmp_path)])
    runner.start()
    runner.proc.wait(timeout=10)
    runner.kill()                                  # darf nicht werfen
    assert runner.poll() == 0
