"""Agent-trace demo page: pick a station and step through what the agent did, turn by turn.

Replays recorded results (no API key in the browser): first look at PLAN + PHOTO, each extra piece of
evidence the model asked for and why, then the verdict with its defect boxes and the suggested fix.
One self-contained HTML file; images are embedded thumbnails.

    python -m survey_qa.trace runs/<run>/results.jsonl out/trace_<job>.html [--max 12]
"""
import argparse
import json
import os

from .report import LABELS, esc, normalize_result, order_key, stations_of, thumb

CSS = """
:root{--bg:#0f1416;--card:#172024;--fg:#e8eef0;--mut:#8fa3aa;--line:#2a373c;--accent:#7fc4d4;--hold:#e0564c;--cav:#e0a43a;--send:#4cbf75}
@media (prefers-color-scheme:light){:root:not([data-theme=dark]){--bg:#f4f6f6;--card:#fff;--fg:#14191b;--mut:#5b6b70;--line:#d9e0e2;--accent:#0f4c5c}}
*{box-sizing:border-box}body{overflow-x:hidden;margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header .k{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent)}
select,button{font:inherit;padding:7px 12px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.p{background:var(--accent);color:#06232b;border:0;font-weight:600}
main{display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
@media (max-width:820px){main{grid-template-columns:1fr}}
main>div{min-width:0}.still .fig.on{animation:none}.still .box{transition:none}
.turns{display:flex;flex-direction:column;gap:8px}
.turn{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:6px;padding:10px 12px;opacity:.35;transition:opacity .3s}
.turn.on{opacity:1;border-left-color:var(--accent)}.turn .h{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:1px}
.turn .tool{font-family:ui-monospace,Consolas,monospace;color:var(--accent)}
.v{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;font-weight:700;font-size:13px}
.HOLD{background:var(--hold)}.CAVEAT{background:var(--cav)}.SEND{background:var(--send)}
.ev{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start}
.fig{position:relative;flex:1 1 240px;max-width:520px;min-width:0;display:none}.fig.on{display:block;animation:in .35s}
.fig img{width:100%;display:block;border:1px solid var(--line);border-radius:4px}.fig .cap{font-size:12px;color:var(--mut)}
.box{position:absolute;border:2px solid #ff3b30;box-shadow:0 0 0 1px #0008;opacity:0;transition:opacity .4s}.show .box{opacity:1}
.box span{position:absolute;top:-18px;left:-2px;background:#ff3b30;color:#fff;font-size:10px;padding:0 4px;white-space:nowrap}
.flags{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;font-family:ui-monospace,Consolas,monospace;font-size:12px;display:none;flex:1 1 220px}
.flags.on{display:block}.mut{color:var(--mut)}.nav{display:flex;gap:8px;margin:8px 0}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
"""

JS = """
const D=JSON.parse(document.getElementById('d').textContent);let S=0,T=0;const $=s=>document.querySelector(s);
function draw(){const s=D[S];
  $('#turns').innerHTML=s.turns.map((t,i)=>`<div class="turn ${i<=T?'on':''}"><div class=h>Turn ${i+1}</div>${t.html}</div>`).join('');
  $('#ev').innerHTML=s.ev.map((e,i)=>e.img?`<div class="fig ${e.at<=T?'on':''} ${T==s.turns.length-1?'show':''}"><div class=cap>${e.label}</div><div style=position:relative><img src="${e.img}">${
    e.boxes.map(b=>`<div class=box style="left:${b.box[0]*100}%;top:${b.box[1]*100}%;width:${(b.box[2]-b.box[0])*100}%;height:${(b.box[3]-b.box[1])*100}%"><span style="${b.box[0]>0.55?'right:-2px;left:auto':''}">${b.label}</span></div>`).join('')}</div></div>`
    :`<div class="flags ${e.at<=T?'on':''}"><div class=cap>${e.label}</div>${e.text}</div>`).join('');
  $('#pos').textContent=`turn ${T+1} of ${s.turns.length}`}
function pick(i){S=+i;T=0;draw()}function step(d){T=Math.max(0,Math.min(D[S].turns.length-1,T+d));draw()}
let timer=null;function play(){clearInterval(timer);T=0;draw();timer=setInterval(()=>{if(T>=D[S].turns.length-1){clearInterval(timer);return}step(1)},2200)}
$('#sel').innerHTML=D.map((s,i)=>`<option value=${i}>${s.id} - ${s.verdict}</option>`).join('');
document.addEventListener('keydown',e=>{if(e.key==='ArrowRight')step(1);if(e.key==='ArrowLeft')step(-1);if(e.key===' '){e.preventDefault();play()}});
if(location.hash==='#end'){T=D[S].turns.length-1;document.body.classList.add('still')}  // open on the verdict, for stills
draw();
"""

