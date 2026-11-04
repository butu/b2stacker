"""Neue Oberfläche (astrostack/web/static/index.html) im echten Browser: Playwright + Chromium.

Der neue Server läuft im Testprozess mit einem kleinen synthetischen Datensatz und nachgebautem PixInsight.
Ohne Playwright (z. B. in der Windows-CI) werden die Tests übersprungen.
"""
import json
import os
import re
import subprocess
import sys
import threading

import pytest

try:
    from playwright.sync_api import expect, sync_playwright
except Exception:  # noqa: BLE001 – Playwright fehlt: alle Tests überspringen
    sync_playwright = None

from astrostack import core, handover, library
from astrostack import db as dbm
from astrostack.settings import Settings
from astrostack.web import server as web

from .conftest import NINA_LIGHT, make_fits
from .test_flow import _calib_tree, _stars
from .test_handover import FAKE

pytestmark = pytest.mark.skipif(sync_playwright is None, reason="Playwright nicht installiert")


# ---------------------------------------------------------------- Testdaten

def make_dataset(root: str) -> str:
    """Wie die Fixture ``env`` + ``_calib_tree``: 6 Lights (HaO3, RGB), Flats, Bias – echte FITS-Dateien."""
    data = os.path.join(root, "Rohdaten, NAS")
    for i in range(3):
        make_fits(os.path.join(data, "vdB 131", "2026-10-12", "HaO3", "L_%04d.fits" % i),
                  dict(NINA_LIGHT, **{"DATE-OBS": "2026-10-12T20:%02d:00" % (10 * i)}), data=_stars(i))
    make_fits(os.path.join(data, "vdB 131", "_rejected", "L_9999.fits"), NINA_LIGHT, data=_stars(9))
    hf = dict(NINA_LIGHT, IMAGETYP="FLAT", EXPOSURE=1.0, EXPTIME=1.0,
              **{"DATE-OBS": "2026-10-31T17:00:00", "DATE-LOC": "2026-10-31T18:00:00"})
    hf.pop("OBJECT")
    for i in range(2):
        make_fits(os.path.join(data, "FLAT", "2026-10-31", "F_%04d.fits" % i), hf, shape=(128, 192), value=30000)
    make_fits(os.path.join(data, "Bias", "B_0000.fits"), dict(NINA_LIGHT, IMAGETYP="BIAS", EXPOSURE=0.0001,
                                                              EXPTIME=0.0001), shape=(128, 192), value=500)
    _calib_tree(data)
    return data


def fake_pixinsight(root: str):
    """Nachgebautes PixInsight als echter Unterprozess (wie in test_core) → (exe, runner)."""
    exe = os.path.join(root, "pi", "bin", "PixInsight.exe")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    open(exe, "w").close()
    fake = os.path.join(root, "fakepi.py")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write(FAKE)

    def run(*a, **kw):
        return handover.run(*a, popen=lambda cmd, **k: subprocess.Popen(
            [sys.executable, fake] + cmd[1:], **{x: y for x, y in k.items() if x != "creationflags"}),
            poll_s=0.05, **kw)
    return exe, run


