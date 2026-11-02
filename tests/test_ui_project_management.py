"""Projektverwaltung in der Oberfläche: Endpunkte, Wortlaut und Sperren müssen im HTML verdrahtet sein."""
import os
import json
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "astrostack")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


APP = _read("app/index.html")
REPORT = _read("report/template.html")


@pytest.mark.parametrize("needle", [
    "/api/project-create",
    "/delete'",
    "/stack-meta'",
    "/stack-delete'",
    "/asset-link'",
])
def test_app_uses_management_endpoints(needle):
    assert needle in APP


def test_remove_confirmations_promise_files_stay():
    # Projekt und Stack entfernen: beide Bestätigungen sagen ausdrücklich, dass nichts gelöscht wird
    assert APP.count("Alle Dateien bleiben erhalten") >= 2


@pytest.mark.parametrize("html", [APP, REPORT], ids=["app", "report"])
def test_status_wording_consistent(html):
    assert "Automatisch ausgeschlossen" in html
    assert "Manuell/über Ordner ausgeschlossen" in html
    assert "Aussortieren?" not in html
    assert "Doch verwenden" in html     # Legende erklärt, wie man Frames wieder aufnimmt


def test_stack_locked_until_reanalysis():
    assert "analysis_required" in APP
    assert re.search(r"analysis_required[^\n]*bl\.push|bl\.push\([^\n]*Neu analysieren", APP)


def test_manual_update_check_in_settings():
    m = re.search(r"function openSettings\(first\)\{(.*?)\n\}", APP, re.S)
    assert m and "checkUpdate" in m.group(1)


def test_hidden_update_button_overrides_flex_display():
    assert re.search(r"#updBtn\[hidden\]\s*\{\s*display\s*:\s*none\s*[;}]", APP)


def test_unreadable_lightbox_image_can_be_hidden():
    assert re.search(r"#frameDlg\s+\.fimg\s+img\[hidden\]\s*\{\s*display\s*:\s*none\s*[;}]", APP)


def test_project_sources_accept_typed_path_without_native_dialog():
    assert 'id="srcPath"' in APP
    assert "$('#srcPath').value.trim()" in APP
    assert "if(!path){const r=await api('/api/pick-folder'" in APP


def test_empty_projects_guard_tabs():
    assert "hasData" in APP
    assert "Planungsprojekt" in APP


def test_frame_browser_wired_to_frames_api():
    assert "/frames'" in APP
    for label in ("Lights", "Darks", "Flats", "Bias", "Dark-Flats"):
        assert label in APP
    # Häkchen schickt echte Auswahl an die Planung, kein reines Anzeige-Flag
    assert re.search(r"/override',\{path,value:val\}", APP)


def test_frame_checkbox_does_not_open_lightbox():
    assert "data-fsel" in APP
    assert "stopPropagation" in APP


def test_lightbox_keyboard_scoped():
    m = re.search(r"function fvKey\(e\)\{(.*?)\n\}", APP, re.S)
    assert m, "fvKey fehlt"
    body = m.group(1)
    assert "frameDlg.open" in body and "' '" in body
    assert "INPUT" in body and "TEXTAREA" in body


def test_embedded_report_defers_frames_to_app():
    assert "html.embedded #framesCard" in REPORT