KIND_LABEL = {"plan": "PLAN (scan + drafted linework)", "photo": "PHOTO 1", "second_photo": "PHOTO 2",
              "neighbour": "NEIGHBOUR PLAN", "qa_flags": "QA FLAGS"}


def station_trace(r, s):
    """Turns and evidence for one station. Evidence item k appears at the turn that requested it."""
    imgs = list(r.get("images") or [])
    ev, turns, img_i = [], [], 0
    kinds = r["evidence"]
    # Leading plan/photo items were shown up front; each later item came from one tool call, in step order.
    n0 = next((k for k, kind in enumerate(kinds) if kind not in ("plan", "photo")), len(kinds))
    for k, kind in enumerate(kinds):
        at = 0 if k < n0 else k - n0 + 1
        if kind == "qa_flags":
            ev.append({"at": at, "label": KIND_LABEL[kind], "text": esc(json.dumps(s.get("qa_flags")))})
        elif kind.startswith("refused_"):
            continue
        elif img_i < len(imgs):
            boxes = [{"box": d["box"], "label": f"{d['severity']} {LABELS[d['class']].lower()}"}
                     for d in r.get("defects") or [] if d["image"] == img_i]
            ev.append({"at": at, "label": KIND_LABEL.get(kind, kind), "img": thumb(imgs[img_i], 520), "boxes": boxes})
            img_i += 1
    if not r["steps"]:  # hybrid geometry-only decision: the model never saw this station
        return {"id": r["station_id"], "verdict": r["verdict"], "ev": [],
                "turns": [{"html": f"<b>Geometry check only</b>, no model call.<p><span class='v {esc(r['verdict'])}'>"
                                   f"{esc(r['verdict'])}</span></p><p>{esc(r['reason'])}</p>"}]}
    turns.append({"html": "<b>Looks at</b> the plan render and the first street photo."})
    for st in r["steps"]:
        if st.get("tool"):
            turns[-1]["html"] += (f"<p>Not sure yet. Asks for <span class=tool>{esc(st['tool'])}</span>: "
                                  f"<i>{esc(st.get('why'))}</i></p>")
            turns.append({"html": f"<b>Gets</b> {esc(KIND_LABEL.get(st['tool'], st['tool']))}."})
    fix = r.get("fix") or {}
    fx = (f"<p><b>Fix:</b> {esc(fix.get('action'))} {esc(fix.get('layer'))} - {esc(fix.get('where'))}</p>" if fix else "")
    tok = sum(st["usage"].get("input_tokens", 0) + st["usage"].get("output_tokens", 0) for st in r["steps"])
    turns[-1]["html"] += (f"<p><span class='v {esc(r['verdict'])}'>{esc(r['verdict'])}</span> "
                          f"<span class=mut>confidence {r.get('confidence') or 0:.2f}</span></p><p>{esc(r['reason'])}</p>{fx}"
                          f"<p class=mut>{len(r['steps'])} model call(s), {tok:,} tokens</p>")
    return {"id": r["station_id"], "verdict": r["verdict"], "turns": turns, "ev": ev}


def pick(results, n):
    """Most instructive first: stations that asked for evidence and came back with boxes; then a clean SEND."""
    ok = [r for r in results if not r.get("error")]
    rich = sorted((r for r in ok if r["verdict"] != "SEND"),
                  key=lambda r: (-(len(r["steps"]) > 1), -bool(r.get("defects")), order_key(r)))
    sends = [r for r in ok if r["verdict"] == "SEND"]
    return rich[:n - min(2, len(sends))] + sends[:2]


def render(results, out, n=12):
    results = [normalize_result(r) for r in results]
    st = stations_of(results[0]["job"])
    data = [station_trace(r, st.get(r["station_id"], {})) for r in pick(results, n)]
    page = (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Agent Trace</title><style>{CSS}</style><body>"
            f"<header><div><div class=k>Survey QA agent · replay</div><h1>{esc(results[0]['job'])}: how the agent decides</h1></div>"
            f"<select id=sel onchange=pick(this.value)></select><button class=p onclick=play()>Play</button>"
            f"<button onclick=step(-1)>Back</button><button onclick=step(1)>Next</button><span id=pos class=mut></span></header>"
            f"<main><div id=turns class=turns></div><div id=ev class=ev></div></main>"
            f"<script id=d type=application/json>{json.dumps(data).replace('</', '<' + chr(92) + '/')}</script><script>{JS}</script>")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write(page)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("out")
    ap.add_argument("--max", type=int, default=12)
    a = ap.parse_args()
    rs = [json.loads(l) for l in open(a.results, encoding="utf-8") if l.strip()]
    print(render(rs, a.out, a.max))