def fake_project(co, name: str, n: int, filters=("Ha", "OIII", "SII"), nights=("2026-08-14", "2026-09-02"),
                 thumb: str | None = None, skip_every: int = 7, offline: bool = True, obj: str = "") -> int:
    """Projekt mit ``n`` erfundenen Lights direkt in der Datenbank (für viele Frames, Monate, Filter).
    Der Quellordner existiert nicht → „Quelle offline“, die Prüfung lässt die Datensätze in Ruhe."""
    db, ws = co.db, co.ws
    folders = [os.path.join(ws.root, "gibt-es-nicht", name)] if offline else []
    pid = library.create_project(db, ws, name, folders=folders)
    field = db.value("SELECT id FROM fields WHERE project_id = ?", (pid,))
    now = dbm.now()
    with db.transaction():
        if obj:
            db.execute("UPDATE projects SET object = ? WHERE id = ?", (obj, pid))
        db.execute("UPDATE source_folders SET online = 0 WHERE project_id = ?", (pid,))
        for i in range(n):
            night = nights[i % len(nights)]
            filt = filters[i % len(filters)]
            date = "%sT%02d:%02d:%02d" % (night, 20 + (i // 3600) % 4, (i // 60) % 60, i % 60)
            cur = db.execute(
                "INSERT INTO files (path, size, mtime_ns, checksum, discovered_at, header_version, type, filter, "
                "exposure, date_obs, night, object, width, height) VALUES (?, 1, 1, ?, ?, 1, 'light', ?, 300, ?, ?, ?, "
                "6000, 4000)",
                (os.path.join(ws.root, "fake", name, filt, "L_%05d.fits" % i), "%064d" % i, now, filt, date, night,
                 obj or name))
            fid = int(cur.lastrowid)
            db.execute("INSERT INTO project_files (project_id, file_id, field_id) VALUES (?, ?, ?)", (pid, fid, field))
            m = dict(nstars=300 + i % 50, fwhm=2.1 + (i % 13) / 10, ecc=0.4 + (i % 5) / 20, bg=1100 + i % 40, noise=14.2)
            db.execute("INSERT INTO frame_metrics (file_id, version, measured_at, nstars, fwhm, ecc, bg, noise, thumb, "
                       "data_json) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (fid, now, m["nstars"], m["fwhm"], m["ecc"], m["bg"], m["noise"], thumb,
                        json.dumps(dict(m, date_obs=date, night=night, filter=filt, exptime=300))))
            if skip_every and i % skip_every == 3:
                db.execute("INSERT INTO file_status (project_id, file_id, status, origin, reason, decided_at) "
                           "VALUES (?, ?, 'skip', 'auto', 'Sterne unscharf (FWHM deutlich über dem Median)', ?)",
                           (pid, fid, now))
    return pid


def settings_file(root: str, workdir: str, pixinsight: str) -> str:
    sf = os.path.join(root, "settings.json")
    with open(sf, "w", encoding="utf-8") as fh:
        json.dump({"workdir": workdir, "pixinsight": pixinsight, "mcp_token": "t" * 40}, fh)
    return sf


class App:
    """Server + Core im Testprozess; ``url`` für den Browser."""

    def __init__(self, root: str, monkeypatch=None, with_settings: bool = True):
        self.root = root
        self.data = make_dataset(root)
        exe, run = fake_pixinsight(root)
        work = os.path.join(root, "work")
        sf = settings_file(root, work, exe) if with_settings else os.path.join(root, "settings.json")
        if monkeypatch is not None:
            monkeypatch.setenv("ASTROSTACK_SETTINGS", sf)
        else:
            os.environ["ASTROSTACK_SETTINGS"] = sf
        self.core = core.Core(work, pixinsight=exe if with_settings else "", workers=1, stack_runner=run)
        self.srv = web.create(0, settings=Settings.load(), core=self.core, refresh=False)
        self.srv.poll_s = 0.05
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = self.srv.url

    def stop(self):
        if not self.srv.stopping.is_set():
            self.srv.stop()
        self.core.close()
        if self.srv.core is not self.core:                # neues Arbeitsverzeichnis wurde übernommen
            self.srv.core.close()


# ---------------------------------------------------------------- Fixtures

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    a = App(str(tmp_path), monkeypatch)
    co = a.core
    co.create_project("vdB 131", [a.data], "Iris-Nebel im Kepheus")
    assert co.tasks.wait_idle(60)
    thumb = co.db.value("SELECT thumb FROM frame_metrics WHERE thumb IS NOT NULL LIMIT 1")
    fake_project(co, "Herz-Nebel", 40, thumb=thumb, obj="IC 1805")
    fake_project(co, "Cirrus", 12, filters=("L", "R", "G", "B"), nights=("2025-12-01",), offline=False)
    yield a
    a.stop()


@pytest.fixture
def page(browser, app):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=1)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(app.url)
    yield pg
    ctx.close()
    assert errors == [], errors


def pid_of(app, name):
    return library.find_project(app.core.db, name)


# ---------------------------------------------------------------- Startseite und neues Projekt

def test_start_page_tiles_search_sort_months_and_new_project(page, app, tmp_path):
    tiles = page.locator(".tile[data-pid]")
    expect(tiles).to_have_count(3)
    vdb = page.locator(".tile[data-pid]", has_text="vdB 131")
    expect(vdb.locator(".hrs b")).to_have_text("1,0")                            # 6 Lights à 10 min
    expect(vdb.locator(".fbr")).to_have_count(2)                                  # HaO3 + RGB
    expect(page.locator(".tile", has_text="Herz-Nebel").locator(".pill.err")).to_contain_text("Quelle offline")
    # Sortieren nach Gesamtbelichtung: Herz-Nebel (40 × 5 min, minus aussortierte) zuerst
    page.select_option("#sort", "hours")
    expect(tiles.first).to_contain_text("Herz-Nebel")
    page.select_option("#sort", "first")
    expect(tiles.first).to_contain_text("Cirrus")                                 # Dezember 2025
    # Suche nach Name und Objekt
    page.fill("#q", "ic 1805")
    expect(tiles).to_have_count(1)
    expect(tiles.first).to_contain_text("Herz-Nebel")
    page.fill("#q", "")
    expect(tiles).to_have_count(3)
    # Nach Monat: vdB 131 erscheint im Juli und im Oktober
    page.locator("label.tog", has_text="Nach Monat").click()
    expect(page.locator(".mhd")).to_have_count(5)
    expect(page.locator(".mhd").first).to_contain_text("Oktober 2026")
    expect(page.locator(".tile[data-pid]", has_text="vdB 131")).to_have_count(2)
    page.locator("label.tog", has_text="Nach Monat").click()
    expect(page.locator(".mhd")).to_have_count(0)

    # Neues Projekt über den Dialog (Pfad eintippen)
    page.click("#newBtn")
    page.fill("#npName", "Neu")
    page.fill("#npFolders input", os.path.join(app.data, "vdB 131"))
    page.click("#npOk")
    expect(page).to_have_url(re.compile(r"#/p/\d+$"))
    expect(page.locator("#pName")).to_have_text("Neu")
    assert app.core.tasks.wait_idle(60)
    expect(page.locator(".pmeta")).to_contain_text("Lights", timeout=10000)
    expect(page.locator("#tind")).to_contain_text("Alles erledigt")
    folders = app.core.project(pid_of(app, "Neu"))["folders"]
    assert folders[0]["path"].endswith("vdB 131")


