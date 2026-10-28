# survey-qa-agent

An agent that does field QA on LiDAR-to-CAD survey drafts with **Meta Muse Spark**. It looks at each
station's plan render and the street photos taken there, asks for more evidence when it is not sure
(the second photo, the geometric QA flags, the next station over), and returns SEND / CAVEAT / HOLD with a
reason. It then rolls the stations up into a verdict per frame and writes the caveat note for the client.

## Built during the hackathon (Oct 26 - Nov 4, 2026)
Everything in this repo: `survey_qa/` (manifest, agent loop, backends, spend guard, scorer, viewer, report, replay),
`run.py`, `tests/`, `data/*.csv` labels.

## Pre-existing input (not part of the submission)
The CAD drafts and review sheets (plan renders, photos, QA_flags.txt) come from our earlier
RockyPointSurvey pipeline. They are input data only. Scans, photos and DXF files are **not** in this
repo (see `.gitignore`).

## Run
**The survey input data is not in this repo** (client scans and photos). The code expects a drafts folder
laid out as `<job>/_package/review_sheets/` holding `fN_staNNNNN_plan.png`, `fN_staNNNNN_photoK.jpg` and `QA_flags.txt`.
Set `SURVEY_QA_DRAFTS` to that folder. Without it, `run.py` has nothing to review; `pytest` runs without it.

    python -m venv .venv && .venv/Scripts/pip install requests pytest pillow
    python run.py e57_cleveland_courtney --backend fake          # zero spend
    python run.py e57_cleveland_courtney --backend muse --max-tokens 600000 --limit 20   # ~21K tokens per station (prompt v5)

`--backend muse` refuses to start without `--max-tokens`. Before every call it estimates the cost and
stops if the call could go past the cap. Token estimates are padded guesses, so one call can still
run over its reservation; the ledger logs that as an overrun and the run stops at once if the token or dollar cap was passed. A `--max-usd` cap also needs `--usd-in/--usd-out` prices.
Each run writes `runs/<job>_<backend>_<time>/` containing `results.jsonl` (written as it goes), `run.json`
and `viewer.html`.

## Scoring
`data/labels.csv` holds the human verdicts per frame. `data/station_reviews.csv` holds stations a human
re-checked, and these override the frame label. When the model is stricter than the label, the station
goes into a "possible defect found" bucket for a person to look at instead of counting as a miss.
(Example: f6_sta00250 was labelled SEND, but Muse flagged lines drawn on a steel truss stack. That was real.)

The `fake` backend only looks at QA flag ratios and never sees pixels. It is the geometry-only reference Muse has to beat. Its
score may be inflated if some labels were partly derived from the same QA flags.

## Hybrid reviewer (pre-registered)
`--backend hybrid`: the free geometry check decides first and Muse is only called where it can change the answer.
Geometry CAVEAT/HOLD stations go to Muse, whose verdict stands. Geometry SEND stations skip the model, except a
fixed 1-in-3 audit sample where Muse may only escalate, at confidence >= 0.8. The exact rule is in
`survey_qa/hybrid.py` and was committed before it was run on any held-out station.

## Suggested fixes and client report
On a CAVEAT or HOLD the agent also names the one edit a drafter should make first (`fix`: action, layer, where in
the plan), in the same reply, so it costs no extra call. `python -m survey_qa.report runs/<run>/results.jsonl`
writes a single self-contained HTML report per job (frame table, client caveats, every flagged station with its
plan and photo, reason and suggested fix) that prints cleanly to PDF. Needs `pip install pillow`.

### Defect boxes and severity (prompt v5)
From v5 the same verdict reply also lists each defect: `{"class", "severity": minor|major|critical, "image", "box"}`.
`class` is one of the surveyor's checklist classes (grass, canopy, elevation_jump, lane_cutoff, car_jumble, gore,
crosswalk, parking_stop, flow_line, median_end, other), `image` indexes the images the model saw, and `box` is
`[x0, y0, x1, y1]` as fractions of that image. Boxes are validated before use: out-of-range values are clamped,
and boxes that are malformed, empty or point at an image the model never saw are dropped (`parse_defects`).
The frame roll-up does not use them; they drive the report and the work queue.

The report draws the boxes in red on the plan and photos, lists stations worst first (HOLD, then severity,
then confidence) and opens with a defect-class bar chart and a per-frame strip of station verdicts.

### Drafter work queue
The report groups HOLD/CAVEAT stations by defect class, with a count, the station list and the standard
fix for that class, and also saves it as `workqueue_<job>.csv`, so a drafter works one defect type at a time
across the whole job. Results from before v5 have no `defects` field, so their classes are mapped from the
reason text by keyword. That mapping is rough; the model's own classes replace it from v5 on.

