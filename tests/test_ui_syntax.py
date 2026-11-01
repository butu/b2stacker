"""Die JavaScript-Teile der Oberfläche und des Berichts müssen fehlerfrei parsen (sonst bleibt die Seite leer)."""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "astrostack")


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
@pytest.mark.parametrize("rel", ["app/index.html", "report/template.html"])
def test_js_parses(rel, tmp_path):
    html = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    scripts = [m for m in re.findall(r"<script>(.*?)</script>", html, re.S)]
    assert scripts
    for i, js in enumerate(scripts):
        f = tmp_path / ("s%d.js" % i)
        f.write_text("new Function(" + repr(js).replace("\\n", "\\\\n") + ");", encoding="utf-8")
        js_file = tmp_path / ("c%d.js" % i)
        js_file.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-800:]
