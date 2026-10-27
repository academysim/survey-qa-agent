"""Score a run against human labels.

Frame labels come from data/labels.csv. Station-level truths from a human re-check (data/station_reviews.csv)
override the frame label for that station. A model verdict STRICTER than the label goes in a
"possible defect found" bucket for human review instead of counting as a plain miss.
"""
import csv
import os

from .agent import RANK

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_labels(path=None):
    path = path or os.path.join(HERE, "data", "labels.csv")
    return {(r["job"], r["frame"]): r["human_verdict"] for r in csv.DictReader(open(path, encoding="utf-8"))}


def load_reviews(path=None):
    path = path or os.path.join(HERE, "data", "station_reviews.csv")
    if not os.path.exists(path):
        return {}
    return {(r["job"], r["station_id"]): r["human_truth"] for r in csv.DictReader(open(path, encoding="utf-8"))}


def score(run, labels=None, reviews=None):
    labels = load_labels() if labels is None else labels
    reviews = load_reviews() if reviews is None else reviews
    job = run["job"]
    st = {"agree": 0, "stricter_unreviewed": [], "stricter_confirmed": [], "too_lenient": [], "too_strict": [],
          "unlabeled": 0, "error": []}
    for r in run["results"]:
        sid = r["station_id"]
        if r.get("error"):
            st["error"].append(sid)  # never counted as a HOLD, right or wrong
            continue
        truth = reviews.get((job, sid))
        reviewed = truth is not None
        truth = truth or labels.get((job, r["frame"]))
        if truth is None:
            st["unlabeled"] += 1
            continue
        if r["verdict"] == truth:
            if reviewed and labels.get((job, r["frame"])) != truth:
                st["stricter_confirmed"].append(sid)  # model matched a human-confirmed defect the frame label missed
            else:
                st["agree"] += 1
        elif RANK[r["verdict"]] > RANK[truth]:
            (st["too_strict"] if reviewed else st["stricter_unreviewed"]).append(sid)
        else:
            st["too_lenient"].append(sid)
    fr = {"agree": 0, "disagree": [], "partial": [], "always_caveat_agree": 0}
    for f in run["frames"]:
        if not f.get("complete") or f["verdict"] == "UNASSESSED":
            fr["partial"].append(f["frame"])  # only fully reviewed frames are scored
            continue
        truth = labels.get((job, f["frame"]))
        if truth is None:
            continue
        fr["always_caveat_agree"] += truth == "CAVEAT"  # trivial baseline, reported next to the model
        if f["verdict"] == truth:
            fr["agree"] += 1
        else:
            fr["disagree"].append(f"{f['frame']}: model {f['verdict']} vs human {truth}")
    labelled = len(run["results"]) - st["unlabeled"] - len(st["error"])
    return {"stations_labelled": labelled,
            "station_agree_pct": round(100 * (st["agree"] + len(st["stricter_confirmed"])) / labelled, 1) if labelled else None,
            "stations": st, "frames": fr,
            "frames_labelled": fr["agree"] + len(fr["disagree"])}
