"""Client QA report for one job: a single self-contained HTML file (thumbnails embedded), printable to PDF.

Header tiles, a defect-class bar chart, a per-frame heat strip, the frame table, the drafter work queue
(HOLD/CAVEAT stations grouped by defect class), then every HOLD and CAVEAT station worst first with its
plan and photo, the model's defect boxes drawn on them, the reason and the suggested fix.

    python -m survey_qa.report runs/<run>/results.jsonl out/report_<job>.html
    python -m survey_qa.report --index out/index.html runs/*/results.jsonl   # job dashboard
"""
import base64
import csv
import html
import io
import json
import os
import re
import sys
import time

from .agent import rollup
from .backends import DEFECT_CLASSES
from .manifest import build

LABELS = {"grass": "Lines in grass", "canopy": "Lines in trees", "elevation_jump": "Elevation jump",
          "lane_cutoff": "Lane line cut off / missing", "car_jumble": "Jumble where cars were",
          "gore": "Gore / cross-hatch", "crosswalk": "Crosswalk", "parking_stop": "Parking stops drafted",
          "flow_line": "Flow line on asphalt", "median_end": "Median end shape", "other": "Other"}
FIXES = {"grass": "Delete linework in grass and landscaping; nothing off pavement is mapped.",
         "canopy": "Delete lines picked up from tree canopy; keep only the ground line beneath.",
         "elevation_jump": "Drop the vertices that climb poles or signs back to road level, or delete them.",
         "lane_cutoff": "Extend or add the lane line so it runs continuously through the station.",
         "car_jumble": "Replace the tangle of short segments with one clean lane line.",
         "gore": "Trace the gore outline and its cross-hatching.",
         "crosswalk": "Redraw the crosswalk with the full set of bars; remove extras.",
         "parking_stop": "Delete parking/wheel stops drafted as linework.",
         "flow_line": "Delete the CGep flow line where asphalt meets the curb face (no gutter).",
         "median_end": "Redraw the median end to the real shape (e.g. bullnose), single clean edge.",
         "other": "Review the station and correct the linework."}
# Fallback for results from prompts before v5, which had no "defects" field: map the reason/fix text.
KEYWORDS = [("parking_stop", r"parking|wheel stop"), ("crosswalk", r"crosswalk"), ("gore", r"gore|hatch|chevron"),
            ("flow_line", r"flow ?line|cgep|gutter"), ("median_end", r"median|bullnose"),
            ("canopy", r"tree|branch|canopy|palm|foliage|trunk"), ("grass", r"grass|landscap|bush|lawn|vegetat|shrub"),
            ("elevation_jump", r"climb|pole|sign|vertical|elevat|jump"),
            ("car_jumble", r"\bcars?\b|vehicle|jumble|tangle"),
            ("lane_cutoff", r"stops? (short|abrupt)|cut off|cut-off|missing|incomplete|gap|ends abrupt|start")]
SEV_RANK = {"critical": 3, "major": 2, "minor": 1}

