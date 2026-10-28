"""Client QA report for one job: a single self-contained HTML file (thumbnails embedded), printable to PDF.

Frame table with verdict and client caveat, then every HOLD and CAVEAT station with its plan and photo,
the agent's reason, and the suggested fix when the model gave one.

    python -m survey_qa.report runs/<run>/results.jsonl out/report_<job>.html
"""
import base64
import html
import io
import json
import os
import sys
import time

from .agent import rollup
from .manifest import build

CSS = """
:root{--bg:#fff;--fg:#1b1b1b;--line:#ddd;--mut:#666;--send:#1f7a3a;--cav:#a86b00;--hold:#b3261e;--un:#666}
@media (prefers-color-scheme:dark){:root:not([data-print]){--bg:#141414;--fg:#e8e8e8;--line:#333;--mut:#999}}
body{margin:0 auto;max-width:1000px;padding:24px 16px;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
h1{font-size:22px;margin:0}h2{font-size:17px;margin:28px 0 8px}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%}td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.b{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px;font-weight:600}
.SEND{background:var(--send)}.CAVEAT{background:var(--cav)}.HOLD{background:var(--hold)}.UNASSESSED{background:var(--un)}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}.tile{flex:1 1 280px}
.st{border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0;break-inside:avoid}
.st img{width:100%;max-width:460px;border:1px solid var(--line)}.imgs{display:flex;gap:8px;flex-wrap:wrap}
.big{font-size:26px;font-weight:700}@media print{body{max-width:none}.st{page-break-inside:avoid}}
"""


def thumb(path, w=380):
    from PIL import Image
    try:
        im = Image.open(path)
    except OSError:
        return ""
    im.thumbnail((w, w * 2))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=60)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def esc(s):
    return html.escape(str(s or ""))


def badge(v):
    return f"<span class='b {esc(v)}'>{esc(v)}</span>"


def render(results, out, job=None):
    job = job or results[0]["job"]
    st = {s["station_id"]: s for s in build(job)["stations"]}
    frames = rollup(results)
    sizes = {}
    for s in st.values():
        sizes[s["frame"]] = sizes.get(s["frame"], 0) + 1
    for f in frames:  # a partly reviewed frame gets no client verdict
        if f["stations"] != sizes.get(f["frame"]):
            f["verdict"] = "UNASSESSED"
    counts = {v: sum(f["verdict"] == v for f in frames) for v in ("SEND", "CAVEAT", "HOLD", "UNASSESSED")}
    partial = [f["frame"] for f in frames if f["stations"] != sizes.get(f["frame"])]
    by_frame = {}
    for r in results:
        by_frame.setdefault(r["frame"], []).append(r)
    p = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
         f"<title>QA Report {esc(job)}</title><style>{CSS}</style>",
         f"<h1>Field QA report: {esc(job)}</h1>",
         f"<div class=mut>{len(results)} stations reviewed by {esc(results[0]['backend'])} · "
         f"generated {time.strftime('%Y-%m-%d %H:%M')} · machine review, a surveyor signs off</div>",
         "<div class=tiles>" + "".join(f"<div class=tile><div class=big>{counts[v]}</div>{badge(v)} frames</div>"
                                       for v in ("SEND", "CAVEAT", "HOLD", "UNASSESSED") if v != "UNASSESSED" or counts[v]) + "</div>"]
    if counts["UNASSESSED"] or partial:
        p.append(f"<p class=mut>Not assessed: {counts['UNASSESSED']} frame(s) with a review error or not fully reviewed; "
                 f"partly reviewed: {', '.join(partial) or 'none'}.</p>")
    p.append("<h2>Frames</h2><table><tr><th>Frame</th><th>Verdict</th><th>Stations S/C/H</th><th>Client note</th></tr>")
    for f in frames:
        c = f["counts"]
        p.append(f"<tr><td>{esc(f['frame'])}</td><td>{badge(f['verdict'])}</td>"
                 f"<td>{c['SEND']}/{c['CAVEAT']}/{c['HOLD']}</td><td>{esc(f['client_note'])}</td></tr>")
    p.append("</table>")
    for f in frames:
        bad = [r for r in by_frame[f["frame"]] if r["verdict"] in ("HOLD", "CAVEAT")]
        if not bad:
            continue
        p.append(f"<h2>{esc(f['frame'])} {badge(f['verdict'])}</h2>")
        for r in sorted(bad, key=lambda r: r["verdict"] != "HOLD"):
            s = st.get(r["station_id"], {})
            imgs = [x for x in [s.get("plan")] + (s.get("photos") or [])[:1] if x]
            fix = r.get("fix")
            fix_html = ""
            if isinstance(fix, dict) and fix:
                fix_html = ("<p><b>Suggested fix:</b> " + esc(fix.get("action")) + " " + esc(fix.get("layer"))
                            + (" - " + esc(fix.get("where")) if fix.get("where") else "") + "</p>")
            p.append(f"<div class=st><b>{esc(r['station_id'])}</b> {badge(r['verdict'])}"
                     f"<p>{esc(r['reason'])}</p>{fix_html}"
                     + (f"<p class=mut>Client note: {esc(r['client_note'])}</p>" if r.get("client_note") else "")
                     + "<div class=imgs>" + "".join(f"<img src='{thumb(x)}' alt='{esc(os.path.basename(x))}'>"
                                                   for x in imgs) + "</div></div>")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write("".join(p))
    return out


if __name__ == "__main__":
    rs = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    print(render(rs, sys.argv[2] if len(sys.argv) > 2 else f"out/report_{rs[0]['job']}.html"))
