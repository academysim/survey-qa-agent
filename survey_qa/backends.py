"""Model backends behind one interface: decide(station, evidence) -> (reply_dict, usage).

reply_dict is either {"tool": "<name>", "why": "..."} to ask for more evidence, or
{"verdict": "SEND"|"CAVEAT"|"HOLD", "confidence": 0-1, "reason": "...", "client_note": "..."}.
"""
import base64
import json
import os
import re
import time

from .budget import MAX_OUTPUT_TOKENS, BudgetExceeded

VERDICTS = ("SEND", "CAVEAT", "HOLD")
TOOLS = {
    "second_photo": "the next street photo at this station",
    "qa_flags": "the geometric QA flag counts for this frame (ZOFF/NOEDGE/WIGGLE/NOCLOUD)",
    "neighbour": "the plan render of the adjacent station in the same frame",
}

SYSTEM = """You are a field-QA reviewer for CAD survey drafts made from mobile LiDAR.
PLAN images: drafted linework over the scan at one station (RED = reference draft, CYAN = our draft).
PHOTO images: street-level photos from the survey vehicle at that station (CYAN = our draft projected).
Decide whether our drafted lines sit on real pavement markings, pavement edges and curbs, or on wrong
features (jersey barrier, median, tree trunks, vegetation, shadows, construction, doubled lines).
Judge WHERE a line sits from the PLAN view. In PHOTOS a ground line is projected flat onto the image, so it
always crosses cars, people, poles, trunks or foliage standing between the camera and the road: that is
occlusion, not a drafting error. Use photos to identify what a feature is, not to locate the line.
Short ticks, gaps and a minority of flagged bins are normal; HOLD only for linework that is clearly on the
wrong feature or clearly broken in the PLAN, CAVEAT for a real but limited problem.

You may ask for ONE more piece of evidence per turn if you are not sure. Tools:
{tools}
Reply ONLY with JSON, one of:
{{"tool": "<tool name>", "why": "<what you need to check>"}}
{{"verdict": "SEND"|"CAVEAT"|"HOLD", "confidence": <0..1>, "reason": "<one sentence>", "client_note": "<caveat text for the client, or empty>",
 "fix": {{"action": "delete"|"redraw"|"shift"|"merge", "layer": "<layer name from the plan, e.g. PMRK, PavtAsphalt, CG>", "where": "<where in the PLAN, e.g. north side, upper-left third>"}}}}
Give "fix" only with CAVEAT or HOLD: the one edit a drafter should make first. Omit it for SEND.
SEND = ready for the client. CAVEAT = usable, but a specific limitation must be stated. HOLD = wrong, fix before sending."""


def parse_reply(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"verdict": "HOLD", "confidence": 0.0, "reason": f"unparseable model reply: {(text or '')[:120]}",
                "client_note": "", "parse_error": True}
    try:
        d = json.loads(m[0])
    except json.JSONDecodeError:
        return {"verdict": "HOLD", "confidence": 0.0, "reason": "invalid JSON from model", "client_note": "",
                "parse_error": True}
    if "verdict" in d:
        d["verdict"] = str(d["verdict"]).upper()
        if d["verdict"] not in VERDICTS:
            d.update(verdict="HOLD", parse_error=True)
    return d


class FakeBackend:
    """Deterministic stand-in so the agent loop, budget and viewer can be built with zero API spend.
    Decides from the QA flag ratio only - it never looks at pixels. It is the geometry-only reference
    Muse has to beat (README); its score may be inflated if labels were partly derived from QA flags."""
    name = "fake"

    def decide(self, station, evidence, allowed_tools):
        kinds = [e["kind"] for e in evidence]
        usage = {"input_tokens": 0, "output_tokens": 0}
        if "qa_flags" not in kinds and "qa_flags" in allowed_tools:
            return {"tool": "qa_flags", "why": "check geometric flags before judging"}, usage
        f = station.get("qa_flags") or {"bins": 1, "flagged": 0, "counts": {}}
        ratio = f["flagged"] / max(f["bins"], 1)
        if 0.3 < ratio < 0.6 and "neighbour" in allowed_tools and "neighbour" not in kinds:
            return {"tool": "neighbour", "why": "borderline flag ratio, compare adjacent station"}, usage
        top = max(f["counts"], key=f["counts"].get) if f["counts"] else "none"
        if ratio >= 0.6:
            return {"verdict": "HOLD", "confidence": 0.6, "reason": f"{ratio:.0%} of bins flagged, mostly {top}",
                    "client_note": ""}, usage
        if ratio >= 0.3:
            return {"verdict": "CAVEAT", "confidence": 0.5, "reason": f"{ratio:.0%} of bins flagged, mostly {top}",
                    "client_note": f"Some linework in this frame is flagged {top}; verify in the field."}, usage
        return {"verdict": "SEND", "confidence": 0.7, "reason": f"only {ratio:.0%} of bins flagged",
                "client_note": ""}, usage