CSS = """
:root{--bg:#fbfaf7;--card:#fff;--fg:#1b1b1b;--line:#e2dfd8;--mut:#6b6860;--send:#1f7a3a;--cav:#a86b00;--hold:#b3261e;
--un:#7a7a7a;--accent:#0f4c5c;--bar:#0f4c5c}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121212;--card:#1b1b1b;--fg:#e8e6e1;--line:#333;
--mut:#9a978f;--accent:#7fc4d4;--bar:#5aa6b8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px 40px}
header.brand{background:var(--accent);color:#fff;padding:18px 0;margin-bottom:20px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) header.brand{color:#0b1d22}}
header.brand .wrap{padding-bottom:0;display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}
header.brand h1{font-size:22px;margin:0;letter-spacing:.2px}header.brand .k{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;opacity:.8}
h2{font-size:16px;margin:30px 0 10px}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%;background:var(--card)}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}
.b{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px;font-weight:600;white-space:nowrap}
.SEND{background:var(--send)}.CAVEAT{background:var(--cav)}.HOLD{background:var(--hold)}.UNASSESSED{background:var(--un)}
.sev{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.sev.critical{color:var(--hold)}.sev.major{color:var(--cav)}.sev.minor{color:var(--mut)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.big{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
.bars{display:grid;grid-template-columns:minmax(120px,210px) 1fr 40px;gap:4px 10px;align-items:center}
.bars .t{height:14px;background:var(--line);border-radius:3px;overflow:hidden}.bars .t i{display:block;height:100%;background:var(--bar)}
.bars .n{font-variant-numeric:tabular-nums;text-align:right}
.strip{display:flex;flex-direction:column;gap:3px}.row{display:flex;align-items:center;gap:8px}
.row .lab{width:44px;font-size:12px;color:var(--mut)}.cells{display:flex;gap:2px;flex-wrap:wrap}
.cells span{width:12px;height:18px;border-radius:2px;display:inline-block}
.legend{display:flex;gap:12px;font-size:12px;margin-top:6px}.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px;background:var(--c)}
.st{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0;break-inside:avoid}
.st img{width:100%;max-width:470px;border:1px solid var(--line)}.imgs{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}.chip{border:1px solid var(--line);border-radius:12px;padding:0 8px;font-size:12px}
.q td:first-child{white-space:nowrap}
a{color:var(--accent)}
@media print{body{background:#fff;color:#000}header.brand{background:#fff;color:#000;border-bottom:3px solid #0f4c5c}
.wrap{max-width:none}.st,.tile,table{break-inside:avoid;border-color:#ccc}h2{break-after:avoid}}
"""
MISSING = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' width='420' height='120'%3E%3Crect width='100%25' "
           "height='100%25' fill='%23888' fill-opacity='.15'/%3E%3Ctext x='50%25' y='50%25' text-anchor='middle' fill='%23888' "
           "font-family='sans-serif' font-size='14'%3Eimage unavailable%3C/text%3E%3C/svg%3E")
COLORS = {"SEND": "var(--send)", "CAVEAT": "var(--cav)", "HOLD": "var(--hold)", "UNASSESSED": "var(--un)"}


def thumb(path, w=420, boxes=()):
    from PIL import Image, ImageDraw
    try:
        im = Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return MISSING
    im.thumbnail((w, w * 2))
    if boxes:
        d = ImageDraw.Draw(im)
        W, H = im.size
        for b in boxes:
            x0, y0, x1, y1 = b["box"]
            lw = 3 if b["severity"] == "critical" else 2
            d.rectangle([x0 * W, y0 * H, x1 * W, y1 * H], outline=(230, 30, 30), width=lw)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=62)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def esc(s):
    return html.escape(str(s or ""))


def badge(v):
    return f"<span class='b {esc(v)}'>{esc(v)}</span>"


def normalize_result(r):
    """Every saved-results consumer goes through this: tolerate old, partial or hand-edited JSONL."""
    from .agent import _conf
    from .backends import SEVERITIES
    r = dict(r)
    r["confidence"] = _conf(r.get("confidence"))
    r["verdict"] = r.get("verdict") if r.get("verdict") in ("SEND", "CAVEAT", "HOLD") else "HOLD"
    for k in ("evidence", "steps", "images", "defects"):
        r[k] = r.get(k) if isinstance(r.get(k), list) else []
    r["steps"] = [st if isinstance(st, dict) else {} for st in r["steps"]]
    for st in r["steps"]:
        st["usage"] = st.get("usage") if isinstance(st.get("usage"), dict) else {}
    r["defects"] = [dict(d, **{"class": d.get("class") if d.get("class") in DEFECT_CLASSES else "other",
                               "severity": d.get("severity") if d.get("severity") in SEVERITIES else "major"})
                    for d in r["defects"] if isinstance(d, dict) and isinstance(d.get("box"), list)
                    and len(d["box"]) == 4 and isinstance(d.get("image"), int)]
    r["fix"] = r.get("fix") if isinstance(r.get("fix"), dict) else None
    for k in ("reason", "client_note", "station_id", "frame", "job", "backend"):
        r[k] = str(r.get(k) or "")
    return r


def stations_of(job):
    """Manifest stations keyed by id, or {} when the drafts folder is not available (archived results)."""
    try:
        return {s["station_id"]: s for s in build(job)["stations"]}
    except OSError:
        return {}


