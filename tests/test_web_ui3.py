"""Neue Oberfläche, Teil 3 im Browser: Frames löschen, Sterne und Zoom in der Großansicht, Kalibrier-Bibliothek,
Selbsttest und „In PixInsight öffnen“, Zoom im Vergleich, Handy-Ansicht."""
import os
import re
import sys

import pytest

from .test_web_ui import App, _wait, browser, pid_of, with_stacks  # noqa: F401

try:
    from playwright.sync_api import expect
except Exception:  # noqa: BLE001
    expect = None

pytestmark = pytest.mark.skipif(expect is None, reason="Playwright nicht installiert")
SHOTS = os.environ.get("B2_SHOTS")                     # Ordner für Bildschirmfotos (zum Anschauen)


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    a = App(str(tmp_path), monkeypatch)
    a.core.create_project("vdB 131", [a.data], "Iris-Nebel im Kepheus")
    assert a.core.tasks.wait_idle(60)
    yield a
    a.stop()


def _page(browser, app, **ctx):  # noqa: F811
    c = browser.new_context(**({"viewport": {"width": 1400, "height": 900}, "device_scale_factor": 1} | ctx))
    pg = c.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(app.url)
    return c, pg, errors


@pytest.fixture
def page(browser, app):  # noqa: F811
    c, pg, errors = _page(browser, app)
    yield pg
    c.close()
    assert errors == [], errors


def _stack(app, pid):
    plan = app.core.plan(pid)
    ha = next(x["id"] for x in plan["runs"] if x["filter"] == "HaO3")
    t = app.core.stack(pid, [ha])
    assert app.core.tasks.wait_idle(60)
    assert app.core.tasks.get(t["id"]).state == "done"


def test_mark_and_delete_frames(page, app, tmp_path):
    pid = pid_of(app, "vdB 131")
    page.goto(app.url + "#/p/%d/frames" % pid)
    expect(page.locator("#vgrid .vt")).to_have_count(7)
    page.locator("#vgrid .vt[data-i='0'] .cb").click()
    page.locator("#vgrid .vt[data-i='1']").click(modifiers=["Shift"])
    page.locator("#selBar [data-bulk='delete']").click()
    expect(page.locator("#vgrid .vt.del")).to_have_count(2)
    expect(page.locator("#delBan")).to_contain_text("2 Frames zum Löschen vorgemerkt")
    page.click("#selClr")
    page.locator("#fStat button", has_text="Zum Löschen").click()
    expect(page.locator("#vgrid .vt")).to_have_count(2)
    paths = [f["path"] for f in app.core.frames(pid, status="delete")["frames"]]
    assert len(paths) == 2 and all(os.path.isfile(p) for p in paths)
    page.click("#delShow")
    dlg = page.locator("#delDlg")
    expect(dlg).to_be_visible()
    expect(dlg.locator(".dl")).to_have_count(2)
    expect(page.locator("#delOk")).to_contain_text("2 Frames")
    shot(page, "loeschen-dialog")
    # einen behalten
    dlg.locator("[data-keep]").first.click()
    expect(dlg.locator(".dl")).to_have_count(1)
    page.click("#delOk")
    expect(dlg).to_be_hidden()
    expect(page.locator("#delBan")).to_be_empty()
    _wait(lambda: sum(os.path.isfile(p) for p in paths) == 1)
    if sys.platform != "win32":
        assert len(os.listdir(tmp_path / "xdg" / "Trash" / "files")) == 1
    page.goto(app.url + "#/p/%d/history" % pid)
    expect(page.locator("#view")).to_contain_text("1 Frame gelöscht")


def test_lightbox_stars_zoom_and_delete_key(page, app):
    pid = pid_of(app, "vdB 131")
    page.goto(app.url + "#/p/%d/frames" % pid)
    page.locator("#vgrid .vt[data-i='0']").dblclick()
    expect(page.locator("#lb")).to_be_visible()
    expect(page.locator("#lbLoad")).to_be_hidden(timeout=15000)
    page.keyboard.press("s")
    expect(page.locator("#lbSvg ellipse").first).to_be_attached(timeout=15000)
    assert page.locator("#lbSvg ellipse").count() > 5
    expect(page.locator("#lbStarInfo")).to_contain_text("Sterne erkannt")
    expect(page.locator("#lbStars")).to_have_class(re.compile(r"\bon\b"))
    box = page.locator("#lbImgBox").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -600)
    expect(page.locator("#lbZoomI")).to_be_visible()
    assert "scale(" in page.locator("#lbZoom").evaluate("e => e.style.transform")
    shot(page, "grossansicht-sterne-zoom")
    page.keyboard.press("0")
    expect(page.locator("#lbZoomI")).to_be_hidden()
    page.keyboard.press("Delete")                                               # vormerken → weiter
    expect(page.locator("#lbN")).to_contain_text("2 von 7")
    _wait(lambda: app.core.deletion_list(pid)["count"] == 1)


