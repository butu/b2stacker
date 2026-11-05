"""Neue Oberfläche, Teil 2 (Server + Core): Stack-Qualität und Vorschau, Vergleich, Galerie, Watcher, Verlauf,
Datenbank-Sicherung mit Wiederherstellen, MCP auf Core, Kommandozeile ``neu``."""
import io
import json
import os
import time

import numpy as np
import pytest
from scipy import ndimage as ndi

from astrostack import cli, core, gallery, history, stackview, xisf
from astrostack import db as dbm
from astrostack.watcher import Watcher, signature

from .test_core import c  # noqa: F401
from .test_flow import env  # noqa: F401
from .test_web import TOKEN, _serve, _settings, _stop, call


# ---------------------------------------------------------------- synthetische Masters

def star_image(seed=0, shift=(0.0, 0.0), noise=0.002, fwhm=2.2, shape=(240, 320), channels=3, nstars=180):
    """Sternfeld (C, H, W) in [0,1] mit Nebel, Versatz und Rauschen – wie ein kleines Light-Master."""
    rng = np.random.default_rng(1234)                       # gleiche Sterne für alle Stacks
    H, W = shape
    yy, xx = np.mgrid[0:H, 0:W]
    img = np.full(shape, 0.04, np.float32)
    s = fwhm / 2.355
    for y, x, a in zip(rng.uniform(12, H - 12, nstars), rng.uniform(12, W - 12, nstars), rng.uniform(.03, .6, nstars)):
        img += a * np.exp(-((yy - y - shift[0]) ** 2 + (xx - x - shift[1]) ** 2) / (2 * s * s))
    img += 0.08 * np.exp(-((yy - H * .45 - shift[0]) ** 2 + (xx - W * .55 - shift[1]) ** 2) / (2 * (H / 5) ** 2))
    n = np.random.default_rng(seed).normal(0, noise, (channels,) + shape).astype(np.float32)
    out = np.clip(img[None] * np.array([1.0, 0.9, 1.1][:channels], np.float32)[:, None, None] + n, 0, 1)
    return out.astype(np.float32)


def real_masters(runner, images):
    """Stack-Runner, der die Light-Masters des nachgebauten PixInsight durch echte XISF-Bilder ersetzt.
    ``images``: Liste der Bilder, je Aufruf das nächste."""
    calls = []

    def run(*a, **kw):
        res = runner(*a, **kw)
        img = images[min(len(calls), len(images) - 1)]
        calls.append(1)
        for m in res.get("masters") or []:
            if os.path.basename(m).lower().startswith("masterlight"):
                xisf.write_image(m, img)
        return res
    return run


@pytest.fixture
def stacked2(c, tmp_path, monkeypatch):  # noqa: F811
    """Projekt mit zwei fertigen Stacks (echte XISF-Masters: B ist verschoben, rauschärmer, schärfer)."""
    co, data = c
    co.stack_runner = real_masters(co.stack_runner, [star_image(1, noise=0.004, fwhm=2.8),
                                                     star_image(2, shift=(6.0, -9.0), noise=0.002, fwhm=2.2)])
    r = co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    plan = co.plan(r["id"])
    ha = next(x["id"] for x in plan["runs"] if x["filter"] == "HaO3")
    for _ in range(2):
        t = co.stack(r["id"], [ha])
        assert co.tasks.wait_idle(60)
        assert co.tasks.get(t["id"]).state == "done", co.tasks.get(t["id"]).error
    return co, r["id"], data


# ---------------------------------------------------------------- Stacks: Qualität, Vorschau, Vergleich, Löschen

