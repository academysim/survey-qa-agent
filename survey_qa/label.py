"""Blinded labelling page: a human verdict per station, with no model output on the page.

Stations are sampled from jobs the agent was never tuned on, stratified by QA-flag ratio (not by any model
verdict), so the labels give an untuned accuracy number. The page saves progress in the browser and
downloads data/station_labels_<name>.csv when done.

    python -m survey_qa.label e57_curlew e57_park_blvd largo e57_gandy_us92 --per-job 15 --out out/label.html
"""
import argparse
import html
import json
import os
import random

from .manifest import build

CSS = """
:root{--bg:#f6f6f4;--fg:#1b1b1b;--card:#fff;--line:#ddd;--mut:#666;--send:#1f7a3a;--cav:#a86b00;--hold:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--card:#1e1e1e;--line:#333;--mut:#999}}
body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0}.mut{color:var(--mut)}main{padding:16px}
.imgs{display:flex;gap:8px;flex-wrap:wrap}.imgs img{max-height:62vh;max-width:100%;border:1px solid var(--line);cursor:zoom-in}
.imgs img.big{max-height:none;cursor:zoom-out}
.bar{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap;align-items:center}
button{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.v{color:#fff;border:0;font-weight:600}.SEND{background:var(--send)}.CAVEAT{background:var(--cav)}.HOLD{background:var(--hold)}
button.on{outline:3px solid var(--fg)}input{font:inherit;padding:7px;flex:1;min-width:200px;background:var(--card);color:var(--fg);border:1px solid var(--line)}
"""

JS = """
const D=JSON.parse(document.getElementById('d').textContent), KEY='label_'+D.name;
let L={};try{L=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
let i=0;const $=s=>document.querySelector(s);
function save(){try{localStorage.setItem(KEY,JSON.stringify(L))}catch(e){}
  $('#prog').textContent=Object.keys(L).length+' / '+D.st.length+' labelled'}
function show(){const s=D.st[i],l=L[s.key]||{};
  $('#where').textContent=(i+1)+' of '+D.st.length+' - '+s.job+' '+s.station_id;
  $('#imgs').innerHTML=s.imgs.map(p=>`<img src="file:///${p}" onclick="this.classList.toggle('big')">`).join('');
  document.querySelectorAll('button.v').forEach(b=>b.classList.toggle('on',b.dataset.v===l.v));
  $('#note').value=l.note||'';save()}
function pick(v){const s=D.st[i];L[s.key]={v,note:$('#note').value};save();if(i<D.st.length-1){i++;show()}else show()}
function csv(){const q=x=>'"'+String(x).replace(/"/g,'""')+'"';
  const rows=['job,frame,station_id,human_truth,note'].concat(D.st.filter(s=>L[s.key]).map(s=>
    [s.job,s.frame,s.station_id,L[s.key].v,q(L[s.key].note||'')].join(',')));
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([rows.join('\\n')+'\\n'],{type:'text/csv'}));
  a.download='station_labels_'+D.name+'.csv';a.click()}
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'&&e.key!=='Enter')return;
  if(e.key==='1')pick('SEND');if(e.key==='2')pick('CAVEAT');if(e.key==='3')pick('HOLD');
  if(e.key==='ArrowRight'&&i<D.st.length-1){i++;show()}if(e.key==='ArrowLeft'&&i>0){i--;show()}
  if(e.key==='Enter')e.target.blur()});
const f=D.st.findIndex(s=>!L[s.key]);i=f<0?0:f;show();
"""


def sample(jobs, per_job, seed=1027, exclude=()):
    rng = random.Random(seed)
    out = []
    for job in jobs:
        st = [s for s in build(job)["stations"] if s["plan"] and s["photos"]
              and f"{job} {s['station_id']}" not in exclude]
        ratio = lambda s: (s["qa_flags"] or {}).get("flagged", 0) / max((s["qa_flags"] or {}).get("bins", 0), 1)
        st.sort(key=ratio)
        # Three flag-ratio bands (low / mid / high), equal draws from each: blind to any model output.
        bands = [st[k * len(st) // 3:(k + 1) * len(st) // 3] for k in range(3)]
        n = [per_job // 3 + (k < per_job % 3) for k in range(3)]
        for band, k in zip(bands, n):
            out += rng.sample(band, min(k, len(band)))
    rng.shuffle(out)  # mix jobs and bands so the labeller sees no pattern
    return out


def render(stations, name, out):
    data = {"name": name, "st": [{"key": f"{s['job']}/{s['station_id']}", "job": s["job"], "frame": s["frame"],
                                  "station_id": s["station_id"], "imgs": [s["plan"]] + s["photos"]}
                                 for s in stations]}
    page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>Station Labelling</title><style>{CSS}</style>"
            f"<header><h1>Station labelling</h1><span id=where class=mut></span><span id=prog class=mut></span>"
            f"<button onclick=csv()>Download CSV</button></header><main>"
            f"<p class=mut>Is OUR CYAN linework on real markings, pavement edges and curbs? Judge as you would before "
            f"sending to the client. Keys: 1 SEND, 2 CAVEAT, 3 HOLD, arrows to move. Click an image to enlarge.</p>"
            f"<div class=bar><button class='v SEND' data-v=SEND onclick=\"pick('SEND')\">1 SEND</button>"
            f"<button class='v CAVEAT' data-v=CAVEAT onclick=\"pick('CAVEAT')\">2 CAVEAT</button>"
            f"<button class='v HOLD' data-v=HOLD onclick=\"pick('HOLD')\">3 HOLD</button>"
            f"<input id=note placeholder='optional note (what is wrong)'></div>"
            f"<div id=imgs class=imgs></div></main>"
            f"<script id=d type=application/json>{json.dumps(data).replace('</', '<\\/')}</script><script>{JS}</script>")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write(page)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("jobs", nargs="+")
    ap.add_argument("--per-job", type=int, default=15)
    ap.add_argument("--name", default="heldout1")
    ap.add_argument("--out", default="out/label.html")
    ap.add_argument("--exclude", help="stations file from an earlier sample; never re-sampled")
    ap.add_argument("--seed", type=int, default=1027)
    a = ap.parse_args()
    ex = set(open(a.exclude).read().splitlines()) if a.exclude else set()
    st = sample(a.jobs, a.per_job, a.seed, ex)
    open(os.path.join(os.path.dirname(a.out) or ".", f"{a.name}_stations.txt"), "w").write(
        "\n".join(f"{s['job']} {s['station_id']}" for s in st))
    print(render(st, a.name, a.out), len(st), "stations")