### Job dashboard and agent replay
    python -m survey_qa.report --index out/index.html runs/*/results.jsonl   # one row per job, totals, links
    python -m survey_qa.trace runs/<run>/results.jsonl out/trace_<job>.html   # step through the agent's turns

The replay page picks a station and shows each turn: what the model looked at, which extra evidence it asked
for and why, then the verdict, the fix and the defect boxes. It replays recorded results, so no API key is
ever in a browser.

## Held-out labels
`python -m survey_qa.label <jobs...>` builds a blinded labelling page: stations from jobs the agent was never
tuned on, sampled by QA-flag ratio band (not by model output), with no model verdict shown. Labels go in
`data/station_labels_<name>.csv`.

### Held-out stations, prompt v3 (surveyor defect checklist)

| Set | Stations (human) | Muse v3 caught | Geometry-only caught | Muse v3 false alarms |
|---|---|---|---|---|
| heldout1 (DEV: v3 checklist written from these notes) | 50 HOLD, 10 SEND | 47/50 | 16/50 | 8/10 |
| heldout2 (clean: new jobs phillippe, sr583 + 4 others, never seen) | 62 HOLD, 0 SEND | **60/62** (55 HOLD, 5 CAVEAT) | 42/62 (10 HOLD, 32 CAVEAT) | not measurable (no clean stations) |

The two heldout2 misses are scope gaps in the checklist: parking stops drafted as lines, and a half-drawn
crosswalk with an untraced gore. v3 is strict: on heldout1 it held 8 of 10 stations a surveyor would send, so it is
a conservative reviewer, not a replacement for one. heldout2 cost about 0.86M tokens for 62 stations. Both held-out sets were scored with prompt v3. The surveyor
re-checked the heldout1 "false alarms" and says those stations were also missing linework, so they may be real
defects. Prompt v4 (parking stops, partly drawn crosswalks and gores, no flow line where asphalt meets the curb
face) was written AFTER heldout2 was scored, so v4 has no clean held-out number yet.

### Full jobs, prompt v4 (no frame labels; volume and cost only)
These are the same four jobs heldout1 was sampled from, and heldout1's notes shaped the checklist, so they are not
held out for v4.

| Job | Stations | HOLD / CAVEAT / SEND | Errors |
|---|---|---|---|
| largo | 115 | 104 / 4 / 7 | 0 |
| e57_curlew | 160 | 136 / 14 / 10 | 0 |
| pasco789 | 248 | 220 / 15 / 13 | 0 |
| e57_strd50 | 334 | 292 / 20 / 22 | 0 |

857 stations, about 14.65M tokens (~17K per station incl. evidence calls, ~1.9 calls each), about $25.

### Full job, prompt v5 (defect boxes; not held out)
largo re-run with v5: 115 stations, 107 HOLD / 5 CAVEAT / 3 SEND, 0 errors, 217 calls, 1.85M tokens
(~16K per station, about $3). 106 of 112 flagged stations came back with at least one box (228 defects:
198 major, 19 minor, 11 critical). Boxes were checked by eye on a 20-station pilot, not scored against labels.
Severity is mostly "major", so it does little to reorder the queue yet.

## Earlier results, prompts v1/v2 (historical, measured 2026-10-27)
Frame verdict rule (a stated client-acceptance policy, not fit to data): HOLD if more than 20% of a frame's
stations are HOLD, CAVEAT if any station is HOLD or CAVEAT, otherwise SEND. A frame with any errored station
is UNASSESSED and not scored.

| Run | Stations | Muse station agree | Muse frames | Fake (QA flags only) | Always-CAVEAT |
|---|---|---|---|---|---|
| cleveland, prompt v1, full job | 236 | 37.7% | 5/19 | 91.9%, 16/19 frames | 2/19 |
| cleveland, prompt v2, full job | 236 | 58.1% | 6/19 | 91.9%, 16/19 frames | 2/19 |
| roosevelt f2-f4, prompt v2 | 54 | 38.9% | 2/3 | 0%, 0/3 frames | **3/3** |

Cleveland frame-level, prompt v2: all 5 HOLD frames caught, and no bad frame was ever passed as SEND. Every miss
went the safe way: 8 of the 12 SEND frames came out CAVEAT, 3 came out HOLD, and the 2 CAVEAT frames came out HOLD.
Muse today is a conservative reviewer, not a better one than the geometry checks.

**How to read this (please do):**
- We revised aggregation after inspecting the Roosevelt predictions and labels. Roosevelt was held out for prompt
  development but is not an independent test of the revised pipeline. The original aggregation agrees on 0/3 frames
  and the revised one on 2/3. All three labels are CAVEAT, so an always-CAVEAT baseline scores 3/3.
- Prompt v2 was written after reading v1's mistakes on cleveland, so cleveland informed prompt tuning.
  These results do not establish generalization.
- Labels are per frame. Station agreement treats the frame label as every station's truth, which is not right
  (a CAVEAT frame has good stations too), so frame agreement is the fairer number.
- The fake reference may be inflated if some labels were partly derived from the same QA flags.
- Cost: ~25K tokens per station with prompt v2 (2.7 calls on average); the full 236-station job was 5.92M tokens.
  In dollars, about $0.0017 per 1K tokens on our Meta billing (blended in+out, read from the account balance,
  not a published price), so roughly $10 for the job.