def test_quality_and_preview_after_stack(stacked2):
    co, pid, _ = stacked2
    st = co.stacks(pid)
    assert len(st) == 2 and all(s["status"] == "done" for s in st)
    q = st[0]["quality"]
    assert q["version"] == stackview.QUALITY_VERSION and not q.get("error"), q
    assert q["stars"] > 50 and 1.5 < q["fwhm_px"] < 4 and q["noise"] > 0 and q["ecc"] is not None
    assert st[0]["preview_url"].startswith("/api/stacks/%d/preview?v=" % st[0]["id"])
    assert st[0]["size_bytes"] > 1000 and st[0]["file_info"][0]["exists"] and not st[0]["missing"]
    assert os.path.isfile(stackview.preview_path(co.ws, q))
    # Kachel der Startseite: neueste Stack-Vorschau, solange kein Projektbild gewählt ist
    assert co.projects()[0]["cover_url"] == st[0]["preview_url"]
    # Stack als Projektbild
    co.set_cover(pid, st[1]["master_asset_id"])
    assert co.projects()[0]["cover_url"] == st[1]["preview_url"]
    assert co.stacks(pid)[1]["cover"] is True


def test_quality_is_computed_lazily_for_old_stacks(stacked2):
    co, pid, _ = stacked2
    co.db.execute("UPDATE stacks SET quality_json = NULL")
    st = co.stacks(pid)
    assert st[0]["quality"] is None
    assert co.tasks.wait_idle(30)
    st = co.stacks(pid)
    assert all(s["quality"] and s["quality"]["stars"] > 50 for s in st)
    # unlesbares Master: Fehler als Klartext, keine Endlosschleife
    path = stackview.light_master(co.db, st[0]["id"])
    with open(path, "wb") as fh:
        fh.write(b"kaputt")
    co.db.execute("UPDATE stacks SET quality_json = NULL WHERE id = ?", (st[0]["id"],))
    co.stacks(pid)
    assert co.tasks.wait_idle(30)
    q = co.stacks(pid)[0]["quality"]
    assert "unlesbar" in q["error"]
    before = len(co.tasks.snapshot()["finished"])
    co.stacks(pid)
    assert co.tasks.wait_idle(5) and len(co.tasks.snapshot()["finished"]) == before


def test_phase_correlation_finds_shift():
    a = star_image(1, shift=(0, 0))
    b = star_image(2, shift=(7.3, -4.6))
    dy, dx, conf = stackview.phase_shift(a, b)
    assert abs(dy + 7.3) < 0.4 and abs(dx - 4.6) < 0.4 and conf > 8
    # B auf A ausrichten → Restversatz klein
    b2 = np.stack([ndi.shift(ch, (dy, dx), order=1) for ch in b])
    dy2, dx2, _ = stackview.phase_shift(a, b2)
    assert abs(dy2) < 0.3 and abs(dx2) < 0.3


def test_compare_two_stacks(stacked2):
    co, pid, _ = stacked2
    new, old = co.stacks(pid)
    r = co.compare(pid, old["id"], new["id"])
    assert r["align"]["aligned"] and abs(r["align"]["dy"] + 6) < 1 and abs(r["align"]["dx"] - 9) < 1
    m = {x["key"]: x for x in r["metrics"]}
    assert m["noise"]["better"] == "b" and m["noise"]["a"] > m["noise"]["b"]          # B rauschärmer
    assert m["fwhm"]["better"] == "b"                                                   # B schärfer
    assert m["stars"]["rule"] == "high" and r["a"]["note"] == "" and r["b"]["hours"] > 0
    from PIL import Image
    pa, _ = co.compare_image(pid, old["id"], new["id"], "a")
    pb, _ = co.compare_image(pid, old["id"], new["id"], "b")
    with Image.open(pa) as ia, Image.open(pb) as ib:
        assert ia.size == ib.size and 300 <= ia.size[0] <= 320 and 228 <= ia.size[1] <= 240
    with pytest.raises(ValueError):
        co.compare(pid, old["id"], old["id"])