def _key():
    k = os.environ.get("MODEL_API_KEY")
    if not k and os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
            k = winreg.QueryValueEx(h, "MODEL_API_KEY")[0]
    if not k:
        raise RuntimeError("MODEL_API_KEY not set")
    return k


def _img(path):
    mime = "image/png" if path.endswith(".png") else "image/jpeg"
    return {"type": "input_image",
            "image_url": f"data:{mime};base64,{base64.b64encode(open(path, 'rb').read()).decode()}"}


# Low effort keeps reasoning inside the output cap. One retry on an empty reply, charged like any call.
REASONING_EFFORT = "low"
EMPTY_RETRIES = 1


class MuseSparkBackend:
    """Meta Muse Spark via the OpenAI-Responses-style endpoint at api.meta.ai."""
    URL = "https://api.meta.ai/v1/responses"

    def __init__(self, model="muse-spark-1.3", budget=None):
        if budget is None:
            raise ValueError("MuseSparkBackend needs a Budget: it never sends an unguarded request")
        self.name, self.budget = model, budget

    def build_content(self, station, evidence, allowed_tools):
        tools = "\n".join(f"- {t}: {TOOLS[t]}" for t in allowed_tools) or "- (none left; you must give a verdict)"
        content = [{"type": "input_text", "text": SYSTEM.format(tools=tools)}]
        for e in evidence:
            content.append({"type": "input_text", "text": e["label"]})
            if e.get("path"):
                content.append(_img(e["path"]))
            else:
                content.append({"type": "input_text", "text": e["text"]})
        return content

    def _send(self, content):
        import requests
        try:
            r = requests.post(self.URL, headers={"Authorization": f"Bearer {_key()}"}, timeout=180,
                              json={"model": self.name, "input": [{"role": "user", "content": content}],
                                    "max_output_tokens": MAX_OUTPUT_TOKENS, "reasoning": {"effort": REASONING_EFFORT},
                                    "stream": False})
            r.raise_for_status()
            j = r.json()
            usage = j.get("usage")
            text = "\n".join(c.get("text", "") for o in j.get("output", []) for c in o.get("content", []) or []
                             if isinstance(c, dict) and c.get("text"))
        except BaseException as e:  # incl. Ctrl+C mid-request and unreadable bodies: may still be billed
            self.budget.record_failed()
            if not isinstance(e, Exception):
                raise
            raise self.budget.stop(f"request failed or unreadable, estimate charged, run stopped: {e!r}") from e
        try:
            self.budget.record_real(usage)  # validates before touching totals
        except BudgetExceeded as e:
            raise self.budget.stop(str(e)) from e
        return text, usage

    def decide(self, station, evidence, allowed_tools):
        content = self.build_content(station, evidence, allowed_tools)
        n_img = sum(1 for c in content if c["type"] == "input_image")
        chars = sum(len(c.get("text", "")) for c in content)
        self.budget.check(n_img, chars)  # raises before any request is sent; logs the reservation
        t = time.time()
        tin = tout = 0
        for attempt in range(1 + EMPTY_RETRIES):
            if attempt:
                self.budget.check(n_img, chars)  # the retry is a full paid call: reserve it like one
            text, usage = self._send(content)
            tin += usage["input_tokens"]
            tout += usage["output_tokens"]
            if text.strip():
                break
        usage = {"input_tokens": tin, "output_tokens": tout, "seconds": round(time.time() - t, 1),
                 "recorded": True, "attempts": attempt + 1}
        if not text.strip():
            return {"verdict": "HOLD", "confidence": 0.0, "client_note": "", "empty_reply": True,
                    "reason": "model returned no text (output cap spent on reasoning)"}, usage
        return parse_reply(text), usage
