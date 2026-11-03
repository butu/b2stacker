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
    assert "noch nicht analysiert" in APP
    # Ein Formular, ein Anlegen-Knopf – kein zweiter Planungs-Wizard.
    assert 'id="projName"' in APP and 'id="createBtn"' in APP
    assert "Planungsprojekt" not in APP
    assert "planBtn" not in APP


def test_frame_browser_wired_to_frames_api():
    assert "/frames'" in APP
    for label in ("Lights", "Darks", "Flats", "Bias", "Dark-Flats"):
        assert label in APP
    # Häkchen schickt echte Auswahl an die Planung, kein reines Anzeige-Flag
    assert re.search(r"/override',\{path:p,value:next\}", APP)


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
    assert "Alle Lights:" in APP and "verwendet:" in APP       # Gesamtstunden getrennt von genutzten
    assert "fvMissingMedia" in APP and "media_ready" in APP


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


def test_lightbox_snapshot_and_space_safe():
    assert "PhotoSwipe" in APP
    assert "frames:view.map(i=>Object.assign" in APP and "state.frames.map(f=>fvPswpItem" in APP   # Sichtungsliste wird beim Öffnen eingefroren
    assert "e.repeat" in _fn("fvKey")
    assert "fvToggleCur" in _fn("fvKey")
    assert "autoNext" not in APP and "nextOnToggle" not in APP


def test_selection_change_uses_small_delta_without_stale_or_reload():
    m = re.search(r"const needsAnalysis=.*", APP)
    assert m and "??" not in m.group(0)
    body = _fn("setFrameUse")
    assert "/override" in body and "r.frame" in body and "r.facts" in body
    assert "/frames" not in body and "api(base)" not in body
    assert "plan_stale=true" not in body and "dirty=true" not in body


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
const frames=()=>PATHS.map((p,i)=>({path:p,type:'light',exposure:60,night:'2026-01-0'+(i+1),status:'ok',auto_status:'ok',flags:[],metrics:{},
  selected:srv.ov[p]?srv.ov[p]==='in':true,selection_source:srv.ov[p]?'override':'auto'}));
