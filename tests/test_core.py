import pytest

from survey_qa.agent import review_station, rollup
from survey_qa.backends import parse_reply
from survey_qa.budget import Budget, BudgetExceeded
from survey_qa.score import score


def man():
    st = [{"job": "j", "frame": "f0", "station": i, "station_id": f"f0_sta{i:05d}", "plan": f"p{i}.png",
           "photos": [f"a{i}.jpg", f"b{i}.jpg"], "qa_flags": {"bins": 10, "flagged": 9, "counts": {"ZOFF": 9}}}
          for i in (0, 250)]
    return {"job": "j", "stations": st}


class Scripted:
    name = "scripted"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def decide(self, station, evidence, tools):
        self.seen.append((list(tools), [e["kind"] for e in evidence]))
        return self.replies.pop(0), {"input_tokens": 100, "output_tokens": 10}


def test_tool_then_verdict():
    b = Scripted([{"tool": "second_photo", "why": "x"}, {"verdict": "CAVEAT", "reason": "r"}])
    r = review_station(man()["stations"][0], man(), b)
    assert r["verdict"] == "CAVEAT" and r["evidence"] == ["plan", "photo", "second_photo"]
    assert "second_photo" not in b.seen[1][0]  # tool is not offered twice


def test_unknown_tool_refused_and_step_limit_holds():
    b = Scripted([{"tool": "bogus"}] * 10)
    r = review_station(man()["stations"][0], man(), b)
    assert r["verdict"] == "HOLD" and "step limit" in r["reason"]


def test_budget_stops_before_call():
    bud = Budget(max_tokens=8000)
    bud.check(1, 100)
    with pytest.raises(BudgetExceeded):
        bud.check(2, 100)


def test_dollar_cap_needs_price():
    with pytest.raises(ValueError):
        Budget(1000, max_usd=1.0)


def test_parse_reply():
    assert parse_reply('blah {"verdict":"send","reason":"ok"}')["verdict"] == "SEND"
    assert parse_reply("no json")["verdict"] == "HOLD"


def test_rollup_single_hold_in_long_frame_is_caveat():
    rs = [{"frame": "f1", "verdict": "SEND", "station_id": str(i), "client_note": ""} for i in range(5)]
    rs[2]["verdict"] = "HOLD"
    assert rollup(rs)[0]["verdict"] == "CAVEAT"


def test_score_buckets():
    run = {"job": "j", "results": [
        {"station_id": "a", "frame": "f0", "verdict": "HOLD"},
        {"station_id": "b", "frame": "f0", "verdict": "CAVEAT"},
        {"station_id": "c", "frame": "f0", "verdict": "SEND"},
        {"station_id": "d", "frame": "f1", "verdict": "SEND"}], "frames": []}
    s = score(run, labels={("j", "f0"): "SEND"}, reviews={("j", "a"): "CAVEAT"})
    st = s["stations"]
    assert st["too_strict"] == ["a"] and st["stricter_unreviewed"] == ["b"] and st["agree"] == 1 and st["unlabeled"] == 1


# --- spend guard (audit 2026-10-26 fixes 1-5, 9) ---
import requests

from survey_qa.agent import review_job
from survey_qa.backends import MuseSparkBackend
from survey_qa.budget import MAX_OUTPUT_TOKENS, UsageMissing


class FakeResp:
    def __init__(self, j, fail=False):
        self.j, self.fail = j, fail

    def raise_for_status(self):
        if self.fail:
            raise requests.HTTPError("500")

    def json(self):
        return self.j


def muse(monkeypatch, resp, cap=10**6):
    sent = []
    monkeypatch.setenv("MODEL_API_KEY", "test")
    monkeypatch.setattr("survey_qa.backends._img", lambda p: {"type": "input_image", "image_url": "x"})

    def post(url, **kw):
        sent.append(kw["json"])
        if isinstance(resp, BaseException):
            raise resp
        return resp
    monkeypatch.setattr(requests, "post", post)
    bud = Budget(cap)
    return MuseSparkBackend(budget=bud), bud, sent


def reply(usage):
    return FakeResp({"output": [{"content": [{"text": '{"verdict": "SEND", "reason": "ok"}'}]}], "usage": usage})


