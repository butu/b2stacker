"""Cancel: siril-cli and its child processes (e.g. Flatpak sandbox) must really end."""
import os
import sys
import time

import pytest

from astrostack.engines import siril
from .fake_siril import write_wrapper

# Child-process kill is checked via sleep and /proc; Windows uses taskkill /T (not covered here).
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="sleep child and /proc check are POSIX-only")


def _alive(pid: int) -> bool:
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().split(") ")[-1].split()[0] != "Z"       # zombie counts as ended
    except OSError:
        return False


@posix_only
def test_kill_ends_child_processes_too(tmp_path):
    pidfile = tmp_path / "child.pid"
    wrapper = tmp_path / "siril-cli"
    # main process starts a child (like flatpak/bwrap) and waits for it
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


def test_kill_after_exit_is_harmless(tmp_path):
    wrapper = write_wrapper(tmp_path / "siril-cli", "exit 0", "exit /b 0")
    runner = siril.SirilRunner(siril.Install(cli=(wrapper,), gui=None, label="t"), "s.ssf", str(tmp_path),
                               [str(tmp_path)])
    runner.start()
    runner.proc.wait(timeout=10)
    runner.kill()                                  # must not raise
    assert runner.poll() == 0
