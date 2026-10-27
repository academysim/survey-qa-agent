"""Run the survey QA agent on one job.

  python run.py e57_cleveland_courtney --backend fake
  python run.py e57_cleveland_courtney --backend muse --max-tokens 250000 --limit 20

The muse backend refuses to start without --max-tokens, reserves max_output_tokens before every call, charges estimates for failed or uncounted calls, and stops the run.
"""
import argparse
import json
import os
import time

from survey_qa.agent import review_job
from survey_qa.backends import FakeBackend, MuseSparkBackend
from survey_qa.budget import Budget
from survey_qa.manifest import build
from survey_qa.score import score
from survey_qa.viewer import render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--backend", choices=["fake", "muse"], default="fake")
    ap.add_argument("--model", default="muse-spark-1.3")
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--max-usd", type=float)
    ap.add_argument("--usd-in", type=float, help="USD per 1M input tokens")
    ap.add_argument("--usd-out", type=float, help="USD per 1M output tokens")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="*", help="station ids or frames, e.g. f0 f6_sta00250")
    a = ap.parse_args()

    if a.backend == "muse":
        if not a.max_tokens:
            ap.error("--backend muse requires --max-tokens (spend guard)")
    name = a.model if a.backend == "muse" else "fake"
    d = f"runs/{a.job}_{name}_{time.strftime('%Y%m%d-%H%M%S')}"
    os.makedirs(d, exist_ok=True)
    if a.backend == "muse":
        budget = Budget(a.max_tokens, a.max_usd, a.usd_in, a.usd_out, ledger=f"{d}/spend_ledger.jsonl")
        backend = MuseSparkBackend(a.model, budget)
    else:
        budget = Budget(a.max_tokens or 10**12)
        backend = FakeBackend()

    man = build(a.job)
    log = open(f"{d}/results.jsonl", "w", encoding="utf-8")

    def on_result(r):
        log.write(json.dumps(r) + "\n")
        log.flush()  # partial results survive a crash or budget stop
        print(f"{r['station_id']:14} {r['verdict']:6} {r['evidence']} {r['reason'][:70]}")

    try:
        run = review_job(man, backend, budget, a.limit, a.only, on_result)
    except BaseException as e:  # anything unexpected still leaves run.json with the spend so far
        json.dump({"job": a.job, "budget": budget.summary(), "stopped": f"crashed: {e!r}"},
                  open(f"{d}/run.json", "w"), indent=1)
        raise
    sc = score(run)
    run["score"] = sc
    json.dump(run, open(f"{d}/run.json", "w"), indent=1)
    render(run, f"{d}/viewer.html", sc)
    print(f"\n{run['reviewed']}/{run['planned']} stations; budget {run['budget']}")
    if run["stopped"]:
        print("STOPPED BY SPEND GUARD:", run["stopped"])
    print(f"station agree {sc['station_agree_pct']}% of {sc['stations_labelled']} labelled; "
          f"frames agree {sc['frames']['agree']}/{sc['frames_labelled']}")
    print("viewer:", os.path.abspath(f"{d}/viewer.html"))


if __name__ == "__main__":
    main()