def classes_of(r):
    """Defect classes for one HOLD/CAVEAT station: the model's own list (v5+), else keyword mapping."""
    if r.get("defects"):
        return sorted({d["class"] for d in r["defects"]}, key=DEFECT_CLASSES.index)
    fix = r.get("fix") or {}
    text = " ".join([r.get("reason") or "", str(fix.get("where") or ""), str(fix.get("layer") or "")]).lower()
    hits = [c for c, pat in KEYWORDS if re.search(pat, text)]
    return hits or ["other"]


def worst_sev(r):
    return max((SEV_RANK[d["severity"]] for d in r.get("defects") or []), default=0)


def order_key(r):
    """Worst first: HOLD before CAVEAT, then highest defect severity, then confidence."""
    return (r["verdict"] != "HOLD", -worst_sev(r), -(r.get("confidence") or 0))


def workqueue(results):
    """Group HOLD/CAVEAT stations by defect class. Classes ordered by summed severity, then count."""
    q = {}
    for r in results:
        if r["verdict"] not in ("HOLD", "CAVEAT") or r.get("error"):
            continue
        for c in classes_of(r):
            q.setdefault(c, []).append(r)
    rows = []
    for c, rs in q.items():
        rs = sorted(rs, key=order_key)
        rows.append({"class": c, "label": LABELS[c], "count": len(rs), "fix": FIXES[c],
                     "severity": sum(max(worst_sev(r), 2 if r["verdict"] == "HOLD" else 1) for r in rs),
                     "stations": [r["station_id"] for r in rs]})
    return sorted(rows, key=lambda x: (-x["severity"], -x["count"], x["class"]))


def write_workqueue_csv(rows, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["class", "label", "count", "severity_score", "fix", "stations"])
        for r in rows:
            w.writerow([r["class"], r["label"], r["count"], r["severity"], r["fix"], " ".join(r["stations"])])
    return path


def summarize(results, job=None, sizes=None):
    """Frame roll-up with partial frames marked UNASSESSED, plus headline counts. Used by report and index."""
    job = job or results[0]["job"]
    if sizes is None:
        sizes = {}
        for s in (stations_of(job) or {r["station_id"]: r for r in results}).values():
            sizes[s["frame"]] = sizes.get(s["frame"], 0) + 1
    frames = rollup(results)
    for f in frames:  # a partly reviewed frame gets no client verdict
        if f["stations"] != sizes.get(f["frame"]):
            f["verdict"] = "UNASSESSED"
    fc = {v: sum(f["verdict"] == v for f in frames) for v in COLORS}
    sc = {v: sum(r["verdict"] == v and not r.get("error") for r in results) for v in ("SEND", "CAVEAT", "HOLD")}
    sc["error"] = sum(bool(r.get("error")) for r in results)
    toks = sum(s["usage"].get("input_tokens", 0) + s["usage"].get("output_tokens", 0)
               for r in results for s in r.get("steps") or [])
    return {"job": job, "frames": frames, "frame_counts": fc, "station_counts": sc, "tokens": toks,
            "backend": results[0]["backend"], "reviewed": len(results), "planned": sum(sizes.values()),
            "partial": [f["frame"] for f in frames if f["stations"] != sizes.get(f["frame"])]}


def bar_chart(rows):
    if not rows:
        return "<p class=mut>No defects recorded.</p>"
    top = max(r["count"] for r in rows)
    return "<div class=bars>" + "".join(
        f"<div>{esc(r['label'])}</div><div class=t><i style='width:{100 * r['count'] / top:.1f}%'></i></div>"
        f"<div class=n>{r['count']}</div>" for r in rows) + "</div>"


def heat_strip(results):
    by = {}
    for r in results:
        by.setdefault(r["frame"], []).append(r)
    out = ["<div class=strip>"]
    for fr in sorted(by, key=lambda f: int(f[1:])):
        cells = "".join(f"<span title='{esc(r['station_id'])}: {'ERROR' if r.get('error') else r['verdict']}' "
                        f"style='background:{COLORS['UNASSESSED' if r.get('error') else r['verdict']]}'></span>"
                        for r in sorted(by[fr], key=lambda r: r["station_id"]))
        out.append(f"<div class=row><div class=lab>{esc(fr)}</div><div class=cells>{cells}</div></div>")
    out.append("</div><div class=legend>" + "".join(f"<span style='--c:{COLORS[v]}'>{v.title() if v != 'UNASSESSED' else 'Error'}</span>"
                                                    for v in COLORS) + "</div>")
    return "".join(out)


