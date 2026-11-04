"""Auto-Update: Versionsvergleich, Prüfung, Austausch-Skript."""
import os

from astrostack import __version__, updater


def _rel(tag, size=10):
    return {"tag_name": tag, "body": "Neu: alles besser", "html_url": "https://github.com/butu/b2stacker/releases/tag/" + tag,
            "assets": [{"name": "b2stacker.exe", "size": size,
                        "browser_download_url": "https://github.com/butu/b2stacker/releases/download/%s/b2stacker.exe" % tag}]}


def test_version_compare():
    assert updater.is_newer("v0.5.10", "0.5.9")
    assert updater.is_newer("1.0.0", "0.9.9")
    assert not updater.is_newer("v0.5.2", "0.5.2")
    assert not updater.is_newer("0.4.9", "0.5.0")


def test_check_newer_and_offline():
    info = updater.check(force=True, fetch=lambda url: _rel("v99.0.0"))
    assert info["newer"] and info["latest"] == "99.0.0" and info["url"].endswith("b2stacker.exe")
    info = updater.check(force=True, fetch=lambda url: _rel("v" + __version__))
    assert not info["newer"]

    def boom(url):
        raise OSError("offline")
    info = updater.check(force=True, fetch=boom)
    assert not info["newer"] and "offline" in info["error"]


def test_install_refused_from_source(monkeypatch):
    import pytest
    updater.check(force=True, fetch=lambda url: _rel("v99.0.0"))
    with pytest.raises(RuntimeError, match="Quellcode"):
        updater.start_install(lambda: None)


def test_install_swaps_via_script(tmp_path, monkeypatch):
    exe = tmp_path / "b2stacker (13).exe"
    exe.write_bytes(b"MZold")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(updater, "exe_path", lambda: str(exe))
    import sys
    from astrostack import install
    # Windows: immer an den festen Ort; sonst neben die laufende Datei
    target = install.install_exe() if sys.platform == "win32" else str(exe)
    new = os.path.splitext(target)[0] + ".new.exe"
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    launched, quit_called = [], []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: launched.append(args))
    updater.check(force=True, fetch=lambda url: _rel("v99.0.0", size=5))

    def fake_dl(url, dst, size, progress):
        open(dst, "wb").write(b"MZnew")
        progress(5, 5)
    with updater._LOCK:
        updater._STATE["dl"] = None
    updater.start_install(lambda: quit_called.append(1), downloader=fake_dl)
    import time
    for _ in range(50):
        if quit_called:
            break
        time.sleep(0.05)
    assert quit_called and launched
    cmd = launched[0][-1]
    text = open(cmd, encoding="utf-8").read()
    assert target in text and new in text and "B2_UPDATED_FROM" in text
    assert str(exe) in text                                            # Rückfall: alte Datei wieder starten
    assert open(new, "rb").read() == b"MZnew"
    assert updater.status()["download"]["state"] == "restart"


def test_busy_blocks_install(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setattr(updater, "exe_path", lambda: str(tmp_path / "b2stacker.exe"))
    with updater._LOCK:
        updater._STATE["dl"] = None
    updater.check(force=True, fetch=lambda url: _rel("v99.0.0"))
    with pytest.raises(RuntimeError, match="Auftrag"):
        updater.start_install(lambda: None, busy_fn=lambda: True)
