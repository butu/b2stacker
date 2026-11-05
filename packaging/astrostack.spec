# PyInstaller-Spezifikation für AstroStack.exe (eine Datei, mit Konsole)
# Bauen: pyinstaller packaging/astrostack.spec --noconfirm
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(root, "astrostack", "app", "index.html"), "astrostack/app"),
    (os.path.join(root, "astrostack", "app", "vendor", "photoswipe"), "astrostack/app/vendor/photoswipe"),
    (os.path.join(root, "astrostack", "web", "static", "index.html"), "astrostack/web/static"),
    (os.path.join(root, "astrostack", "report", "template.html"), "astrostack/report"),
    (os.path.join(root, "astrostack", "report", "vendor"), "astrostack/report/vendor"),
    (os.path.join(root, "astrostack", "pjsr", "split.js"), "astrostack/pjsr"),
    (os.path.join(root, "astrostack", "pjsr", "cc_builder.js"), "astrostack/pjsr"),
    (os.path.join(root, "astrostack", "pjsr", "b2launch.js"), "astrostack/pjsr"),
]
datas += collect_data_files("astropy", excludes=["**/tests/**", "**/*.c", "**/*.h"])
datas += collect_data_files("astropy_iers_data")
hidden = collect_submodules("astropy.coordinates") + collect_submodules("astropy.io.fits") + ["erfa"]
hidden += ["astrostack.mcp", "astrostack.appclient", "astrostack.claude_link", "astrostack.web.server",
           "astrostack.web.window", "astrostack.web.lan", "astrostack.trash", "astrostack.selftest_neu",
           "astrostack.wbppschema", "astrostack.stackimport", "astrostack.flow.insights", "astrostack.checklist"]
hidden += collect_submodules("segno")         # QR-Code fürs Handy (nur in Funktionen importiert)
if sys.platform == "win32":
    hidden += ["tkinter", "tkinter.filedialog", "clr"]
    hidden += collect_submodules("webview")
    datas += collect_data_files("webview")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[root],
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "IPython", "pytest", "pandas", "PyQt5", "PySide6", "notebook", "sphinx"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="b2stacker",
    console=False,                 # eigenes Programmfenster, kein schwarzes Konsolenfenster
    icon=os.path.join(SPECPATH, "astrostack.ico"),
    upx=False,
)
