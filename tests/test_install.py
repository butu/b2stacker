"""Fester Installationsort, Update an diesen Ort, Claude-Eintrag auf die installierte Datei."""
import json
import os
import sys

import pytest

from astrostack import claude_link, install, updater


@pytest.fixture
def fake_win(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    cfg = tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    monkeypatch.setenv("B2_CLAUDE_CONFIG", str(cfg))
    dl = tmp_path / "Downloads" / "b2stacker (13).exe"
    dl.parent.mkdir()
    dl.write_bytes(b"MZ neue exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(dl))
    monkeypatch.setattr(install, "_shortcut", lambda target: True)
    monkeypatch.setenv("ASTROSTACK_SETTINGS", str(tmp_path / "s.json"))
    return tmp_path, dl, cfg


def test_status_offers_install_when_running_from_downloads(fake_win, monkeypatch):
    tmp, dl, _ = fake_win
    monkeypatch.setattr(sys, "platform", "win32")
    st = install.status()
    assert st["offer"] and not st["installed"] and st["target"].endswith(os.path.join("b2stacker", "b2stacker.exe"))
    assert st["target"].startswith(str(tmp / "Local"))


def test_install_copies_starts_and_repoints_claude(fake_win):
    tmp, dl, cfg = fake_win
    cfg.write_text(json.dumps({"mcpServers": {"andere": {"command": "x"},
                                              "b2stacker": {"command": str(dl.parent / "b2stacker (12).exe"),
                                                            "args": ["mcp"]}}}))
    started, quit_ = [], []
    res = install.install(start=started.append, quit_fn=lambda: quit_.append(1))
    dst = install.install_exe()
    assert res["changed"] and open(dst, "rb").read() == b"MZ neue exe"
    assert started == [dst] and quit_ == [1]
    d = json.loads(cfg.read_text())
    e = d["mcpServers"]["b2stacker"]
    assert e["command"] == dst and e["args"] == ["mcp"] and d["mcpServers"]["andere"]
    assert "gelöscht werden" in res["text"]


def test_install_without_claude_entry_leaves_config(fake_win):
    tmp, dl, cfg = fake_win
    cfg.write_text(json.dumps({"mcpServers": {"andere": {"command": "x"}}}))
    install.install(start=lambda p: None)
    assert "b2stacker" not in json.loads(cfg.read_text())["mcpServers"]


def test_installed_instance_is_not_reinstalled(fake_win, monkeypatch):
    tmp, dl, cfg = fake_win
    os.makedirs(install.install_dir())
    open(install.install_exe(), "wb").write(b"MZ")
    monkeypatch.setattr(sys, "executable", install.install_exe())
    assert install.is_installed()
    res = install.install(start=lambda p: pytest.fail("darf nicht starten"))
    assert res["changed"] is False


def test_update_script_targets_fixed_location(fake_win, monkeypatch):
    tmp, dl, cfg = fake_win
    text = updater.SWAP_CMD.format(exe=install.install_exe(), new=install.install_exe()[:-4] + ".new.exe",
                                   old=install.install_exe()[:-4] + ".old.exe", pid=1, ppid=1, cur="0.10.2",
                                   new_version="0.11.0", cur_exe=str(dl), old_alt="X.old-123.exe",
                                   failfile="F.txt")
    assert "wird auf Version 0.11.0 aktualisiert" in text
    # alte exe noch von einem Prozess belegt (z. B. MCP-Server der Vorversion) → ausweichen statt scheitern
    assert 'if exist "%OLD%" set "OLD=X.old-123.exe"' in text
    assert '>"F.txt" echo %WHY%' in text
    assert 'if not exist "%EXE%" goto place' in text                 # erste Installation über das Update
    assert 'start "" "%CUR%"' in text and str(dl) in text            # Fehler: alte Version startet wieder
    assert text.count("(") == text.count(")")
    assert updater.SWAP_CMD.isascii()                                # cmd.exe liest OEM-Codepage: nur ASCII


def test_claude_link_command_override(tmp_path, monkeypatch):
    assert claude_link.server_entry("C:/x/b2stacker.exe") == {"command": "C:/x/b2stacker.exe", "args": ["mcp"]}


def test_cleanup_removes_all_old_exes_and_reports_failed_swap(tmp_path, monkeypatch):
    exe = tmp_path / "b2stacker.exe"
    exe.write_bytes(b"MZ")
    for n in ("b2stacker.old.exe", "b2stacker.old-0.11.0.exe", "b2stacker.old-17.exe"):
        (tmp_path / n).write_bytes(b"MZ")
    monkeypatch.setattr(updater, "exe_path", lambda: str(exe))
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "b2stacker_update_failed.txt").write_text("b2stacker.exe liess sich nicht umbenennen\n")
    monkeypatch.delenv("B2_UPDATED_FROM", raising=False)
    updater.cleanup_after_update()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b2stacker.exe"]
    assert updater.status()["update_failed"] == "b2stacker.exe liess sich nicht umbenennen"
    updater._STATE.pop("failed", None)
