"""Standalone interactive incident timeline: no CDNs, no external JS, escaped data."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from .rca import IncidentReport


def render_incident_html(report: IncidentReport) -> str:
    """Generate a self-contained HTML timeline with time scrubber and evidence navigation."""
    data = json.dumps(report.to_dict(), ensure_ascii=False, default=str)
    # Keep user-supplied log messages inert even inside an inline script.
    safe = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    service_count = len(report.by_service)
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TTD Incident Investigation</title>
<style>
:root{color-scheme:dark;font:15px/1.55 system-ui,-apple-system,'Segoe UI',sans-serif;
  --bg:#0e1524;--card:#172238;--muted:#aebfd4;--stroke:#334661;--ink:#f0f5ff;--gold:#ffd37c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink)}
main{max-width:1100px;margin:0 auto;padding:28px 18px 52px}h1{font-size:clamp(24px,4vw,34px);margin:0}
h2{font-size:18px;margin:0 0 12px}p{margin:7px 0 12px;color:var(--muted)}
header{margin-bottom:20px}.eyebrow{font-size:12px;font-weight:bold;letter-spacing:.14em;color:#80c5fc}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:18px 0}
.tile,.panel{border:1px solid var(--stroke);background:var(--card);border-radius:14px;padding:18px}
.tile b{display:block;font-size:21px}.tile small{color:var(--muted)}
.panel{margin:14px 0}.warning{border-left:4px solid var(--gold);padding:12px 16px;background:#312a1d;color:#ffe8ba;border-radius:8px}
small,.muted{color:var(--muted)}code{color:#b7dbff;overflow-wrap:anywhere}
input[type=range]{width:100%;accent-color:#68baff;margin:12px 0}
.controls{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.chip{border:1px solid #4c6884;border-radius:16px;padding:2px 9px;font-size:12px;color:#bedbf8}
button{border:1px solid #5379a4;border-radius:8px;background:#243e5e;color:white;padding:7px 10px;cursor:pointer}
button:hover,button:focus-visible{background:#376192}
.feed{margin:0;padding:0;list-style:none;max-height:540px;overflow:auto}
.feed li{display:grid;grid-template-columns:155px 86px 105px minmax(0,1fr);gap:9px;
  padding:9px;border-bottom:1px solid #334661;align-items:start;font-size:13px}
.feed li.active{background:#2b4768}.feed li.error{border-left:3px solid #f48b8b}
.feed .message{overflow-wrap:anywhere}.feed .date{font-variant-numeric:tabular-nums;color:#a7c9ef}
.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:670px}th{text-align:left;color:#b2c6dd}
th,td{border-bottom:1px solid var(--stroke);padding:10px 7px;vertical-align:top}td{font-size:13px}
ul.limitations{padding-left:23px;color:#aebfd4}
@media(max-width:740px){.feed li{grid-template-columns:1fr 1fr}.feed .message{grid-column:1/-1}}
</style></head><body><main>
<header><div class="eyebrow">TIME-TRAVEL DEBUGGER / INCIDENT INVESTIGATION</div>
<h1>Historical root-cause investigation</h1><p>Selected failure: <code id="selected-anchor"></code></p></header>
<div class="warning"><strong>Evidence, not proof.</strong> This report ranks potential precursors observed in logs.
It cannot determine a verified root cause from chronology or shared traces alone.</div>
<div class="grid"><div class="tile"><small>Observed events</small><b>""" + str(report.event_count) + """</b></div>
<div class="tile"><small>Services in window</small><b>""" + str(service_count) + """</b></div>
<div class="tile"><small>Candidate precursors</small><b>""" + str(len(report.evidence)) + """</b></div>
<div class="tile"><small>Explicit service links</small><b>""" + str(len(report.service_links)) + """</b></div></div>
<section class="panel"><h2>Investigation finding</h2><p id="finding"></p></section>
<section class="panel"><h2>Travel through time</h2>
<p>Move the slider or select an event. Events recorded after the chosen point remain hidden until you move forward.</p>
<div class="controls"><span id="current-date" class="chip"></span><span id="visible-count" class="chip"></span></div>
<input type="range" id="scrubber" min="0" step="1" aria-label="Choose a timeline event">
<div class="controls"><button id="previous">Previous event</button><button id="jump-error">Jump to selected error</button>
<button id="next">Next event</button></div>
<h2 style="margin-top:20px">Events up to selected time</h2><ul class="feed" id="feed"></ul>
<p id="omission"></p></section>
<section class="panel"><h2>Ranked precursor evidence</h2>
<p>Score = heuristic sorting only, NOT probability. Select evidence to jump to the recorded event.</p>
<div class="table-scroll"><table><thead><tr><th>Recorded at</th><th>Service</th><th>Link type</th><th>Score</th><th>Observation</th><th>Jump</th></tr></thead>
<tbody id="evidence"></tbody></table></div></section>
<section class="panel"><h2>Explicit service relationships</h2><p>Derived only from matching trace IDs and parent/child span IDs.</p>
<div id="links"></div></section>
<section class="panel"><h2>Important limitations</h2><ul class="limitations" id="limitations"></ul></section>
</main><script>""" + "const REPORT = " + safe + ";\n" + r"""
(function(){'use strict';
const $=id=>document.getElementById(id), doc=document;
const events=REPORT.timeline, scrub=$('scrubber'), anchor=REPORT.anchor;
$('selected-anchor').textContent = anchor.timestamp+' | '+anchor.service+' | '+anchor.message;
$('finding').textContent=REPORT.summary;
$('omission').textContent=REPORT.omitted_from_timeline ?
  `${REPORT.omitted_from_timeline} events omitted from the display limit (counts include them).` : '';
function node(tag,cls,txt){const n=doc.createElement(tag);if(cls)n.className=cls;if(txt!==undefined)n.textContent=String(txt);return n;}
const anchorAt=events.findIndex(e=>e.event_id===anchor.event_id);
scrub.max=String(Math.max(0,events.length-1));scrub.value=String(Math.max(anchorAt,0));
function update(){
 const position=Number(scrub.value), selected=events[position];
 const ul=$('feed');ul.replaceChildren();
 if(!selected){$('current-date').textContent='No events';return;}
 $('current-date').textContent=selected.timestamp;
 const visible=events.slice(0,position+1);
 $('visible-count').textContent=visible.length+' / '+events.length+' displayed';
 for(const e of visible){
   const li=node('li',(e.level==='ERROR'||e.level==='CRITICAL'||e.level==='FATAL')?'error':'');
   if(e.event_id===selected.event_id)li.className+=' active';
   li.append(node('span','date',e.timestamp.replace('T',' ').slice(0,23)),node('span','',e.level),node('span','',e.service));
   const msg=node('div','message');msg.append(node('strong','',e.message));
   if(e.is_evidence)msg.append(node('span','chip',' precursor evidence'));
   if(e.event_id===anchor.event_id)msg.append(node('span','chip',' selected failure'));
   msg.append(node('div','muted',e.event_id + (e.trace_id?' · trace '+e.trace_id:'')));
   li.append(msg);li.tabIndex=0;li.title='Select this event';
   li.addEventListener('click',()=>jump(e.event_id));
   ul.append(li);
 }
}
function jump(id){const i=events.findIndex(e=>e.event_id===id);if(i>=0){scrub.value=String(i);update();}}
scrub.addEventListener('input',update);
$('previous').addEventListener('click',()=>{scrub.value=String(Math.max(0,Number(scrub.value)-1));update();});
$('next').addEventListener('click',()=>{scrub.value=String(Math.min(events.length-1,Number(scrub.value)+1));update();});
$('jump-error').addEventListener('click',()=>jump(anchor.event_id));
for(const e of REPORT.evidence){
 const tr=node('tr');tr.append(node('td','',e.timestamp),node('td','',e.service),node('td','',e.strength),node('td','',e.score));
 const explanation=node('td');explanation.append(node('strong','',e.message),node('p','',e.explanation));tr.append(explanation);
 const td=node('td'),b=node('button','','View');b.addEventListener('click',()=>jump(e.event_id));td.append(b);tr.append(td);$('evidence').append(tr);
}
if(!REPORT.evidence.length){$('evidence').append(node('tr','','No candidate precursor events recorded.'));}
for(const l of REPORT.service_links){$('links').append(node('p','',l.caller+' → '+l.callee+' | trace '+l.trace_id));}
if(!REPORT.service_links.length)$('links').textContent='No explicit cross-service parent/child spans found in this window.';
for(const line of REPORT.limitations)$('limitations').append(node('li','',line));
update();
})();
""" + "</script></body></html>"


def write_incident_html(report: IncidentReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_incident_html(report), encoding="utf-8")
    return path