SORT_CASES = r"""
const F=[
  {night:'2026-01-02',path:'a',status:'ok',flags:[],metrics:{nstars:50,ecc:0.4,fwhm:2.1,hfr_name:null}},
  {night:'2026-01-01',path:'b',status:'error',flags:[{level:'error',text:'x'}],metrics:{nstars:null,ecc:null,fwhm:null,hfr_name:2.5}},
  {night:'2026-01-03',path:'c',status:'warn',flags:[{level:'warn',text:'y'}],metrics:{nstars:10,ecc:0.7,fwhm:3.0,hfr_name:3.1}},
  {night:'2026-01-01',path:'d',status:'suggest',flags:[],metrics:{nstars:0,ecc:0.2,fwhm:null,hfr_name:1.9}},
  {path:'e',type:'flat',status:'rejected',flags:[],metrics:{}},
];
const I=F.map((_,i)=>i), p=(k,d)=>fvSortIdx(F,I,k,d).map(i=>F[i].path).join('');
console.log(JSON.stringify({
  starsUp:p('nstars',1), starsDown:p('nstars',-1), fwhmUp:p('fwhm',1), fwhmDown:p('fwhm',-1),
  hfrUp:p('hfr_name',1), qualUp:p('quality',1), qualDown:p('quality',-1), nightUp:p('night',1)}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_frame_sort_missing_values_last_both_directions(tmp_path):
    m = re.search(r"(const QRANK=.*?function fvSortIdx\(.*?\n\})", APP, re.S)
    assert m, "Sortier-Helfer fehlen"
    js = tmp_path / "sort.js"
    js.write_text(m.group(1) + SORT_CASES, encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    # 0 Sterne ist ein echter Messwert, null/leer = nicht gemessen und immer am Ende
    assert got["starsUp"] == "dcabe" and got["starsDown"] == "acdbe"
    assert got["fwhmUp"] == "acbde" and got["fwhmDown"] == "cabde"
    assert got["hfrUp"] == "dbcae"           # Aufnahme-HFR getrennt von gemessener FWHM
    # Qualität = Bewertung: Messfehler → automatisch ausgeschlossen → Hinweis → OK; ohne Bewertung zuletzt
    assert got["qualUp"] == "bdcae" and got["qualDown"] == "acdbe"
    assert got["nightUp"] == "bdace"


def test_frame_filters_and_labels():
    assert "fvSort" in APP and "fvDir" in APP
    assert "nicht gemessen" in APP
    assert "HFR (Aufnahme" in APP and "FWHM (gemessen" in APP
    assert "Auffällig" in APP and "Fehlerhaft" in APP


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_frame_type_normalization_preserves_bias():
    helper = re.search(r"const ftype=[^\n]*", APP).group(0)
    result = subprocess.run(
        ["node", "-e", helper + ";console.log(JSON.stringify(['bias','Lights','Darks','Flats','Dark-Flats'].map(ftype)))"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == ["bias", "light", "dark", "flat", "darkflat"]


def _fn(name):
    m = re.search(r"function %s\(.*?\n\}" % name, APP, re.S)
    assert m, name + " fehlt"
    return m.group(0)


def test_frame_tiles_never_load_full_previews():
    tile = _fn("fvTile")
    assert "preview_url" not in tile          # volle Vorschau nur in der Lightbox
    assert "thumb_url" in tile and 'loading="lazy"' in tile
    assert "FV.limit" in _fn("drawFrames")    # nicht 1000+ Kacheln auf einmal
    assert "outerHTML" not in _fn("fvUpdateTile")   # Häkchen lädt kein Bild neu


def test_lightbox_preview_debounced_and_space_safe():
    assert "clearTimeout" in _fn("renderFV")
    assert "e.repeat" in _fn("fvKey")


def test_selection_change_marks_plan_stale_immediately():
    m = re.search(r"const needsAnalysis=.*", APP)
    assert m and "??" not in m.group(0)
    assert "plan_stale=true" in _fn("setFrameUse")


TILE_CASES = r"""
const bad={path:'C:/x/<img src=x onerror=alert(1)>.fits',type:'light',filter:'<b>Ha</b>',night:'"><script>n</script>',
  selected:true,selection_source:'auto',status:'error',flags:[{level:'error',text:'<script>alert(2)</script>'}],
  thumb_url:'/api/frame-thumb/p?path=a" onload="alert(3)',metrics:{nstars:12,ecc:null,fwhm:null,hfr_name:null}};
