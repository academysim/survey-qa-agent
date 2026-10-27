"""Hybrid reviewer: the free geometry check decides first, Muse is called only where it can change the answer.

Rule (fixed and committed BEFORE it was run on any held-out station):
- Geometry verdict from the QA-flag ratio, same thresholds as FakeBackend: <30% flagged SEND, 30-60% CAVEAT, >=60% HOLD.
- Geometry CAVEAT or HOLD: Muse adjudicates and its verdict stands (it can see whether flagged bins are a real
  drafting problem or scan noise such as vegetation or rural edges with no curb).
- Geometry SEND: no call, except a fixed 1-in-3 audit sample (by hash of job/station id). On an audited station
  Muse may only escalate, and only with confidence >= 0.8.
- A Muse error or guard stop leaves the station as an error (never silently the geometry verdict).
"""
import hashlib

from .agent import RANK, review_station

LOW, HIGH = 0.3, 0.6
AUDIT_EVERY = 3
ESCALATE_CONF = 0.8


def geometry_verdict(station):
    f = station.get("qa_flags") or {"bins": 1, "flagged": 0, "counts": {}}
    ratio = f["flagged"] / max(f["bins"], 1)
    return ("HOLD" if ratio >= HIGH else "CAVEAT" if ratio >= LOW else "SEND"), ratio


def audited(station):
    h = hashlib.sha256(f"{station['job']}/{station['station_id']}".encode()).digest()
    return h[0] % AUDIT_EVERY == 0


def review_station_hybrid(station, manifest, backend, budget=None):
    g, ratio = geometry_verdict(station)
    base = {"job": station["job"], "frame": station["frame"], "station_id": station["station_id"],
            "backend": f"hybrid({backend.name})", "geometry": g, "flag_ratio": round(ratio, 3)}
    if g == "SEND" and not audited(station):
        return base | {"verdict": "SEND", "confidence": None, "decided_by": "geometry",
                       "reason": f"only {ratio:.0%} of bins flagged; not in audit sample", "client_note": "",
                       "evidence": ["qa_flags"], "steps": [], "stopped": None, "error": False}
    r = review_station(station, manifest, backend, budget)
    out = base | {k: r[k] for k in ("confidence", "reason", "client_note", "evidence", "steps", "stopped", "error")}
    if r["error"]:
        return out | {"verdict": r["verdict"], "decided_by": "error"}
    if g != "SEND":
        return out | {"verdict": r["verdict"], "decided_by": "muse_adjudicated"}
    if RANK[r["verdict"]] > RANK["SEND"] and (r["confidence"] or 0) >= ESCALATE_CONF:
        return out | {"verdict": r["verdict"], "decided_by": "muse_audit_escalated"}
    return out | {"verdict": "SEND", "decided_by": "muse_audit_confirmed",
                  "reason": f"geometry SEND; audit: {r['reason']}"}
