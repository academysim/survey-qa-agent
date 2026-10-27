"""Static HTML viewer for a run: station list, plan | photos side by side, verdict + reason, frame roll-up.
Images are linked by local file path, never copied, so the page stays out of git with the data."""
import html
import json
import os
import sys

from .agent import rollup
from .manifest import build

CSS = """
:root{--bg:#f6f6f4;--fg:#1b1b1b;--card:#fff;--line:#ddd;--send:#1f7a3a;--cav:#a86b00;--hold:#b3261e;--mut:#666}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--card:#1e1e1e;--line:#333;--mut:#999}}
body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px;border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 4px}
.wrap{display:grid;grid-template-columns:300px 1fr;height:calc(100vh - 70px)}
nav{overflow:auto;border-right:1px solid var(--line)}nav div{padding:6px 12px;cursor:pointer;border-bottom:1px solid var(--line)}
nav div.on{background:var(--card)}nav .fr{font-weight:600;background:transparent;cursor:default}
main{overflow:auto;padding:16px}.imgs{display:flex;gap:8px;flex-wrap:wrap}.imgs img{max-height:420px;max-width:100%;border:1px solid var(--line)}
.b{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px;font-weight:600}
.SEND{background:var(--send)}.CAVEAT{background:var(--cav)}.HOLD{background:var(--hold)}.mut{color:var(--mut)}
table{border-collapse:collapse}td,th{padding:3px 10px;border-bottom:1px solid var(--line);text-align:left}
"""

JS = """
const D=JSON.parse(document.getElementById('d').textContent);
const nav=document.querySelector('nav'),main=document.querySelector('main');
const b=v=>`<span class="b ${v}">${v}</span>`;
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let last=null;
D.results.forEach((r,i)=>{
  if(r.frame!==last){const f=D.frames.find(x=>x.frame===r.frame);const h=document.createElement('div');
    h.className='fr';h.innerHTML=`${r.frame} ${f?b(f.verdict):''} <span class="mut">${f?f.stations:''} st</span>`;nav.append(h);last=r.frame;}
  const d=document.createElement('div');d.innerHTML=`${b(r.verdict)} ${r.station_id}`;d.onclick=()=>show(i,d);nav.append(d);});
function show(i,el){document.querySelectorAll('nav div.on').forEach(x=>x.classList.remove('on'));el&&el.classList.add('on');
  const r=D.results[i],s=D.st[r.station_id]||{};
  const imgs=[s.plan,...(s.photos||[])].filter(Boolean).map(p=>`<img src="${'file:///'+p}" title="${p}">`).join('');
  const steps=(r.steps||[]).map(x=>`<li>${x.tool?('asked for <b>'+x.tool+'</b>: '+esc(x.why)):'verdict'}${x.note?' - '+esc(x.note):''}</li>`).join('');
  main.innerHTML=`<h2>${r.station_id} ${b(r.verdict)} <span class="mut">${esc(r.backend)}${r.confidence!=null?' · conf '+r.confidence:''}</span></h2>
  <p>${esc(r.reason)}</p>${r.client_note?`<p><b>Client note:</b> ${esc(r.client_note)}</p>`:''}
  <p class="mut">Agent steps:</p><ol>${steps}</ol><div class="imgs">${imgs||'<i>images not found locally</i>'}</div>`;}
main.innerHTML=`<h2>Frame roll-up</h2><table><tr><th>Frame</th><th>Verdict</th><th>SEND/CAVEAT/HOLD</th><th>Note</th></tr>`+
 D.frames.map(f=>`<tr><td>${f.frame}</td><td>${b(f.verdict)}</td><td>${f.counts.SEND}/${f.counts.CAVEAT}/${f.counts.HOLD}</td><td>${esc(f.client_note)}</td></tr>`).join('')+
 `</table>${D.score?'<h2>Score vs human labels</h2><pre>'+esc(JSON.stringify(D.score,null,1))+'</pre>':''}`;
"""


def load_run(path):
    if path.endswith(".jsonl"):
        results = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        return {"job": results[0]["job"], "backend": results[0]["backend"], "results": results,
                "frames": rollup(results)}
    return json.load(open(path, encoding="utf-8"))


def render(run, out, score=None):
    man = build(run["job"])
    st = {s["station_id"]: {"plan": s["plan"], "photos": s["photos"]} for s in man["stations"]}
    data = dict(run, st=st, score=score)
    title = f"Survey QA - {run['job']}"
    sub = f"{len(run['results'])} stations · backend {run['backend']}"
    if run.get("budget"):
        sub += f" · {run['budget']['input_tokens'] + run['budget']['output_tokens']} tokens"
    page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style>"
            f"<header><h1>{html.escape(title)}</h1><div class=mut>{html.escape(sub)}</div></header>"
            f"<div class=wrap><nav></nav><main></main></div>"
            f"<script id=d type=application/json>{json.dumps(data).replace('</', '<\\/')}</script><script>{JS}</script>")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write(page)
    return out


if __name__ == "__main__":
    print(render(load_run(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "out/viewer.html"))