let inflight=0, maxInflight=0, n=0, rev=0, calls=[];
function api(path,body){
  const delay=[60,5,40,1,30,2][n++%6];
  inflight++;maxInflight=Math.max(maxInflight,inflight);calls.push(path);
  return new Promise(res=>setTimeout(()=>{inflight--;
    if(path.endsWith('/override')){
      if(body.value)srv.ov[body.path]=body.value;else delete srv.ov[body.path];
      const row=frames().find(f=>f.path===body.path);
      res({revision:++rev,frame:row,facts:{lights_total:3,lights_used:PATHS.filter(p=>srv.ov[p]!=='out').length,hours_used:2.5},
        analysis_required:false,selection_ready:true});
    } else res({frames:frames(),plan_stale:true});
  },delay));
}
(async()=>{
  FV.list=frames();FV.project='p';FV.want.clear();FV._flush=false;
  // three synchronous clicks on the same frame must collapse to the last intent
  setFrameUse(0,'out'); setFrameUse(0,'in'); const p=setFrameUse(0,'out');
  setFrameUse(1,'out'); await p;
  const r1={max:maxInflight,a:FV.list[0].selected,b:FV.list[1].selected,srvA:srv.ov.a,srvB:srv.ov.b,
    fullGets:calls.filter(x=>x.endsWith('/frames')||x==='/api/project/p').length,
    overrideCalls:calls.filter(x=>x.endsWith('/override')).length,stale:!!P.analysis_required,
    planStale:!!(P.sources||{}).plan_stale,dirtyCalls:dirty,facts:{...(P.facts||{})}};
  // snapshot list: a toggle updates the frame and totals but never reorders the open walk
  FV.use='in'; FV.view=fvFiltered(); const snap=[...FV.view], before=snap.map(i=>FV.list[i].path).join('');
  await setFrameUse(FV.view[0],'out');
  const r2={before,after:FV.view.map(i=>FV.list[i].path).join(''),snap:snap.map(i=>FV.list[i].path).join(''),count:FV.view.length};
  console.log(JSON.stringify({r1,r2,q:fvSortVal({status:'ok',auto_status:'error',flags:[]},'quality')}));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_frame_toggles_delta_coalesced_without_full_reload(tmp_path):
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "\n".join([pick(r"const esc=[^\n]*"), pick(r"const fmt=[^\n]*"), pick(r"const STXT=[^\n]*"),
                    "let FMODE='legacy';", pick(r"const FTYPES=.*?(?=async function setThumb)")]) + QUEUE_CASES
    f = tmp_path / "queue.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-1200:]
    got = json.loads(r.stdout)
    r1 = got["r1"]
    assert r1["max"] == 1                                  # nie zwei Anfragen gleichzeitig
    assert r1["overrideCalls"] == 2                        # 3 Klicks auf a werden zu einem Intent
    assert (r1["srvA"], r1["srvB"]) == ("out", "out")
    assert (r1["a"], r1["b"]) == (False, False)            # Anzeige folgt dem Delta
    assert r1["fullGets"] == 0                             # kein ganzer Projekt-/Frame-Reload
    assert r1["stale"] is False and r1["planStale"] is False
    assert r1["facts"]["lights_used"] == 1                 # Facts lokal übernommen
    assert got["r2"]["after"] == got["r2"]["before"]        # Snapshot bleibt beim Sichten stabil
    assert got["r2"]["snap"] == got["r2"]["before"]
    assert got["q"] == 0                                   # Qualität nach automatischer Bewertung


def test_lightbox_zoom_and_full_image_guarded():
    assert "fvPswpItem" in APP and "preview_url" in APP and "fullUrl" in APP
    assert "s.currZoomLevel>s.zoomLevels.initial*1.02" in APP    # Full erst beim Zoom
    assert "FVS.saved" in APP and "correctPan" in APP            # Zoom + Ausschnitt bleiben erhalten
    assert "Ganzes Bild" in APP                                  # expliziter Reset
    assert "pswp.prev()" in APP and "pswp.next()" in APP         # Pfeile blättern
    assert "FVS.token" in APP and "FVS.closed" in APP            # Dispose/Identity-Guards
    # PhotoSwipe resets the outgoing slide before dispatching change; transition guards must
    # protect the last user snapshot from that reset.
    assert "slideDeactivate" in APP and "slideActivate" in APP
    assert "!e.slide.isActive" in APP
    assert "e.slide===pswp.currSlide" in APP
    assert "setTimeout(()=>{if(FVS===state&&!state.closed)fvApplyZoom" not in APP
    assert "FVS.view=p.currIndex" not in APP
    assert "preview_url" not in _fn("fvTile")
    assert "(P.sources||{})" in _fn("renderDirty")


ZOOM_CASES = r"""
let FVS={token:1,closed:false,saved:{z:2,cx:.25,cy:.75},applying:false,awaiting:true};
const calls=[];
const slide={width:1000,height:500,zoomLevels:{initial:1,max:8},currZoomLevel:1,pan:{x:0,y:0},
  bounds:{center:{x:0,y:0},correctPan:(axis,v)=>v},
  zoomTo(z){this.currZoomLevel=z;calls.push(['zoom',z])},
  applyCurrentZoomPan(){calls.push(['apply',this.pan.x,this.pan.y])}};
const pswp={currIndex:0,currSlide:slide};
function fvLoadFull(){calls.push(['full']);}
const snap=fvZoomSnapshot({currSlide:{width:1000,height:500,zoomLevels:{initial:1},currZoomLevel:2,pan:{x:500,y:-250},bounds:{center:{x:0,y:0}}}});
fvApplyZoom(pswp,1);
console.log(JSON.stringify({snap,z:slide.currZoomLevel,pan:slide.pan,await:FVS.awaiting,applying:FVS.applying,calls}));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_zoom_and_crop_persist_between_slides(tmp_path):
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "\n".join([pick(r"function fvZoomSnapshot.*?\n\}"), pick(r"function fvApplyZoom.*?\n\}")]) + ZOOM_CASES
    f = tmp_path / "zoom.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    assert abs(got["snap"]["cx"] - .25) < 1e-6 and abs(got["snap"]["cy"] - .75) < 1e-6
    assert got["z"] == 2 and got["pan"] == {"x": 500, "y": -250}
    assert got["await"] is False and got["applying"] is False
    assert got["calls"][0][0] == "zoom" and got["calls"][-1][0] == "full"


ASSET_PREP_CASES = r"""
const calls=[], toasts=[]; let cur={view:'overview',project:'Proj X'};
const api=(path,body)=>{calls.push({path,body});return Promise.resolve({title:'Bilder vorbereiten Proj X',kind:'assets'});};
const toast=m=>toasts.push(m); const refresh=async()=>{};
const btn={disabled:false};
(async()=>{ await prepareAssets(btn);
  console.log(JSON.stringify({calls,view:cur.view,disabled:btn.disabled,toasts}));})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_old_project_prepare_assets_uses_dedicated_route(tmp_path):
    assert "assets_migration_needed" in APP
    assert "Bildbestand noch nicht vorbereitet" in APP
    assert 'id="assetPrepareBtn"' in APP and "keine Bilder als vorhanden" in APP
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "const confirm=()=>true;\n" + pick(r"async function prepareAssets\(.*?\n\}") + ASSET_PREP_CASES
    f = tmp_path / "assets.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    got = json.loads(r.stdout)
    assert len(got["calls"]) == 1
    assert got["calls"][0]["path"] == "/api/project/Proj%20X/prepare-assets"
    assert got["calls"][0]["body"] == {}
    assert not any("reanalyze" in c["path"] for c in got["calls"])
    assert got["view"] == "job" and got["disabled"] is True


ERROR_CASES = r"""
const dummy=new Proxy(function(){},{get:(t,k)=>['classList','style'].includes(k)?dummy:(['remove','toggle','removeAttribute','blur','add'].includes(k)?()=>{}:undefined),set:()=>true});
const $=()=>dummy, document={querySelector:()=>null,querySelectorAll:()=>[],activeElement:null};
class Image{set src(v){}}
const toasts=[]; const toast=m=>toasts.push(String(m));
const renderDirty=()=>{};
const engName=()=>'PixInsight';
let P={name:'p',sources:{roots:['r']}}, cur={project:'p',tab:'frames'};
const FRAME={path:'a',type:'light',exposure:60,night:'2026-01-01',status:'ok',auto_status:'ok',flags:[],metrics:{},
  selected:true,selection_source:'auto',thumb_url:'/api/frame-thumb/p?path=a',preview_url:'/api/frame-preview/p?path=a'};
let responses=[], calls=[];
function delta(selected){return {revision:1,frame:{path:'a',selected,selection_source:'override',status:'ok',auto_status:'ok',flags:[],metrics:{}},
  facts:{lights_total:1,lights_used:selected?1:0},analysis_required:false,selection_ready:true};}
function api(path,body){
  calls.push(body.value);
  const r=responses.shift()||{ok:true,selected:body.value==='in'};
  return new Promise((res,rej)=>setTimeout(()=>r.ok?res(delta(r.selected??(body.value==='in'))):rej(new Error('Netz weg')),r.delay||1));
}
(async()=>{
  FV.list=[Object.assign({},FRAME)];FV.project='p';FV.server=new Map([['a',Object.assign({},FRAME)]]);FV.want.clear();FV._flush=false;FV.gen=0;
  responses=[{ok:false,delay:1}];
  await setFrameUse(0,'out');
  const failed={selected:FV.list[0].selected,source:FV.list[0].selection_source,thumb:FV.list[0].thumb_url,preview:FV.list[0].preview_url,toasts:toasts.length,calls:[...calls]};
  // a newer intent waits while the first POST fails: do not flash the old server state back
  FV.list=[Object.assign({},FRAME)];FV.server=new Map([['a',Object.assign({},FRAME)]]);FV.want.clear();FV._flush=false;FV.q=Promise.resolve();calls=[];
  responses=[{ok:false,delay:30},{ok:true,selected:true,delay:1}];
  const p1=setFrameUse(0,'out');
  await new Promise(r=>setTimeout(r,5));
  const p2=setFrameUse(0,'in');
  await p2;
  const raced={selected:FV.list[0].selected,source:FV.list[0].selection_source,calls:[...calls],thumb:FV.list[0].thumb_url,preview:FV.list[0].preview_url,toasts:toasts.length};
  // an open viewer survives list invalidation: its snapshot stays readable, toggles are blocked
  FVS={pswp:{currIndex:0},frames:[Object.assign({},FRAME)],strip:dummy,project:'p',gen:FV.gen,closed:false};
  calls=[];
  let threw=null;
  try{fvDropList();fvSyncStrip();fvToggleCur();}catch(e){threw=String(e);}
  const invalidated={threw,calls:[...calls],frames:FVS.frames.length,selected:FVS.frames[0].selected};
  console.log(JSON.stringify({failed,raced,invalidated}));
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node nicht installiert")
def test_toggle_post_failure_restores_server_state(tmp_path):
    pick = lambda pat: re.search(pat, APP, re.S).group(0)
    js = "\n".join([pick(r"const esc=[^\n]*"), pick(r"const fmt=[^\n]*"), pick(r"const STXT=[^\n]*"),
                    "let FMODE='legacy';", pick(r"const FTYPES=.*?(?=async function setThumb)")]) + ERROR_CASES
    f = tmp_path / "error.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-1200:]
    got = json.loads(r.stdout)
    assert got["failed"]["selected"] is True, "fehlgeschlagener POST zeigt wieder den Serverstand"
    assert got["failed"]["source"] == "auto"
    assert got["failed"]["toasts"] >= 1
    assert got["failed"]["thumb"] and got["failed"]["preview"], "Media-URLs bleiben erhalten"
    assert got["raced"]["selected"] is True, "neuer Intent gewinnt nach Fehler"
    assert got["raced"]["calls"] == ["out", "in"]
    assert got["raced"]["thumb"] and got["raced"]["preview"]
    assert got["invalidated"]["threw"] is None
    assert got["invalidated"]["calls"] == [], "invalidierter Viewer darf keinen falschen POST auslösen"
    assert got["invalidated"]["frames"] == 1 and got["invalidated"]["selected"] is True
    assert "if(!FV.list)" in _fn("fvRedraw")
    # Grid und Lightbox werden beim Wiederherstellen synchronisiert.
    assert "fvUpdateTile(k)" in _fn("fvRestoreFrame") and "fvSyncStrip()" in _fn("fvRestoreFrame")


def test_photo_swipe_assets_and_prepare_route_are_explicit():
    base = os.path.join(ROOT, "app", "vendor", "photoswipe")
    for name in ("photoswipe.umd.min.js", "photoswipe.css", "LICENSE"):
        assert os.path.isfile(os.path.join(base, name)), name
    assert "PhotoSwipe 5.4.4" in open(os.path.join(base, "photoswipe.umd.min.js"), encoding="utf-8").read(400)
    assert "/static/photoswipe.js" in APP and "/static/photoswipe.css" in APP
    assert "/prepare-previews" in APP
    assert "Vorschauen vorbereiten" in APP and "Vorschau nicht vorbereitet" in APP
    assert "app/vendor/photoswipe/*" in open(os.path.join(ROOT, "..", "pyproject.toml"), encoding="utf-8").read()
    assert 'app", "vendor", "photoswipe' in open(os.path.join(ROOT, "..", "packaging", "astrostack.spec"), encoding="utf-8").read()


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


def test_design_sections_and_review_keys():
    # Projektansicht nach Entwurf: drei Bereiche, Übersicht mit Analyse/Nächten aus /insights
    for label in ("'Übersicht'", "'Frames'", "'Stacken'"):
        assert label in APP
    assert "/insights'" in APP and "analysisModel" in APP and "nightsModel" in APP
    # Sichtung: V/N entscheiden und springen weiter, A setzt auf Automatik zurück; Leertaste bleibt Umschalter
    key = _fn("fvKey")
    assert "fvDecideCur" in key and "fvToggleCur" in key
    decide = _fn("fvDecideCur")
    assert "p.next()" in decide and "fvSetSnapshotUse(f,null)" in decide
    assert "p.next" not in _fn("fvToggleCur")
