"""Ordner-Dialog: nativer Linux-Weg (zenity/kdialog) vor dem Tk-Hilfsprozess."""
import os
import subprocess

import pytest

from astrostack.app import server, window


def _result(cmd, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)


def _linux(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(window, "pick_folder", lambda initial="": False)


def _tools(monkeypatch, paths):
    monkeypatch.setattr(server.shutil, "which", lambda name: paths.get(name))


def _run(monkeypatch, fn):
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return fn(cmd, kw)

    monkeypatch.setattr(server.subprocess, "run", fake)
    return calls


def test_zenity_returns_selected_folder_without_tk(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {"zenity": "/usr/bin/zenity"})
    start = tmp_path / "Bilder für Ümlaut"
    start.mkdir()
    chosen = str(start / "Nacht ümlaut")
    calls = _run(monkeypatch, lambda cmd, kw: _result(cmd, 0, chosen + "\n"))

    out = server.pick_folder(str(start))

    assert out == os.path.normpath(chosen)
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd == ["zenity", "--file-selection", "--directory", "--title=Ordner wählen",
                   "--filename=%s" % os.path.join(str(start), "")]
    assert kw.get("shell") in (None, False)
    assert kw.get("timeout") == 900
    assert kw.get("encoding") == "utf-8"


def test_initial_dir_prefers_existing_parent_and_home(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {"zenity": "/usr/bin/zenity"})
    existing = tmp_path / "mit leerzeichen"
    existing.mkdir()
    calls = _run(monkeypatch, lambda cmd, kw: _result(cmd, 1))
    server.pick_folder(str(existing / "fehlt" / "tiefer"))
    assert calls[0][0][-1] == "--filename=%s" % os.path.join(str(existing), "")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert server._linux_initial_dir("") == str(home)
    assert server._linux_initial_dir("   ") == str(home)


def test_cancel_returns_none_and_opens_no_second_dialog(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {"zenity": "/usr/bin/zenity", "kdialog": "/usr/bin/kdialog"})
    calls = _run(monkeypatch, lambda cmd, kw: _result(cmd, 1, "", "cancel"))

    assert server.pick_folder(str(tmp_path)) is None
    assert len(calls) == 1, "Cancel darf keinen Ausweichdialog öffnen"


def test_error_and_timeout_raise_clear_runtime_error(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {"zenity": "/usr/bin/zenity"})
    _run(monkeypatch, lambda cmd, kw: _result(cmd, 2, "", "cannot open display"))
    with pytest.raises(RuntimeError, match="nicht gestartet") as err:
        server.pick_folder(str(tmp_path))
    assert "cannot open display" in str(err.value)

    def timeout(cmd, kw):
        raise subprocess.TimeoutExpired(cmd, 900)

    calls = _run(monkeypatch, timeout)
    with pytest.raises(RuntimeError, match="nicht gestartet|Zeit"):
        server.pick_folder(str(tmp_path))
    assert len(calls) == 1, "Timeout darf nicht in den Tk-Weg fallen"


def test_kdialog_is_used_when_zenity_is_missing(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {"kdialog": "/usr/bin/kdialog"})
    chosen = str(tmp_path / "Ziel mit Ümlaut")
    calls = _run(monkeypatch, lambda cmd, kw: _result(cmd, 0, chosen))

    assert server.pick_folder(str(tmp_path)) == os.path.normpath(chosen)
    assert calls[0][0] == ["kdialog", "--getexistingdirectory", str(tmp_path), "--title", "Ordner wählen"]


def test_without_native_helper_tk_path_still_works(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {})
    chosen = str(tmp_path / "Tk Auswahl")
    calls = _run(monkeypatch, lambda cmd, kw: _result(cmd, 0, chosen))

    assert server.pick_folder(str(tmp_path)) == os.path.normpath(chosen)
    assert "_pick-folder" in calls[0][0]


def test_tk_failure_on_linux_mentions_zenity_or_kdialog(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {})
    _run(monkeypatch, lambda cmd, kw: _result(cmd, 1, "", "no module named tkinter"))

    with pytest.raises(RuntimeError, match="zenity oder kdialog installieren"):
        server.pick_folder(str(tmp_path))


def test_webview_dialog_has_priority(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tools(monkeypatch, {"zenity": "/usr/bin/zenity"})
    monkeypatch.setattr(window, "pick_folder", lambda initial="": str(tmp_path / "Webview"))

    def no_native(cmd, kw):
        raise AssertionError("native Dialog darf bei vorhandenem webview nicht starten")

    _run(monkeypatch, no_native)
    assert server.pick_folder(str(tmp_path)) == os.path.normpath(str(tmp_path / "Webview"))
