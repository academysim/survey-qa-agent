"""Index a draft package's review_sheets into stations: {job, frame, station, plan, photos[], qa_flags}."""
import ast
import json
import os
import re
import sys

# Survey drafts are input data and are not in this repo; point SURVEY_QA_DRAFTS at your own copy.
DRAFTS = os.environ.get("SURVEY_QA_DRAFTS", "A:/RockyPointSurvey/drafts")
SHEET_RE = re.compile(r"^(f\d+)_sta(\d+)_(plan|photo\d+)\.(png|jpg)$")
FLAG_RE = re.compile(r"^(f\d+): (\d+) bin-sides, (\d+) flagged: (\{.*\})\s*$")


def parse_qa_flags(path):
    """QA_flags.txt -> {frame: {"bins": n, "flagged": n, "counts": {...}}}"""
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        m = FLAG_RE.match(line.strip())
        if m:
            out[m[1]] = {"bins": int(m[2]), "flagged": int(m[3]), "counts": ast.literal_eval(m[4])}
    return out


def build(job, drafts=DRAFTS):
    pkg = f"{drafts}/{job}/_package"
    sheets = f"{pkg}/review_sheets"
    flags = parse_qa_flags(f"{pkg}/QA_flags.txt")
    stations = {}
    for name in sorted(os.listdir(sheets)):
        m = SHEET_RE.match(name)
        if not m:
            continue
        frame, sta, kind = m[1], int(m[2]), m[3]
        sid = f"{frame}_sta{m[2]}"
        s = stations.setdefault(sid, {"job": job, "frame": frame, "station": sta, "station_id": sid,
                                      "plan": None, "photos": []})
        if kind == "plan":
            s["plan"] = f"{sheets}/{name}"
        else:
            s["photos"].append(f"{sheets}/{name}")
    ordered = sorted(stations.values(), key=lambda s: (int(s["frame"][1:]), s["station"]))
    for s in ordered:
        s["qa_flags"] = flags.get(s["frame"])
    return {"job": job, "package": pkg, "stations": ordered}


def neighbours(manifest, sid):
    """Previous and next station in the same frame."""
    st = manifest["stations"]
    i = next(k for k, s in enumerate(st) if s["station_id"] == sid)
    fr = st[i]["frame"]
    return [st[k] for k in (i - 1, i + 1) if 0 <= k < len(st) and st[k]["frame"] == fr]


if __name__ == "__main__":
    job = sys.argv[1]
    m = build(job)
    out = sys.argv[2] if len(sys.argv) > 2 else f"manifest_{job}.json"
    json.dump(m, open(out, "w"), indent=1)
    n = m["stations"]
    print(f"{job}: {len(n)} stations, {sum(1 for s in n if s['plan'])} with plan, "
          f"{sum(len(s['photos']) for s in n)} photos, {len({s['frame'] for s in n})} frames -> {out}")