def test_library_page_and_delete_master(page, app):
    pid = pid_of(app, "vdB 131")
    _stack(app, pid)
    page.click("#libBtn")
    expect(page.locator("h1")).to_have_text("Kalibrier-Bibliothek")
    rows = page.locator(".mrow")
    n = len(app.core.calibration_library())
    expect(rows).to_have_count(n)
    expect(page.locator("#libBox")).to_contain_text("Master-Bias")
    expect(page.locator("#libBox")).to_contain_text("in 1 Stack verwendet")
    shot(page, "kalibrier-bibliothek")
    rows.first.locator("[data-mdel]").click()
    page.click("#askOk")
    expect(rows).to_have_count(n - 1)


def test_stack_open_in_pixinsight_and_selftest_settings(page, app, monkeypatch):
    pid = pid_of(app, "vdB 131")
    _stack(app, pid)
    calls = []
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, *a, **k: calls.append(cmd))
    page.goto(app.url + "#/p/%d/stacks" % pid)
    page.locator("[data-pi]").first.click()
    expect(page.locator(".toast", has_text="PixInsight öffnet")).to_be_visible()
    assert calls and calls[0][0] == app.core.pixinsight
    page.click("#setBtn")
    expect(page.locator("#setDlg")).to_contain_text("Selbsttest mit PixInsight")
    expect(page.locator("#stBtn")).to_be_visible()


def test_compare_zoom(page, app):
    pid = pid_of(app, "vdB 131")
    with_stacks(app.core, pid)
    page.goto(app.url + "#/p/%d/stacks" % pid)
    page.click("#cmpLatest")
    page.wait_for_function("() => document.querySelector('#cmpIa').naturalWidth > 0")
    box = page.locator("#cmpBox").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -500)
    expect(page.locator("#cmpZoomI")).to_be_visible()
    x0 = page.locator("#cmpBox").evaluate("e => e.style.getPropertyValue('--x')")
    b2 = page.locator("#cmpImg").bounding_box()                                  # ziehen weit weg vom Schieber: verschieben
    page.mouse.move(b2["x"] + 30, b2["y"] + b2["height"] - 40)
    page.mouse.down()
    page.mouse.move(b2["x"] + 130, b2["y"] + b2["height"] - 60, steps=4)
    page.mouse.up()
    assert page.locator("#cmpBox").evaluate("e => e.style.getPropertyValue('--x')") == x0
    shot(page, "vergleich-zoom")
    page.keyboard.press("Escape")


def test_phone_layout_swipe_and_no_delete(browser, app):  # noqa: F811
    from playwright.sync_api import sync_playwright  # noqa: F401
    pid = pid_of(app, "vdB 131")
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True,
                              has_touch=True)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    try:
        for url in ("#/", "#/p/%d" % pid, "#/p/%d/frames" % pid, "#/p/%d/plan" % pid, "#/p/%d/stacks" % pid,
                    "#/kalibrierung", "#/verlauf"):
            pg.goto(app.url + url)
            pg.wait_for_timeout(300)
            over = pg.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            assert over <= 1, (url, over)                                      # kein waagrechtes Scrollen
        pg.goto(app.url + "#/p/%d/frames" % pid)
        pg.locator("#vgrid .vt[data-i='0']").tap()
        expect(pg.locator("#lb")).to_be_visible()
        expect(pg.locator("#lbN")).to_contain_text("1 von 7")
        box = pg.locator("#lbImgBox").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        def swipe(dx, dy):
            pg.locator("#lbImgBox").dispatch_event("pointerdown", {"pointerType": "touch", "pointerId": 7,
                                                                    "clientX": cx, "clientY": cy, "isPrimary": True})
            pg.locator("#lbImgBox").dispatch_event("pointerup", {"pointerType": "touch", "pointerId": 7,
                                                                  "clientX": cx + dx, "clientY": cy + dy, "isPrimary": True})
        swipe(-120, 5)
        expect(pg.locator("#lbN")).to_contain_text("2 von 7")
        swipe(4, 110)                                                          # runter: nicht verwenden → weiter
        expect(pg.locator("#lbN")).to_contain_text("3 von 7")
        fid = app.core.frames(pid, limit=10)["frames"][1]["id"]
        _wait(lambda: next(f for f in app.core.frames(pid, limit=10)["frames"] if f["id"] == fid)["status"] == "skip")
        pg.keyboard.press("Escape")
        app.core.decide(pid, [fid], "delete")
        pg.goto(app.url + "#/p/%d/frames" % pid)
        pg.click("#delShow")
        expect(pg.locator("#delBody")).to_contain_text("nur am Computer")
        expect(pg.locator("#delOk")).to_be_disabled()
        shot(pg, "handy-loeschen")
    finally:
        ctx.close()
    assert errors == [], errors