FV.list=[bad,Object.assign({},bad,{thumb_url:null,metrics:{nstars:null,ecc:null,fwhm:null,hfr_name:null}})];
console.log(JSON.stringify({a:fvTile(0),b:fvTile(1)}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_frame_tile_thumbnail_and_escaping(tmp_path):
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "\n".join([pick(r"const esc=[^\n]*"), pick(r"const fmt=[^\n]*"), pick(r"const STXT=[^\n]*"),
                    pick(r"const FTYPES=.*?function fvTile\(.*?\n\}")]) + TILE_CASES
    f = tmp_path / "tile.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    a, b = got["a"], got["b"]
    for html in (a, b):
        assert "<script" not in html and "<b>Ha" not in html and "<img src=x" not in html
    assert 'loading="lazy"' in a and 'decoding="async"' in a and "/api/frame-thumb/" in a
    assert 'onload="alert' not in a                 # Anführungszeichen in der URL sind escaped
    assert "Vorschau nicht lesbar" in a             # defektes Bild: Kachel bleibt, Text sichtbar
    assert "preview_url" not in a and "Kein Vorschaubild" in b
    assert "nicht gemessen" in a and "nicht gemessen" in b


REPORT_CASES = r"""
const cols=[['night','Nacht'],['status','Status'],['fwhm_as','FWHM "'],['nstars','Sterne']];
const rows=[{night:'2026-01-01',filter:'Ha',frame:3,status:'error',fwhm_as:null,nstars:null,noise:null,moon_illum:null,moon_alt:null,flags:['Messung fehlgeschlagen']},
            {night:'<i>n</i>',filter:'Ha',frame:4,status:'weird<b>',fwhm_as:2.5,nstars:9,noise:1,moon_illum:3,moon_alt:-5,flags:[{level:'warn',text:'<b>x</b>'}]}];
console.log(JSON.stringify({rows:rows.map(f=>frameRowHtml(f,cols)),tips:rows.map(frameTip),opts:SOPTS}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_report_renders_error_and_unknown_status(tmp_path):
    pick = lambda pat: re.search(pat, REPORT, re.S).group(0)
    js = "\n".join([pick(r"const STATUS = [^\n]*"), pick(r"const st = [^\n]*"), pick(r"const flagTxt = [^\n]*"), pick(r"const fmt = [^\n]*"),
                    pick(r"const esc = [^\n]*"), pick(r"const SOPTS=[^\n]*"),
                    pick(r"function frameRowHtml\(.*?\n\}"), pick(r"function frameTip\(.*?\n\}")]) + REPORT_CASES
    f = tmp_path / "rep.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    assert "Messung fehlgeschlagen" in got["rows"][0] and "b-critical" in got["rows"][0]
    assert "Messung fehlgeschlagen" in got["opts"]
    assert "Messung fehlgeschlagen" in " ".join(got["tips"][0])
    assert "<b>" not in got["rows"][1] and "<i>" not in got["rows"][1]   # unbekannter Status escaped, kein Crash
    assert any("x" in t for t in got["tips"][1])                          # Flag-Objekte als Text
    assert "['error'," in REPORT.split("stLegend")[-1]           # Legende erklärt auch Messfehler


QUEUE_CASES = r"""
const dummy=new Proxy(function(){},{get:(t,k)=>['classList','style'].includes(k)?dummy:(['remove','toggle','removeAttribute','blur','add'].includes(k)?()=>{}:undefined),set:()=>true});
const $=()=>dummy, document={querySelector:()=>null,querySelectorAll:()=>[],activeElement:null};
class Image{set src(v){}}
const toast=()=>{}, ftag=x=>x, ic=()=>'';
const frameDlg={open:false,showModal(){this.open=true},close(){this.open=false}};
let P={name:'p',sources:{roots:['r']}}, cur={project:'p',tab:'frames'}, dirty=0;
const renderDirty=()=>{dirty++};
const srv={ov:{}}, PATHS=['a','b','c'];
const frames=()=>PATHS.map((p,i)=>({path:p,type:'light',night:'2026-01-0'+(i+1),status:'ok',auto_status:'ok',flags:[],metrics:{},
  selected:srv.ov[p]?srv.ov[p]==='in':true,selection_source:srv.ov[p]?'override':'auto'}));
let inflight=0, maxInflight=0, n=0, detailCalls=0;
function api(path,body){
  const delay=[60,5,40,1,30,2][n++%6];            // later calls would answer first if they overlapped
  inflight++;maxInflight=Math.max(maxInflight,inflight);
  return new Promise(res=>setTimeout(()=>{inflight--;
    if(path.endsWith('/override')){if(body.value)srv.ov[body.path]=body.value;else delete srv.ov[body.path];res(srv.ov);}
    else if(path.endsWith('/frames'))res({frames:frames(),plan_stale:true});
    else{detailCalls++;res({name:'p',sources:{roots:['r'],plan_stale:true},analysis_required:true});}},delay));
}
(async()=>{
  FV.list=frames();FV.project='p';
  setFrameUse(0,'out'); const dirtyNow=dirty;
  setFrameUse(0,'in'); setFrameUse(0,'out'); await setFrameUse(1,'out');
  const r1={max:maxInflight,dirtyNow,a:FV.list[0].selected,b:FV.list[1].selected,srvA:srv.ov.a,srvB:srv.ov.b,stale:!!P.analysis_required,detailCalls};
  // only-used filter: deselected frame leaves the lightbox walk, which moves to the next frame
  FV.use='in'; FV.view=fvFiltered(); frameDlg.open=true; FMODE='fv'; FV.pos=0;
  const before=FV.list[FV.view[0]].path;
  await setFrameUse(FV.view[0],'out');
  const r2={before,now:frameDlg.open?FV.list[FV.view[FV.pos]].path:null,view:FV.view.map(i=>FV.list[i].path).join('')};
  await setFrameUse(FV.view[0],'out');
  const r3={open:frameDlg.open,view:FV.view.length};
  console.log(JSON.stringify({r1,r2,r3,q:fvSortVal({status:'ok',auto_status:'error',flags:[]},'quality')}));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_frame_toggles_serialized_and_authoritative(tmp_path):
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "\n".join([pick(r"const esc=[^\n]*"), pick(r"const fmt=[^\n]*"), pick(r"const STXT=[^\n]*"),
                    "let FMODE='fv';", pick(r"const FTYPES=.*?(?=async function setThumb)")]) + QUEUE_CASES
    f = tmp_path / "queue.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-1200:]
    got = json.loads(r.stdout)
    r1 = got["r1"]
    assert r1["max"] == 1                                  # nie zwei Anfragen gleichzeitig
    assert r1["dirtyNow"] >= 1                             # Sperr-Banner sofort, vor der Antwort
    assert (r1["srvA"], r1["srvB"]) == ("out", "out")
    assert (r1["a"], r1["b"]) == (False, False)            # Anzeige = Serverstand
    assert r1["stale"] and r1["detailCalls"] >= 1          # Serverdetail bestätigt die Sperre
    assert got["r2"] == {"before": "c", "now": None, "view": ""} or got["r2"]["now"] != got["r2"]["before"]
    assert got["r2"]["before"] not in got["r2"]["view"]
    assert got["r3"] == {"open": False, "view": 0}        # leer gefiltert: Lightbox schließt
    assert got["q"] == 0                                  # Qualität nach automatischer Bewertung


def test_lightbox_hires_guarded_and_dirty_null_safe():
    body = _fn("renderFV")
    assert "FV.tok" in body and "frameDlg.open" in body
    assert "(P.sources||{})" in _fn("renderDirty")


ATTR_CASES = r"""
const evil="C:/Astro/O'Brien' onmouseover='alert(1)'/x\"y.fits";
const html=dirLink(evil,'L');
const m=html.match(/onclick='([^']*)'/);
const dec=s=>s.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
let got=null; const openDir=p=>{got=p};
new Function('openDir',dec(m[1]))(openDir);
console.log(JSON.stringify({quotes:(html.match(/'/g)||[]).length,got,ok:got===evil,mouse:/onmouseover=/.test(html.replace(/"[^"]*"|'[^']*'/g,''))}));   // attribute names outside quoted values
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_inline_handlers_escape_apostrophes(tmp_path):
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "\n".join([pick(r"const esc=[^\n]*"), pick(r"const ICONS=\{.*?\n\};"), pick(r"const ic=[^\n]*"),
                    pick(r"const jsArg=[^\n]*"), pick(r"const dirLink=[^\n]*")]) + ATTR_CASES
    f = tmp_path / "attr.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    assert got["quotes"] == 2 and not got["mouse"] and got["ok"]
    # kein Handler bettet JSON oder esc() roh in ein Attribut mit Apostrophen ein
    assert not re.search(r"onclick='[^'\n]*\$\{JSON\.stringify", APP)
    assert not re.search(r"onclick=\"[^\"\n]*\('\$\{esc\(", APP)