def test_max_output_tokens_sent_and_reserved(monkeypatch):
    b, bud, sent = muse(monkeypatch, reply({"input_tokens": 500, "output_tokens": 50}))
    review_station(man()["stations"][0], man(), b, bud)
    assert sent[0]["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert bud.estimate(0, 0)[1] == MAX_OUTPUT_TOKENS
    assert bud.calls == 1 and bud.used_in == 500  # recorded once, not twice


def test_missing_usage_charges_estimate_and_stops_run(monkeypatch):
    b, bud, sent = muse(monkeypatch, reply({}))
    run = review_job(man(), b, bud)
    assert len(sent) == 1 and run["stopped"] and "no usable token counts" in run["stopped"]
    assert bud.used_in > 0 and bud.used_out == MAX_OUTPUT_TOKENS and bud.estimated_calls == 1
    assert run["results"][0]["stopped"]  # the station is kept, not dropped


def test_failed_request_charged_and_run_ends_cleanly(monkeypatch):
    b, bud, sent = muse(monkeypatch, requests.Timeout("slow"))
    run = review_job(man(), b, bud)
    assert len(sent) == 1 and "request failed" in run["stopped"]
    assert bud.estimated_calls == 1 and bud.used_out == MAX_OUTPUT_TOKENS
    assert run["reviewed"] == 1


def test_http_error_charged(monkeypatch):
    b, bud, sent = muse(monkeypatch, FakeResp({}, fail=True))
    run = review_job(man(), b, bud)
    assert run["stopped"] and bud.estimated_calls == 1


def test_budget_stop_mid_station_keeps_paid_steps():
    class Then:
        name = "then"
        n = 0

        def decide(self, s, ev, tools):
            self.n += 1
            if self.n == 2:
                raise BudgetExceeded("cap")
            return {"tool": "second_photo"}, {"input_tokens": 100, "output_tokens": 10}
    r = review_job(man(), Then(), Budget(10**6))
    assert r["stopped"] == "cap" and len(r["results"]) == 1
    assert len(r["results"][0]["steps"]) == 1


def test_reserve_blocks_call_that_only_fits_without_output():
    bud = Budget(max_tokens=10**6)
    tin, tout = bud.estimate(1, 300)
    bud.max_tokens = tin + tout - 1  # input alone fits, input + reserved output does not
    with pytest.raises(BudgetExceeded):
        bud.check(1, 300)


@pytest.mark.parametrize("usage", [{"input_tokens": 100, "output_tokens": 0},
                                   {"input_tokens": 100},
                                   {"input_tokens": 100, "output_tokens": None},
                                   {"input_tokens": "abc", "output_tokens": 5},
                                   {"input_tokens": 100, "output_tokens": -50},
                                   None, [1, 2]])
def test_bad_usage_charges_estimate_and_stops(monkeypatch, usage):
    b, bud, sent = muse(monkeypatch, reply(usage))
    run = review_job(man(), b, bud)
    assert len(sent) == 1 and run["stopped"]
    assert bud.estimated_calls == 1 and bud.used_out == MAX_OUTPUT_TOKENS and bud.used_in > 0


def test_unreadable_body_charged(monkeypatch):
    b, bud, sent = muse(monkeypatch, FakeResp([1, 2]))
    run = review_job(man(), b, bud)
    assert len(sent) == 1 and run["stopped"] and bud.estimated_calls == 1


def test_ctrl_c_mid_request_charged(monkeypatch):
    b, bud, sent = muse(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        review_job(man(), b, bud)
    assert bud.estimated_calls == 1


def test_stop_is_latched(monkeypatch):
    b, bud, sent = muse(monkeypatch, requests.Timeout("slow"))
    review_job(man(), b, bud)
    with pytest.raises(BudgetExceeded):
        bud.check(0, 0)


def test_unguarded_backend_refused():
    with pytest.raises(ValueError):
        MuseSparkBackend()


def test_ledger_reservation_written_before_send(monkeypatch, tmp_path):
    led = tmp_path / "l.jsonl"
    b, bud, sent = muse(monkeypatch, None)
    bud.ledger = open(led, "a")

    def post(url, **kw):
        assert '"reserve"' in led.read_text()  # on disk before the request goes out
        return reply({"input_tokens": 5, "output_tokens": 5})
    monkeypatch.setattr(requests, "post", post)
    review_station(man()["stations"][0], man(), b, bud)
    assert '"actual"' in led.read_text()


def test_last_step_offers_no_tools():
    b = Scripted([{"tool": "bogus"}] * 10)
    review_station(man()["stations"][0], man(), b)
    assert b.seen[-1][0] == []


# --- Day-1 fixes 2026-10-27: empty replies, error bucket, partial frames ---
def empty(usage={"input_tokens": 500, "output_tokens": MAX_OUTPUT_TOKENS}):
    return FakeResp({"output": [{"type": "reasoning", "content": []}], "usage": usage})


def test_empty_reply_retried_once_and_both_charged(monkeypatch):
    b, bud, sent = muse(monkeypatch, empty())
    r = review_station(man()["stations"][0], man(), b, bud)
    assert len(sent) == 2 and bud.calls == 2 and bud.used_out == 2 * MAX_OUTPUT_TOKENS
    assert r["error"] and r["verdict"] == "HOLD"


def test_empty_then_good_reply_is_not_an_error(monkeypatch):
    b, bud, sent = muse(monkeypatch, None)
    resps = [empty(), reply({"input_tokens": 500, "output_tokens": 50})]
    monkeypatch.setattr(requests, "post", lambda url, **kw: (sent.append(kw["json"]), resps.pop(0))[1])
    r = review_station(man()["stations"][0], man(), b, bud)
    assert len(sent) == 2 and r["verdict"] == "SEND" and not r["error"]
    assert r["steps"][0]["usage"]["input_tokens"] == 1000


def test_reasoning_effort_sent(monkeypatch):
    b, bud, sent = muse(monkeypatch, reply({"input_tokens": 500, "output_tokens": 50}))
    review_station(man()["stations"][0], man(), b, bud)
    assert sent[0]["reasoning"] == {"effort": "low"}


def test_retry_respects_cap(monkeypatch):
    b, bud, sent = muse(monkeypatch, empty({"input_tokens": 10, "output_tokens": 10}))
    st = man()["stations"][0]
    c = b.build_content(st, __import__("survey_qa.agent").agent.initial_evidence(st), ["second_photo", "qa_flags"])
    est = sum(bud.estimate(sum(x["type"] == "input_image" for x in c), sum(len(x.get("text", "")) for x in c)))
    bud.max_tokens = est + 30  # first call fits, the retry's reservation cannot
    r = review_station(man()["stations"][0], man(), b, bud)
    assert len(sent) == 1 and r["error"] and r["stopped"]


def test_errors_never_score_as_hold():
    run = {"job": "j", "results": [{"station_id": "a", "frame": "f0", "verdict": "HOLD", "error": True},
                                   {"station_id": "b", "frame": "f0", "verdict": "HOLD"}], "frames": []}
    s = score(run, labels={("j", "f0"): "HOLD"}, reviews={})
    assert s["stations"]["error"] == ["a"] and s["stations"]["agree"] == 1 and s["stations_labelled"] == 1


def test_parse_error_marks_station_error():
    r = review_station(man()["stations"][0], man(), Scripted([parse_reply("garbage")]))
    assert r["error"]


def test_partial_frames_not_scored():
    m = man()
    run = review_job(m, Scripted([{"verdict": "HOLD", "reason": "r"}]), limit=1)
    assert run["frames"][0]["complete"] is False
    s = score(run, labels={("j", "f0"): "HOLD"}, reviews={})
    assert s["frames_labelled"] == 0 and s["frames"]["partial"] == ["f0"]
    run = review_job(m, Scripted([{"verdict": "HOLD", "reason": "r"}] * 2))
    assert score(run, labels={("j", "f0"): "HOLD"}, reviews={})["frames_labelled"] == 1


def fr(*vs, err=()):
    return [{"frame": "f0", "station_id": f"s{i}", "verdict": v, "client_note": "", "error": i in err}
            for i, v in enumerate(vs)]


def test_rollup_hold_share_policy():
    assert rollup(fr(*["HOLD"] * 2 + ["SEND"] * 8))[0]["verdict"] == "CAVEAT"   # 20%: localized
    assert rollup(fr(*["HOLD"] * 3 + ["SEND"] * 7))[0]["verdict"] == "HOLD"     # 30%: widespread
    assert rollup(fr("CAVEAT", "SEND", "SEND"))[0]["verdict"] == "CAVEAT"
    assert rollup(fr("SEND", "SEND"))[0]["verdict"] == "SEND"


def test_errored_frame_unassessed_and_not_scored():
    rs = [r | {"job": "j"} for r in fr("HOLD", "HOLD", err=(0,))]
    frames = rollup(rs)
    assert frames[0]["verdict"] == "UNASSESSED"
    frames[0]["complete"] = True
    s = score({"job": "j", "results": rs, "frames": frames}, labels={("j", "f0"): "HOLD"}, reviews={})
    assert s["frames_labelled"] == 0


def test_missing_complete_flag_not_scored():
    s = score({"job": "j", "results": [], "frames": [{"frame": "f0", "verdict": "HOLD"}]},
              labels={("j", "f0"): "HOLD"}, reviews={})
    assert s["frames_labelled"] == 0


# --- hybrid reviewer (rule fixed before any held-out run) ---
from survey_qa import hybrid
from survey_qa.hybrid import geometry_verdict, review_station_hybrid


def hst(flagged, sid="f0_sta00000"):
    s = dict(man()["stations"][0], station_id=sid)
    s["qa_flags"] = {"bins": 100, "flagged": flagged, "counts": {"ZOFF": flagged}}
    return s


def test_geometry_thresholds():
    assert [geometry_verdict(hst(n))[0] for n in (29, 30, 59, 60)] == ["SEND", "CAVEAT", "CAVEAT", "HOLD"]


def test_hybrid_clean_unaudited_makes_no_call(monkeypatch):
    monkeypatch.setattr(hybrid, "audited", lambda s: False)
    b = Scripted([])
    r = review_station_hybrid(hst(5), man(), b)
    assert r["verdict"] == "SEND" and r["decided_by"] == "geometry" and b.seen == []


def test_hybrid_flagged_muse_decides():
    r = review_station_hybrid(hst(70), man(), Scripted([{"verdict": "SEND", "confidence": 0.9, "reason": "noise"}]))
    assert r["verdict"] == "SEND" and r["decided_by"] == "muse_adjudicated"


def test_hybrid_audit_escalates_only_when_confident(monkeypatch):
    monkeypatch.setattr(hybrid, "audited", lambda s: True)
    hi = review_station_hybrid(hst(5), man(), Scripted([{"verdict": "HOLD", "confidence": 0.85, "reason": "truss"}]))
    lo = review_station_hybrid(hst(5), man(), Scripted([{"verdict": "HOLD", "confidence": 0.6, "reason": "maybe"}]))
    assert hi["verdict"] == "HOLD" and hi["decided_by"] == "muse_audit_escalated"
    assert lo["verdict"] == "SEND" and lo["decided_by"] == "muse_audit_confirmed"


def test_hybrid_error_stays_error():
    r = review_station_hybrid(hst(70), man(), Scripted([parse_reply("garbage")]))
    assert r["error"] and r["decided_by"] == "error"


def test_audit_sample_is_about_one_in_three():
    n = sum(hybrid.audited({"job": "j", "station_id": f"s{i}"}) for i in range(3000))
    assert 850 < n < 1150


def test_fix_kept_only_on_problem_verdicts():
    fix = {"action": "delete", "layer": "PMRK", "where": "north side"}
    hold = review_station(man()["stations"][0], man(), Scripted([{"verdict": "HOLD", "reason": "r", "fix": fix}]))
    send = review_station(man()["stations"][0], man(), Scripted([{"verdict": "SEND", "reason": "r", "fix": fix}]))
    junk = review_station(man()["stations"][0], man(), Scripted([{"verdict": "HOLD", "reason": "r", "fix": "redo it"}]))
    assert hold["fix"] == fix and send["fix"] is None and junk["fix"] is None


def test_api_gets_bare_model_id_and_runs_record_prompt_version(monkeypatch):
    b, bud, sent = muse(monkeypatch, reply({"input_tokens": 500, "output_tokens": 50}))
    r = review_station(man()["stations"][0], man(), b, bud)
    assert sent[0]["model"] == "muse-spark-1.3" and r["backend"].endswith("+" + __import__("survey_qa.backends").backends.PROMPT_VERSION)


def test_overrun_past_cap_stops_run():
    b = Budget(max_tokens=10000)
    b.check(1, 0)
    with pytest.raises(BudgetExceeded):
        b.record_real({"input_tokens": 20000, "output_tokens": 10})
    assert b.stopped
    with pytest.raises(BudgetExceeded):
        b.check(1, 0)


def test_overrun_past_dollar_cap_stops_run():
    b = Budget(max_tokens=10**9, max_usd=0.01, usd_per_m_in=1.0, usd_per_m_out=1.0)
    b.check(1, 0)
    with pytest.raises(BudgetExceeded):
        b.record_real({"input_tokens": 20000, "output_tokens": 10})
    assert b.stopped