def test_project_page_rename_describe_and_live_progress(page, app):
    pid = pid_of(app, "vdB 131")
    page.goto(app.url + "#/p/%d" % pid)
    expect(page.locator("#pName")).to_have_text("vdB 131")
    expect(page.locator(".banner.ok")).to_contain_text("Bereit zum Stacken")
    expect(page.locator(".srcrow")).to_have_count(1)
    # Umbenennen (Enter) und Beschreibung
    page.click("#pRen")
    page.fill("#renIn", "Iris")
    page.press("#renIn", "Enter")
    expect(page.locator("#pName")).to_have_text("Iris")
    assert app.core.project(pid)["name"] == "Iris"
    page.click("#pDesc")
    page.fill("#descIn", "Neue Notiz")
    page.click("#descOk")
    expect(page.locator("#pDesc")).to_have_text("Neue Notiz")
    # Neu prüfen: Auftrag erscheint live in der Kopfzeile und endet mit „Alles erledigt“
    page.click("#pRefresh")
    expect(page.locator("#tind")).to_contain_text("Alles erledigt", timeout=30000)


# ---------------------------------------------------------------- Frames

def test_frames_virtualized_grid_lightbox_and_keyboard(page, app):
    pid = fake_project(app.core, "Viele", 3000, thumb=None, skip_every=0)
    page.goto(app.url + "#/p/%d/frames" % pid)
    expect(page.locator("#fSum")).to_contain_text(re.compile(r"3\s000 von 3\s000"))
    n = page.locator("#vgrid .vt").count()
    assert 0 < n < 200, n                                                         # nur sichtbare Kacheln im DOM
    page.locator("#view").evaluate("v => v.scrollTop = v.scrollHeight")
    expect(page.locator("#vgrid .vt[data-i='2999']")).to_be_attached()             # letzte Kachel gerendert
    assert page.locator("#vgrid .vt").count() < 200
    # Status-Filter und Mehrfachauswahl
    page.locator("#fStat button", has_text="Aussortiert").click()
    expect(page.locator("#fSum")).to_contain_text(re.compile(r"^0 von 3\s000"))
    page.locator("#fStat button", has_text="Alle").click()
    page.locator("#view").evaluate("v => v.scrollTop = 0")
    first = page.locator("#vgrid .vt[data-i='0']")
    first.locator(".cb").click()
    page.locator("#vgrid .vt[data-i='4']").click(modifiers=["Shift"])
    expect(page.locator("#selBar")).to_contain_text("5 ausgewählt")
    page.locator("#selBar [data-bulk='skip']").click()
    expect(page.locator("#vgrid .vt.skip")).to_have_count(5)
    expect(page.locator("#fSum")).to_contain_text("5 aussortiert")
    page.click("#selClr")

    # Großansicht im kleinen Projekt mit echten Bildern
    vdb = pid_of(app, "vdB 131")
    page.goto(app.url + "#/p/%d/frames" % vdb)
    expect(page.locator("#vgrid .vt")).to_have_count(7)
    page.locator("#vgrid .vt[data-i='0']").dblclick()
    lb = page.locator("#lb")
    expect(lb).to_be_visible()
    expect(page.locator("#lbN")).to_contain_text("1 von 7")
    expect(page.locator("#lbSide .meta")).to_contain_text("FWHM")
    expect(page.locator("#lbLoad")).to_be_hidden(timeout=15000)                   # große Vorschau geladen
    ids = [f["id"] for f in app.core.frames(vdb, limit=10)["frames"]]
    st = lambda i: app.core.frames(vdb, limit=10)["frames"][i]                    # noqa: E731
    page.keyboard.press("n")                                                      # nicht verwenden → weiter
    expect(page.locator("#lbN")).to_contain_text("2 von 7")
    _wait(lambda: st(0)["status"] == "skip" and st(0)["origin"] == "manual")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#lbSide .pill").first).to_have_text("aussortiert")
    page.keyboard.press("a")                                                      # zurück auf Automatik
    _wait(lambda: st(0)["origin"] is None or st(0)["origin"] == "auto")
    page.keyboard.press("v")
    _wait(lambda: st(0)["status"] == "use" and st(0)["origin"] == "manual")
    for _ in range(8):
        page.keyboard.press("ArrowRight")
    expect(page.locator("#lbN")).to_contain_text("7 von 7")                       # bleibt am Ende stehen
    expect(page.locator("#lbMsg")).to_have_text("letztes Bild")
    page.keyboard.press("Escape")
    expect(lb).to_be_hidden()
    assert ids[0] == st(0)["id"]