def test_delete_stack_keeps_foreign_files_and_drops_previews(stacked2):
    co, pid, _ = stacked2
    st = co.stacks(pid)[0]
    sdir = os.path.join(co.ws.project_dir(co.project(pid)["folder"]), st["folder"])
    foreign = os.path.join(sdir, "bearbeitet.jpg")
    _jpeg(foreign)
    co.add_images(pid, [foreign])
    assert co.gallery(pid)[0]["stack_id"] == st["id"]
    prev = stackview.preview_path(co.ws, st["quality"])
    removed = co.delete_stack(pid, st["id"])
    assert removed and not os.path.exists(st["files"][0]) and not os.path.exists(prev)
    assert os.path.isfile(foreign) and os.path.isdir(sdir)                 # fremde Datei und Ordner bleiben
    g = co.gallery(pid)
    assert g[0]["stack_id"] is None and not g[0]["missing"]
    assert [s["id"] for s in co.stacks(pid)] != [st["id"]]


# ---------------------------------------------------------------- Galerie

def _jpeg(path, size=(64, 48), color=(200, 40, 40), fmt="JPEG", mode="RGB"):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new(mode, size, color if mode == "RGB" else 30000).save(path, fmt)
    return path


def test_gallery_add_files_and_folders_cover_missing_hide(c, tmp_path):  # noqa: F811
    co, data = c
    pid = co.create_project("Galerie", [])["id"]
    assert co.tasks.wait_idle(30)
    ext = tmp_path / "Bearbeitet"
    _jpeg(str(ext / "a.jpg"))
    _jpeg(str(ext / "Sub" / "b.png"), fmt="PNG")
    _jpeg(str(ext / "c16.tif"), mode="I;16", fmt="TIFF")
    xisf.write_image(str(ext / "d.xisf"), star_image(3, shape=(60, 80)))
    (ext / "e.psd").write_bytes(b"8BPS kaputt")
    (ext / "notiz.txt").write_text("x")
    single = _jpeg(str(tmp_path / "einzeln.jpg"))
    r = co.add_images(pid, [str(ext), single])
    assert r["new"] == 6 and r["missing"] == []
    g = {a["name"]: a for a in co.gallery(pid)}
    assert set(g) == {"a.jpg", "b.png", "c16.tif", "d.xisf", "e.psd", "einzeln.jpg"}
    for n in ("a.jpg", "b.png", "c16.tif", "d.xisf"):
        p, v = co.asset_thumb(g[n]["id"])
        assert os.path.isfile(p) and v == g[n]["version"]
    with pytest.raises(Exception):
        co.asset_thumb(g["e.psd"]["id"])                       # kaputtes PSD: Platzhalter in der Oberfläche
    # Bild-Infos: Maße, Bittiefe, Header/EXIF, Messwerte (zwischengespeichert)
    d = co.asset_info(g["d.xisf"]["id"])
    assert (d["width"], d["height"], d["format"], d["float"]) == (80, 60, "XISF", True)
    assert d["measured"] and d["measured"]["stars"] >= 1
    assert co.asset_info(g["d.xisf"]["id"]) == d
    t = co.asset_info(g["c16.tif"]["id"])
    assert t["bits"] == 16 and t["channels"] == 1
    j = co.asset_info(g["a.jpg"]["id"])
    assert j["bits"] == 8 and j["format"] == "JPEG"
    with pytest.raises(Exception):
        co.asset_info(g["e.psd"]["id"])
    assert co.add_images(pid, [single])["known"] == 1          # doppelt: nichts Neues
    with pytest.raises(ValueError, match="unterstützt"):
        co.add_images(pid, [str(ext / "notiz.txt")])
    # Projektbild
    co.set_cover(pid, g["a.jpg"]["id"])
    tile = [p for p in co.projects() if p["id"] == pid][0]
    assert tile["cover_url"] == g["a.jpg"]["thumb_url"]
    # Datei fehlt
    os.remove(single)
    gallery.check_missing(co.db, pid)
    assert {a["name"]: a for a in co.gallery(pid)}["einzeln.jpg"]["missing"]
    # aus der Galerie entfernen: die Datei bleibt, Projektbild fällt zurück
    co.hide_image(pid, g["a.jpg"]["id"])
    assert "a.jpg" not in {a["name"] for a in co.gallery(pid)} and os.path.isfile(ext / "a.jpg")
    assert [p for p in co.projects() if p["id"] == pid][0]["cover_url"] is None
    assert co.add_images(pid, [str(ext / "a.jpg")])["back"] == 1     # bewusst wieder hinzufügen
    kinds = [h["kind"] for h in co.timeline(pid)]
    assert "assets_added" in kinds and "cover_set" in kinds


