"""Review agent: per-station loop that pulls more evidence when unsure, then frame and job roll-up."""
import json

from .backends import TOOLS
from .budget import BudgetExceeded
from .manifest import neighbours

MAX_STEPS = 4
RANK = {"SEND": 0, "CAVEAT": 1, "HOLD": 2}


def initial_evidence(station):
    ev = []
    if station["plan"]:
        ev.append({"kind": "plan", "label": f"PLAN {station['station_id']}:", "path": station["plan"]})
    if station["photos"]:
        ev.append({"kind": "photo", "label": "PHOTO 1:", "path": station["photos"][0]})
    return ev


def run_tool(name, station, manifest, evidence):
    """Return an evidence item, or None if the tool has nothing to give."""
    if name == "second_photo":
        used = {e["path"] for e in evidence if e.get("path")}
        rest = [p for p in station["photos"] if p not in used]
        return {"kind": "second_photo", "label": "PHOTO 2:", "path": rest[0]} if rest else None
    if name == "qa_flags":
        return {"kind": "qa_flags", "label": "QA FLAGS for this frame:",
                "text": json.dumps(station["qa_flags"] or "no flags recorded")}
    if name == "neighbour":
        for n in neighbours(manifest, station["station_id"]):
            if n["plan"]:
                return {"kind": "neighbour", "label": f"NEIGHBOUR PLAN {n['station_id']}:", "path": n["plan"]}
    return None


def available_tools(station, manifest, evidence):
    kinds = {e["kind"] for e in evidence}
    out = []
    if "second_photo" not in kinds and len(station["photos"]) > 1:
        out.append("second_photo")
    if "qa_flags" not in kinds:
        out.append("qa_flags")
    if "neighbour" not in kinds and any(n["plan"] for n in neighbours(manifest, station["station_id"])):
        out.append("neighbour")
    return out


def review_station(station, manifest, backend, budget=None):
    evidence = initial_evidence(station)
    steps, stopped = [], None
    for i in range(MAX_STEPS):
        # Last step offers no tools, so the model has to give a verdict.
        tools = available_tools(station, manifest, evidence) if i < MAX_STEPS - 1 else []
        try:
            reply, usage = backend.decide(station, evidence, tools)
        except BudgetExceeded as e:
            # Keep the steps already paid for; the station is reported as stopped, not dropped.
            stopped = str(e)
            reply = {"verdict": "HOLD", "confidence": 0.0, "reason": f"stopped by spend guard: {stopped}",
                     "client_note": "", "error": True}
            break
        if budget and not usage.get("recorded"):
            budget.record(usage)
        steps.append({"reply": reply, "usage": usage})
        tool = reply.get("tool")
        if "verdict" in reply or tool is None:
            break
        item = run_tool(tool, station, manifest, evidence) if tool in tools else None
        if item is None:
            # Unknown or exhausted tool: tell the model and force a verdict next turn.
            evidence.append({"kind": f"refused_{tool}", "label": "TOOL RESULT:",
                             "text": f"'{tool}' is not available. Give your verdict now."})
        else:
            evidence.append(item)
    else:
        reply = {"verdict": "HOLD", "confidence": 0.0, "reason": "agent hit step limit without a verdict",
                 "client_note": ""}
    if "verdict" not in reply:
        reply = {"verdict": "HOLD", "confidence": 0.0, "reason": "no verdict returned", "client_note": ""}
    return {"job": station["job"], "frame": station["frame"], "station_id": station["station_id"],
            "backend": backend.name, "verdict": reply["verdict"], "confidence": reply.get("confidence"),
            "reason": reply.get("reason", ""), "client_note": reply.get("client_note", ""),
            # Suggested edit for the drafter; only kept on a CAVEAT/HOLD and only if it is a dict.
            "fix": reply.get("fix") if reply["verdict"] != "SEND" and isinstance(reply.get("fix"), dict) else None,
            "evidence": [e["kind"] for e in evidence],
            "steps": [{"tool": s["reply"].get("tool"), "why": s["reply"].get("why"), "usage": s["usage"]}
                      for s in steps],
            "stopped": stopped,
            # Parse errors, empty replies and guard stops default to HOLD but are not real verdicts.
            "error": bool(stopped or reply.get("error") or reply.get("parse_error") or reply.get("empty_reply")
                          or reply.get("reason") in ("no verdict returned", "agent hit step limit without a verdict"))}


# Client-acceptance policy (stated up front, not fit to data): a frame is usable with the flagged areas
# excluded while HOLD stations are at most this share of it; beyond that it goes back for correction.
HOLD_SHARE = 0.20


def rollup(results):
    """HOLD if more than HOLD_SHARE of stations are HOLD, CAVEAT if any station is HOLD or CAVEAT, else SEND.
    A frame with any errored station is UNASSESSED: an error's default HOLD is not a verdict."""
    frames = {}
    for r in results:
        frames.setdefault(r["frame"], []).append(r)
    out = []
    for fr, rs in sorted(frames.items(), key=lambda kv: int(kv[0][1:])):
        counts = {v: sum(1 for r in rs if r["verdict"] == v) for v in RANK}
        errors = sum(1 for r in rs if r.get("error"))
        if errors:
            verdict = "UNASSESSED"
        elif counts["HOLD"] > HOLD_SHARE * len(rs):
            verdict = "HOLD"
        elif counts["HOLD"] + counts["CAVEAT"]:
            verdict = "CAVEAT"
        else:
            verdict = "SEND"
        notes = sorted({r["client_note"] for r in rs if r["client_note"]})
        out.append({"frame": fr, "verdict": verdict, "stations": len(rs), "counts": counts, "errors": errors,
                    "hold_stations": [r["station_id"] for r in rs if r["verdict"] == "HOLD"],
                    "client_note": " ".join(notes)})
    return out


def review_job(manifest, backend, budget=None, limit=None, only=None, on_result=None, reviewer=None):
    reviewer = reviewer or review_station
    stations = manifest["stations"]
    if only:
        stations = [s for s in stations if s["station_id"] in only or s["frame"] in only]
    if limit:
        stations = stations[:limit]
    results, stopped = [], None
    for s in stations:
        try:
            r = reviewer(s, manifest, backend, budget)
        except BudgetExceeded as e:
            stopped = str(e)
            break
        results.append(r)
        if on_result:
            on_result(r)
        if r["stopped"]:
            stopped = r["stopped"]
            break
    frame_sizes = {}
    for s in manifest["stations"]:
        frame_sizes[s["frame"]] = frame_sizes.get(s["frame"], 0) + 1
    frames = rollup(results)
    for f in frames:
        f["complete"] = f["stations"] == frame_sizes.get(f["frame"])
    return {"job": manifest["job"], "backend": backend.name, "results": results, "frames": frames,
            "budget": budget.summary() if budget else None, "stopped": stopped,
            "reviewed": len(results), "planned": len(stations)}