def render(results, out, job=None, queue_csv=None):
    results = [normalize_result(r) for r in results]
    job = job or results[0]["job"]
    st = stations_of(job)
    sizes = {}
    for s in (st or {r["station_id"]: r for r in results}).values():
        sizes[s["frame"]] = sizes.get(s["frame"], 0) + 1
    S = summarize(results, job, sizes)
    frames, fc, sc = S["frames"], S["frame_counts"], S["station_counts"]
    queue = workqueue(results)
    write_workqueue_csv(queue, queue_csv or os.path.join(os.path.dirname(out) or ".", f"workqueue_{job}.csv"))
    model_defects = any(r.get("defects") for r in results)
    p = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>QA Report {esc(job)}</title><style>{CSS}</style><body>",
         f"<header class=brand><div class=wrap><div><div class=k>Field QA report</div><h1>{esc(job)}</h1></div>"
         f"<div class=k>{esc(S['backend'])} · {time.strftime('%Y-%m-%d %H:%M')}</div></div></header><div class=wrap>",
         f"<p class=mut>{S['reviewed']} of {S['planned']} stations reviewed by machine. A surveyor signs off before anything is sent.</p>",
         "<div class=tiles>"
         + "".join(f"<div class=tile><div class=big>{fc[v]}</div>{badge(v)} frames</div>"
                   for v in COLORS if v != "UNASSESSED" or fc[v])
         + f"<div class=tile><div class=big>{sc['HOLD']}</div>stations on hold</div>"
         + f"<div class=tile><div class=big>{sc['SEND']}</div>stations clean</div></div>"]
    if fc["UNASSESSED"] or S["partial"]:
        p.append(f"<p class=mut>Not assessed: {fc['UNASSESSED']} frame(s) with a review error or not fully reviewed; "
                 f"partly reviewed: {', '.join(S['partial']) or 'none'}.</p>")
    p.append("<h2>Defects by type</h2>" + bar_chart(queue)
             + ("" if model_defects else "<p class=mut>Classes mapped from the review text (results predate per-defect output).</p>"))
    p.append("<h2>Stations by frame</h2>" + heat_strip(results))
    p.append("<h2>Frames</h2><table><tr><th>Frame</th><th>Verdict</th><th>Stations S/C/H</th><th>Client note</th></tr>")
    for f in frames:
        c = f["counts"]
        p.append(f"<tr><td>{esc(f['frame'])}</td><td>{badge(f['verdict'])}</td>"
                 f"<td>{c['SEND']}/{c['CAVEAT']}/{c['HOLD']}</td><td>{esc(f['client_note'])}</td></tr>")
    p.append("</table><h2>Drafter work queue</h2><table class=q><tr><th>Defect</th><th>Stations</th><th>Fix</th><th>Where</th></tr>")
    for q in queue:
        shown = ", ".join(q["stations"][:12]) + (f" +{len(q['stations']) - 12} more" if len(q["stations"]) > 12 else "")
        p.append(f"<tr><td>{esc(q['label'])}</td><td>{q['count']}</td><td>{esc(q['fix'])}</td><td class=mut>{esc(shown)}</td></tr>")
    p.append(f"</table><p class=mut>Also saved as workqueue_{esc(job)}.csv.</p><h2>Stations to fix, worst first</h2>")
    for r in sorted((r for r in results if r["verdict"] in ("HOLD", "CAVEAT")), key=order_key):
        s = st.get(r["station_id"], {})
        imgs = r.get("images") or [x for x in [s.get("plan")] + (s.get("photos") or [])[:1] if x]
        defects = r.get("defects") or []
        boxed = {d["image"] for d in defects}
        show = [i for i in range(len(imgs)) if i < 2 or i in boxed]
        fix = r.get("fix")
        fix_html = ""
        if isinstance(fix, dict) and fix:
            fix_html = ("<p><b>Suggested fix:</b> " + esc(fix.get("action")) + " " + esc(fix.get("layer"))
                        + (" - " + esc(fix.get("where")) if fix.get("where") else "") + "</p>")
        chips = "".join(f"<span class=chip><span class='sev {d['severity']}'>{d['severity']}</span> {esc(LABELS[d['class']])}</span>"
                        for d in defects) or "".join(f"<span class=chip>{esc(LABELS[c])}</span>" for c in classes_of(r))
        p.append(f"<div class=st><b>{esc(r['station_id'])}</b> {badge('UNASSESSED' if r.get('error') else r['verdict'])}"
                 f" <span class=mut>confidence {r.get('confidence') or 0:.2f}</span><div class=chips>{chips}</div>"
                 f"<p>{esc(r['reason'])}</p>{fix_html}"
                 + (f"<p class=mut>Client note: {esc(r['client_note'])}</p>" if r.get("client_note") else "")
                 + "<div class=imgs>" + "".join(
                     f"<img src='{thumb(imgs[i], boxes=[d for d in defects if d['image'] == i])}' alt='{esc(os.path.basename(imgs[i]))}'>"
                     for i in show) + "</div></div>")
    p.append("</div></body></html>")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write("".join(p))
    return out