def _wait(cond, timeout=10.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    assert cond()


# ---------------------------------------------------------------- Plan und Stacken

def test_plan_setting_persists_and_stack_shows_progress(page, app):
    pid = pid_of(app, "vdB 131")
    page.goto(app.url + "#/p/%d/plan" % pid)
    runs = page.locator(".run")
    expect(runs).to_have_count(2)
    ha = page.locator(".run", has_text="HaO3")
    expect(ha.locator(".calrow").first).to_contain_text("Flats")
    # Lokale Normalisierung umschalten → „angepasst“, gespeichert, Zurücksetzen geht
    sw = ha.locator(".setrow", has_text="Lokale Normalisierung")
    before = sw.locator("input").is_checked()
    sw.locator(".switch").click()
    expect(ha.locator(".setrow", has_text="Lokale Normalisierung").locator(".adj")).to_have_text("angepasst")
    plan = app.core.plan(pid)
    s = [x for r in plan["runs"] if r["name"] == "HaO3" for x in r["settings"] if x["key"] == "local_normalization"][0]
    assert s["value"] is (not before) and s["edited"]
    page.reload()
    expect(page.locator(".run", has_text="HaO3").locator(".setrow", has_text="Lokale Normalisierung")
           .locator(".adj")).to_have_text("angepasst")
    page.locator(".run", has_text="HaO3").locator(".setrow", has_text="Lokale Normalisierung").locator(".rst").click()
    expect(page.locator(".run", has_text="HaO3").locator(".adj")).to_have_count(0)
    # Auswahl: Rejection
    page.locator(".run", has_text="HaO3").locator("select[data-set$='|rejection']").select_option("winsorized")
    expect(page.locator(".run", has_text="HaO3").locator(".setrow", has_text="Pixel-Rejection").locator(".adj")) \
        .to_have_text("angepasst")
    # Stacken: Auftrag startet, Fortschritt kommt per SSE, am Ende fertig
    go = page.locator("#stackGo")
    expect(go).to_be_enabled()
    os.environ["FAKE_STEP_S"] = "0.4"
    try:
        go.click()
        expect(page.locator("#stackBox")).to_contain_text("gestackt", timeout=10000)
        expect(page.locator("#tind")).not_to_contain_text("Alles erledigt")
        assert app.core.tasks.wait_idle(60)
    finally:
        os.environ.pop("FAKE_STEP_S", None)
    expect(page.locator("#stackGo")).to_be_visible(timeout=10000)
    assert app.core.stacks(pid)[0]["status"] == "done"
    page.locator("#tind").click()
    expect(page.locator("#taskPop")).to_contain_text("fertig")


# ---------------------------------------------------------------- Ersteinrichtung und Einstellungen

def test_first_start_setup_and_settings(browser, tmp_path, monkeypatch):
    a = App(str(tmp_path), monkeypatch, with_settings=False)
    try:
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()
        pg.goto(a.url)
        expect(pg.locator("h1")).to_have_text("Willkommen bei b2stacker")
        expect(pg.locator("#suPiChk")).to_contain_text("✗")
        pg.fill("#suPi", os.path.join(str(tmp_path), "pi", "bin", "PixInsight.exe"))
        work2 = os.path.join(str(tmp_path), "Arbeitsverzeichnis")
        pg.fill("#suWd", work2)
        pg.click("#suGo")
        expect(pg.locator("#newDlg")).to_be_visible()                 # leer → gilt sofort, ohne Neustart
        assert Settings.load().workdir == work2 and os.path.exists(Settings.path())
        assert a.srv.core.ws.root == os.path.abspath(work2)
        pg.click("#newDlg [data-close]")
        expect(pg.locator("h1")).to_have_text("Projekte")
        pg.click("#setBtn")
        expect(pg.locator("#mcpUrl")).to_have_value(a.url + "mcp")
        pg.fill("#set_analysis_workers", "3")
        pg.click("#setSave")
        expect(pg.locator("#setDlg")).to_be_hidden()
        assert Settings.load().analysis_workers == 3 and a.srv.core.workers == 3
        ctx.close()
    finally:
        a.stop()