def test_phone_pairs_over_lan_and_sees_projects(browser, app, monkeypatch):  # noqa: F811
    from astrostack.settings import Settings
    from astrostack.web import server as web
    s = Settings.load()
    s.lan_port = 0
    s.save()
    web.set_lan(app.srv, True)
    port = app.srv.lan_server.server_address[1]
    key = Settings.load().lan_key
    # Einstellungen am Computer zeigen den QR-Code
    c, pg, errors = _page(browser, app)
    try:
        pg.click("#setBtn")
        expect(pg.locator("#lanOn")).to_be_checked()
        expect(pg.locator("#lanBox")).to_contain_text("Alle Handys abmelden")
        pg.locator("#lanBox").scroll_into_view_if_needed()
        shot(pg, "einstellungen-handy")
    finally:
        c.close()
    assert errors == [], errors
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    ph = ctx.new_page()
    errs = []
    ph.on("pageerror", lambda e: errs.append(str(e)))
    try:
        ph.goto("http://127.0.0.1:%d/" % port)
        expect(ph.locator("body")).to_contain_text("nicht gekoppelt")
        ph.goto("http://127.0.0.1:%d/pair?k=%s" % (port, key))
        expect(ph.locator(".tile[data-pid]")).to_have_count(1)
        expect(ph.locator("#setBtn")).to_be_hidden()
        pid = pid_of(app, "vdB 131")
        ph.goto("http://127.0.0.1:%d/#/p/%d/frames" % (port, pid))
        expect(ph.locator("#vgrid .vt")).to_have_count(7)
        ph.wait_for_function("() => [...document.querySelectorAll('#vgrid img')].some(i => i.naturalWidth > 0)")
        shot(ph, "handy-gekoppelt-frames")
    finally:
        ctx.close()
    assert errs == [], errs


def test_frames_sort_filter_group_size_and_click_zoom(page, app):
    pid = pid_of(app, "vdB 131")
    page.goto(app.url + "#/p/%d/frames" % pid)
    expect(page.locator("#vgrid .vt")).to_have_count(7)
    page.select_option("#fSortSel", "fwhm")                                    # schlechteste (größte) zuerst
    fw = [f["fwhm"] for f in app.core.frames(pid)["frames"]]
    first = page.locator("#vgrid .vt[data-i='0'] .fx .m").inner_text()
    assert first.replace("FWHM ", "").replace(",", ".").strip() == "%.2f" % max(x for x in fw if x is not None)
    page.select_option("#fFilt", "RGB")
    expect(page.locator("#vgrid .vt")).to_have_count(3)
    page.select_option("#fFilt", "")
    page.locator("label.tog", has_text="nach Nacht gruppieren").click()
    expect(page.locator("#vgrid .vgh")).to_have_count(2)                      # 12.10. und 20.07.
    expect(page.locator("#vgrid .vgh").first).to_contain_text("Nacht vom")
    shot(page, "frames-gruppiert")
    w0 = page.locator("#vgrid .vt").first.bounding_box()["width"]
    page.locator("#fSize").fill("400")
    w1 = page.locator("#vgrid .vt").first.bounding_box()["width"]
    assert w1 > w0 * 1.5
    page.locator("label.tog", has_text="nach Nacht gruppieren").click()
    # Großansicht: Klick zoomt, Klick passt wieder ein; Metadaten aus dem Header
    page.locator("#vgrid .vt[data-i='0']").click()
    expect(page.locator("#lbMore")).to_contain_text("Gain")
    box = page.locator("#lbImgBox").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(page.locator("#lbZoomI")).to_be_visible()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    expect(page.locator("#lbZoomI")).to_be_hidden()