def render_index(jobs, out):
    """jobs: list of (summary, report_href). One dashboard row per job plus totals."""
    tot = {k: sum(j["station_counts"][k] for j, _ in jobs) for k in ("SEND", "CAVEAT", "HOLD", "error")}
    ftot = {k: sum(j["frame_counts"][k] for j, _ in jobs) for k in COLORS}
    toks = sum(j["tokens"] for j, _ in jobs)
    n = sum(j["reviewed"] for j, _ in jobs)
    p = [f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>Survey QA Jobs</title><style>{CSS}</style><body>",
         f"<header class=brand><div class=wrap><div><div class=k>Survey QA agent</div><h1>Job dashboard</h1></div>"
         f"<div class=k>{time.strftime('%Y-%m-%d %H:%M')}</div></div></header><div class=wrap>",
         "<div class=tiles>"
         f"<div class=tile><div class=big>{len(jobs)}</div>jobs</div>"
         f"<div class=tile><div class=big>{n}</div>stations reviewed</div>"
         f"<div class=tile><div class=big>{tot['HOLD']}</div>{badge('HOLD')} stations</div>"
         f"<div class=tile><div class=big>{ftot['SEND']}/{sum(ftot.values())}</div>frames ready to send</div>"
         f"<div class=tile><div class=big>{toks / 1e6:.1f}M</div>tokens</div></div>",
         "<h2>Jobs</h2><table><tr><th>Job</th><th>Stations</th><th>Hold share</th><th>Frames S/C/H/U</th><th>Top defect</th><th>Tokens</th><th>Backend</th></tr>"]
    for j, href in jobs:
        sc, fc = j["station_counts"], j["frame_counts"]
        share = sc["HOLD"] / max(j["reviewed"], 1)
        p.append(f"<tr><td><a href='{esc(href)}'>{esc(j['job'])}</a></td><td>{j['reviewed']}/{j['planned']}</td>"
                 f"<td><div class=bars style='grid-template-columns:1fr 40px'><div class=t><i style='width:{share * 100:.0f}%;background:var(--hold)'></i></div>"
                 f"<div class=n>{share:.0%}</div></div></td>"
                 f"<td>{fc['SEND']}/{fc['CAVEAT']}/{fc['HOLD']}/{fc['UNASSESSED']}</td><td>{esc(j.get('top_defect', ''))}</td>"
                 f"<td>{j['tokens'] / 1e6:.2f}M</td><td class=mut>{esc(j['backend'])}</td></tr>")
    p.append("</table><p class=mut>Machine review only; a surveyor signs off every frame.</p></div></body></html>")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w", encoding="utf-8").write("".join(p))
    return out


def _load(path):
    return [normalize_result(json.loads(l)) for l in open(path, encoding="utf-8") if l.strip()]


if __name__ == "__main__":
    if sys.argv[1] == "--index":
        out, rows = sys.argv[2], []
        for path in sys.argv[3:]:
            rs = _load(path)
            S = summarize(rs)
            q = workqueue(rs)
            S["top_defect"] = q[0]["label"] if q else ""
            rows.append((S, f"report_{S['job']}.html"))
        print(render_index(rows, out))
    else:
        rs = _load(sys.argv[1])
        print(render(rs, sys.argv[2] if len(sys.argv) > 2 else f"out/report_{rs[0]['job']}.html"))