def test_rename_project_moves_stored_paths(stacked2):
    co, pid, _ = stacked2
    st = co.stacks(pid)[0]
    co.rename(pid, "Iris")
    st2 = co.stacks(pid)[0]
    assert st2["files"][0] != st["files"][0] and os.path.isfile(st2["files"][0]) and "Iris" in st2["files"][0]
    assert os.path.isfile(stackview.light_master(co.db, st2["id"]))


# ---------------------------------------------------------------- Watcher

def test_signature_and_offline(tmp_path):
    d = tmp_path / "w"
    (d / "a").mkdir(parents=True)
    s1 = signature(str(d))
    assert s1 and signature(str(tmp_path / "fehlt")) is None
    time.sleep(0.02)
    (d / "a" / "x.fits").write_bytes(b"1")
    assert signature(str(d)) != s1


def test_watcher_refreshes_sources_and_finds_gallery_images(c, tmp_path):  # noqa: F811
    co, data = c
    from .conftest import NINA_LIGHT, make_fits
    from .test_flow import _stars
    pid = co.create_project("vdB 131", [data, str(tmp_path / "offline")])["id"]
    assert co.tasks.wait_idle(60)
    co.db.execute("UPDATE events SET at = '2026-10-20T08:00:00Z'")   # erstes Einlesen liegt länger zurück
    w = Watcher(lambda: co, 0.1)
    assert w.tick() == []                                     # erster Durchgang: nur merken
    lights = co.project(pid)["counts"]["light"]
    # neue Lights in der Quelle
    for i in range(2):
        make_fits(os.path.join(data, "vdB 131", "2026-10-13", "HaO3", "L_1%03d.fits" % i),
                  dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-13T21:%02d:00" % i}), data=_stars(i))
    # ein Bild im Projektordner
    pdir = co.ws.project_dir(co.project(pid)["folder"])
    _jpeg(os.path.join(pdir, "Endbild.jpg"))
    assert w.tick() == []                                     # entprellt: noch in Bewegung?
    done = w.tick()
    assert ("refresh", pid) in done and ("gallery", pid) in done
    assert co.tasks.wait_idle(60)
    assert co.project(pid)["counts"]["light"] == lights + 2
    assert [a["name"] for a in co.gallery(pid)] == ["Endbild.jpg"]
    assert w.tick() == [] and w.tick() == []                  # nichts geändert → nichts
    tl = co.timeline(pid)
    new = [i for i in tl if i["kind"] == "files_discovered"][0]
    night = co.db.value("SELECT night FROM files WHERE path LIKE '%L_1000.fits'")
    assert new["title"] == "2 neue Lights · HaO3 · 0,3 h", new
    assert new["detail"] == "Nacht vom " + history._day(night) and new["night"] == night


def test_watcher_thread_starts_and_stops(c):  # noqa: F811
    co, data = c
    co.create_project("A", [data])
    assert co.tasks.wait_idle(60)
    w = Watcher(lambda: co, 0.05).start()
    t0 = time.monotonic()
    while w.ticks < 3 and time.monotonic() - t0 < 10:
        time.sleep(0.02)
    w.stop()
    assert w.ticks >= 3 and not w.running


def test_watcher_skips_unreachable_source_quickly(c, tmp_path, monkeypatch):  # noqa: F811
    co, data = c
    import astrostack.watcher as wm
    pid = co.create_project("B", [str(tmp_path / "nas")])["id"]
    assert co.tasks.wait_idle(30)
    real = wm.signature

    def slow(path, skip=None):
        if path.endswith("nas"):
            time.sleep(1.0)
        return real(path, skip)
    monkeypatch.setattr(wm, "signature", slow)
    monkeypatch.setattr(wm, "PROBE_TIMEOUT", 0.1)
    w = Watcher(lambda: co, 1)
    t0 = time.monotonic()
    w.tick()
    w.tick()
    assert time.monotonic() - t0 < 1.5 and w._skip.get(str(tmp_path / "nas"), 0) > 0
    w.stop()
    assert pid


def test_server_starts_and_stops_watcher(c, tmp_path, monkeypatch):  # noqa: F811
    co, data = c
    from astrostack.web import server as web
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = web.create(0, settings=s, core=co, refresh=False, watch=True, watch_interval=0.05)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert srv.watcher.running
    finally:
        srv.stop()
    assert not srv.watcher.running


# ---------------------------------------------------------------- Verlauf

def _ev(db, at, kind, pid, data):
    db.execute("INSERT INTO events (at, project_id, kind, data_json) VALUES (?, ?, ?, ?)",
               (at, pid, kind, json.dumps(data)))


def test_timeline_groups_and_texts(c):  # noqa: F811
    co, data = c
    pid = co.create_project("M 31", [])["id"]
    db = co.db
    db.execute("UPDATE events SET at = '2026-10-01T12:00:00Z'")
    g = lambda n, f, night, h: dict(type="light", filter=f, night=night, n=n, hours=h)  # noqa: E731
    _ev(db, "2026-11-01T20:00:00Z", "files_discovered", pid, dict(new=10, groups=[g(10, "Ha", "2026-11-01", 1.0)]))
    _ev(db, "2026-11-01T22:00:00Z", "files_discovered", pid, dict(new=12, groups=[g(10, "Ha", "2026-11-01", 1.0),
                                                                                  dict(type="flat", filter="Ha",
                                                                                       night="2026-11-01", n=2,
                                                                                       hours=0)]))
    _ev(db, "2026-11-02T08:00:00Z", "stack_done", pid, dict(run="RGB", hours=14.2, lights=300))
    _ev(db, "2026-11-02T08:05:00Z", "stack_done", pid, dict(run="Ha", hours=2.0, lights=20))
    _ev(db, "2026-11-02T09:00:00Z", "frames_decided", pid, dict(n=3, decision="skip"))
    _ev(db, "2026-11-02T09:10:00Z", "frames_decided", pid, dict(n=1, decision="use"))
    _ev(db, "2026-11-02T10:00:00Z", "stack_failed", pid, dict(run="OIII", error="WBPP hat kein Master erzeugt."))
    tl = co.timeline(pid)
    texts = [i["title"] for i in tl]
    assert texts[0] == "Stacken fehlgeschlagen: OIII" and tl[0]["tone"] == "err"
    assert texts[1] == "Frames von Hand: 1 verwendet, 3 aussortiert" and tl[1]["count"] == 2
    assert texts[2] == "Gestackt: RGB 14 h, Ha 2,0 h" and tl[2]["detail"] == "320 Lights"
    assert texts[3] == "20 neue Lights · Ha · 2,0 h" and tl[3]["detail"] == "Nacht vom 01.11.2026 · dazu 2 Flats"
    assert texts[-1] == "Projekt angelegt"
    # allgemeiner Verlauf: dieselben Einträge mit Projektname
    assert co.timeline()[0]["project"] == "M 31"
    assert history.hours(0.25) == "0,2 h" or history.hours(0.25) == "0,3 h"


# ---------------------------------------------------------------- Sicherung und Wiederherstellen

@pytest.fixture
def srv2(c, tmp_path, monkeypatch):  # noqa: F811
    co, data = c
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    yield srv, data
    _stop(srv)
    srv.core.close()


def test_backup_location_list_and_restore(srv2, tmp_path):
    srv, data = srv2
    extra = str(tmp_path / "Sicherungen woanders")
    assert call(srv, "POST", "api/settings", {"backup_dir": extra})[0] == 200
    st, b, _ = call(srv, "POST", "api/backup")
    assert st == 200 and os.path.isfile(os.path.join(extra, os.path.basename(b["path"])))
    first = os.path.basename(b["path"])
    assert b["backups"][0]["places"] == ["system", "extra"] and b["backups"][0]["size"] > 0
    old = srv.core
    pid = old.create_project("Nach der Sicherung", [])["id"]
    assert old.tasks.wait_idle(30)
    st, lst, _ = call(srv, "GET", "api/backups")
    assert st == 200 and lst[0]["name"] == first and lst[0]["at"]
    assert call(srv, "POST", "api/backups/restore", {"name": "gibt-es-nicht.db"})[0] == 400
    st, r, _ = call(srv, "POST", "api/backups/restore", {"name": first})
    assert st == 200 and r["restored"] == first
    assert srv.core is not old
    assert call(srv, "GET", "api/projects")[1] == []                   # Projekt nach der Sicherung ist weg
    assert any(x["label"] == "vor-wiederherstellen" for x in srv.core.backups())
    assert srv.core.timeline()[0]["kind"] == "backup_restored"
    assert pid


def test_restore_refused_while_stacking(srv2, monkeypatch):
    srv, data = srv2
    co = srv.core
    name = os.path.basename(co.backup())
    import threading
    go = threading.Event()
    co.tasks.submit("stack", "x stacken", lambda t: go.wait(10), lane="stack")
    st, body, _ = call(srv, "POST", "api/backups/restore", {"name": name})
    go.set()
    assert st == 400 and "gestackt" in body["error"]


# ---------------------------------------------------------------- HTTP-Routen für Bilder und Galerie

def test_http_stack_preview_compare_gallery_cover(stacked2, tmp_path, monkeypatch):
    co, pid, data = stacked2
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    srv = _serve(co, s)
    try:
        st = call(srv, "GET", "api/projects/%d/stacks" % pid)[1]
        code, body, h = call(srv, "GET", st[0]["preview_url"], raw=True)
        assert code == 200 and body[:2] == b"\xff\xd8" and "immutable" in h["Cache-Control"]
        assert call(srv, "GET", "api/stacks/999/preview", raw=True)[0] == 404
        a, b = st[1]["id"], st[0]["id"]
        code, cmp, _ = call(srv, "GET", "api/projects/%d/compare?a=%d&b=%d" % (pid, a, b))
        assert code == 200 and cmp["image_a"] and cmp["metrics"][0]["key"] == "fwhm"
        assert call(srv, "GET", cmp["image_b"], raw=True)[1][:2] == b"\xff\xd8"
        assert call(srv, "GET", "api/projects/%d/compare?a=%d&b=999" % (pid, a))[0] == 404
        img = _jpeg(str(tmp_path / "x" / "Final.jpg"))
        code, r, _ = call(srv, "POST", "api/projects/%d/gallery" % pid, {"paths": [os.path.dirname(img)]})
        assert code == 200 and r["new"] == 1
        g = call(srv, "GET", "api/projects/%d/gallery" % pid)[1]
        assert call(srv, "GET", g[0]["thumb_url"], raw=True)[1][:2] == b"\xff\xd8"
        assert call(srv, "POST", "api/projects/%d/cover" % pid, {"asset_id": g[0]["id"]})[1] == {"ok": True}
        assert call(srv, "GET", "api/projects")[1][0]["cover_url"] == g[0]["thumb_url"]
        assert call(srv, "POST", "api/projects/%d/cover" % pid, {"asset_id": "x"})[0] == 400
        assert call(srv, "POST", "api/projects/%d/gallery/%d/remove" % (pid, g[0]["id"]))[1] == {"ok": True}
        assert call(srv, "GET", "api/projects/%d/gallery" % pid)[1] == []
        hist = call(srv, "GET", "api/history?project=%d" % pid)[1]
        assert hist[0]["title"] and "icon" in hist[0]
        assert call(srv, "GET", "api/history?raw=1")[1][0]["data_json"]
    finally:
        _stop(srv)


# ---------------------------------------------------------------- MCP auf Core

def mcp_call(srv, name, args=None, mid=1):
    st, body, _ = call(srv, "POST", "mcp", {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                                            "params": {"name": name, "arguments": args or {}}},
                       headers={"Authorization": "Bearer " + TOKEN})
    assert st == 200, body
    res = body["result"]
    text = res["content"][0]["text"]
    return (json.loads(text) if not res["isError"] else text), res["isError"]


def test_mcp_tools_on_core(srv2, tmp_path):
    srv, data = srv2
    h = {"Authorization": "Bearer " + TOKEN}
    st, init, _ = call(srv, "POST", "mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, headers=h)
    assert "Hintergrund" in init["result"]["instructions"]
    st, tl, _ = call(srv, "POST", "mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
    names = {t["name"] for t in tl["result"]["tools"]}
    assert {"status", "list_projects", "project", "frames", "set_frames", "change_plan", "analyze", "create_project",
            "add_folders", "stack", "job", "cancel_job", "history", "logs", "stacks", "run_pixinsight_script"} <= names
    assert not names & {"delete_stack", "delete_frames"}                 # nichts kann Bilder löschen
    r, err = mcp_call(srv, "create_project", {"name": "vdB 131", "folders": [data]})
    assert not err and r["job"]["state"] in ("queued", "running")
    r, _ = mcp_call(srv, "job", {"id": r["job"]["id"], "wait_s": 60})
    assert r["state"] == "done", r
    r, _ = mcp_call(srv, "list_projects")
    assert r[0]["name"] == "vdB 131" and r[0]["lights"] == 6
    p, _ = mcp_call(srv, "project", {"name": "vdB 131"})
    assert p["ready"] and p["runs"][0]["settings"]
    ins, err = mcp_call(srv, "insights", {"project": "vdB 131"})
    assert not err and ins["nights"] and ins["nights"][0]["why"] and isinstance(ins["rig"], list)
    r, err = mcp_call(srv, "import_stacks", {"project": "vdB 131", "paths": [str(tmp_path / "gibt-es-nicht")]})
    assert not err and r["added"] == [] and r["found"] == 0
    fr, _ = mcp_call(srv, "frames", {"project": "vdB 131", "sort": "fwhm", "limit": 3})
    assert fr["total"] == 7 and len(fr["frames"]) == 3 and fr["more"] == 4
    r, _ = mcp_call(srv, "set_frames", {"project": "vdB 131", "frames": [fr["frames"][0]["name"]], "decision": "out"})
    assert r["changed"] == 1 and r["decision"] == "skip"
    r, err = mcp_call(srv, "set_frames", {"project": "vdB 131", "frames": ["gibt-es-nicht.fits"], "decision": "in"})
    assert err and "Nichts geändert" in r
    run_id = p["runs"][0]["id"]
    r, _ = mcp_call(srv, "change_plan", {"project": "vdB 131", "runs": {run_id: {"local_normalization": True}}})
    assert r["changed"] and "local_normalization" in r["runs"][0]["edited"] or r["runs"][1]["edited"]
    r, _ = mcp_call(srv, "stack", {"project": "vdB 131"})
    assert r["job"]["title"].endswith("· per Claude")
    j, _ = mcp_call(srv, "job", {"id": r["job"]["id"], "wait_s": 60})
    assert j["state"] == "done", j
    sts, _ = mcp_call(srv, "stacks", {"project": "vdB 131"})
    assert sts[0]["status"] == "done"
    r, _ = mcp_call(srv, "stack_note", {"project": "vdB 131", "stack_id": sts[0]["id"], "text": "per KI"})
    assert srv.core.stacks("vdB 131")[0]["note"] == "per KI"
    hi, _ = mcp_call(srv, "history", {"project": "vdB 131"})
    assert any(x["title"].startswith("Gestackt") for x in hi)
    r, err = mcp_call(srv, "cancel_job", {"id": "gibt-es-nicht"})
    assert err
    r, _ = mcp_call(srv, "analyze", {"project": "vdB 131"})
    assert mcp_call(srv, "job", {"id": r["job"]["id"], "wait_s": 60})[0]["state"] == "done"
    # PixInsight-Skript als Auftrag in der Spur „stack“ (nachgebauter Lauf)
    script = os.path.join(srv.core.ws.root, "test.js")
    with open(script, "w") as fh:
        fh.write("#engine v8\nconsole.writeln('hallo');\n")
    seen = {}

    def fake_run(exe, path, args, workdir, log=print, cancel=None, timeout_s=0):
        log("PI: hallo")
        seen.update(path=path, args=args)
        return dict(exit_code=0, state="done", output_tail=["hallo"])
    srv.core.script_runner = fake_run
    r, _ = mcp_call(srv, "run_pixinsight_script", {"script": "test.js", "args": ["a"]})
    assert r["job"]["title"] == "Skript test.js · per Claude"
    j, _ = mcp_call(srv, "job", {"id": r["job"]["id"], "wait_s": 30})
    assert j["state"] == "done" and j["result"]["exit_code"] == 0 and seen["args"] == ["a"]
    r, err = mcp_call(srv, "run_pixinsight_script", {"script": "/etc/passwd"})
    assert err and "Arbeitsverzeichnis" in r
    st, _ = mcp_call(srv, "status")
    assert st["pixinsight_ok"] and st["projects"] == 1


# ---------------------------------------------------------------- Kommandozeile „neu“

def test_cli_neu_direct_and_via_server(c, tmp_path, monkeypatch, capsys):  # noqa: F811
    co, data = c
    s = _settings(tmp_path, monkeypatch, co.ws.root, co.pixinsight)
    co.create_project("vdB 131", [data])
    assert co.tasks.wait_idle(60)
    srv = _serve(co, s)
    try:
        monkeypatch.setenv("B2_WEB_URL", srv.url)
        cli.main(["--json", "neu", "projects"])
        out = json.loads(capsys.readouterr().out)
        assert out[0]["name"] == "vdB 131" and out[0]["lights"] == 6
        cli.main(["--json", "neu", "refresh", "vdB 131"])
        assert json.loads(capsys.readouterr().out)["state"] == "done"
        cli.main(["neu", "history", "vdB 131"])
        assert "neue Lights" in capsys.readouterr().out
        cli.main(["neu", "frames", "vdB 131", "--status", "skip"])
        assert "1 von 1" in capsys.readouterr().out
        with pytest.raises(SystemExit, match="nicht gefunden"):
            cli.main(["neu", "project", "gibt es nicht"])
    finally:
        _stop(srv)
    co.close()
    # ohne Server: Core direkt
    monkeypatch.setenv("B2_WEB_URL", "http://127.0.0.1:9/")
    cli.main(["--json", "neu", "stacks", "vdB 131"])
    assert json.loads(capsys.readouterr().out) == []
    cli.main(["neu", "project", "vdB 131"])
    assert "Bereit zum Stacken" in capsys.readouterr().out


def test_broken_backup_is_refused_and_old_state_stays(srv2):
    srv, data = srv2
    co = srv.core
    co.create_project("Bleibt", [])
    assert co.tasks.wait_idle(30)
    name = os.path.basename(co.backup())
    path = co.find_backup(name)
    with open(path, "r+b") as fh:                       # Sicherung beschädigen
        fh.seek(100)
        fh.write(b"\0" * 4000)
    st, body, _ = call(srv, "POST", "api/backups/restore", {"name": name})
    assert st == 400 and "bisherige Stand" in body["error"]
    assert [p["name"] for p in call(srv, "GET", "api/projects")[1]] == ["Bleibt"]
