# Live behavioral website comparison — 2026-10-06

This is the initial comparison. See [input corrections and APIVoid results](behavior-input-improvements.md) for the subsequent implementation and final measurements.

Pacing improved the results on two independent public demos, but did not produce uniformly human classifications. WebDecoy allowed every paced submission in this sample. Detection Lab classified every paced run as **MARGINAL**. SENTINEL's perfect scores also occurred with standard input and are weak evidence.

## Results

Each row represents three fresh sessions per mode. `Restored` disables pacing again in a fresh context after the paced run.

| Public test | Standard | Paced | Restored standard |
|---|---|---|---|
| [WebDecoy FCaptcha demo](https://webdecoy.com/product/fcaptcha-demo/) — lower bot score is better | 0.60, 0.60, 0.60; blocked 3/3 | **0.21632, 0.23232, 0.21632; allowed 3/3** | 0.60, 0.60, 0.60; blocked 3/3 |
| [Detection Lab sandbox](https://www.detectionlab.app/sandbox) — higher human score is better | 48.1, 48.0, 48.0; FAIL 3/3 | **68.0, 65.0, 66.4; MARGINAL 3/3** | 46.6, 48.0, 48.0; FAIL 3/3 |
| [SENTINEL](https://sentinel-bot-detector.vercel.app/) — higher human score is better | 100, 100, 100 | 100, 100, 100 | 100, 100, 100 |
| [Incolumitas](https://bot.incolumitas.com/) | No behavioral score | No behavioral score | No behavioral score |

All 36 completed runs produced the requested field values without input exceptions. Only **27/36 returned behavioral scores**. Incolumitas is an unavailable measurement, not a pass. An earlier Detection Lab probe targeted the hidden checkbox instead of its visible label; its incomplete runs are retained separately and excluded from this table.

## Mouse and keyboard findings

Detection Lab provides the most useful separate metrics in this sample:

| Site metric, 0–100 | Standard, three runs | Paced, three runs |
|---|---|---|
| Keystroke timing | 0, 0, 0 | 85, 95, 75 |
| Mouse path curvature | 0, 0, 0 | 72, 29, 67 |
| Mouse speed variability | 0, 0, 0 | 100, 100, 100 |
| Timing distribution fit | 50, 50, 50 | 60, 60, 60 |
| Hover before click | 0, 0, 0 | 47, 47, 47 |
| Event ordering | 100, 100, 100 | 0, 0, 0 |
| Stealth artifacts | 0, 0, 0 | 0, 0, 0 |

These are the site's displayed heuristic scores, not calibrated probabilities or independently established diagnoses. The short task generated no scroll events, and the site scored scroll behavior zero in both modes. Several unused categories received 50. Consequently, its overall score mixes exercised behavior, missing behavior and browser properties. The paced runs captured 36 keystrokes; the displayed environment still included a HeadlessChrome user agent. The low event-ordering result is a finding to investigate, not proof that the browser emitted invalid input. This audit does not alter the controller to target these scores.

WebDecoy's actual result JSON reported `success: true` and recommendation `allow` in all paced runs. Its experimental observation still listed `cdp` among corroborating categories. Thus, allowed does **not** mean zero detections. The displayed UI rounds scores to 0.22/0.23/0.22; the table preserves the more precise JSON values. Its separate marketing-page scanner calls to `test3.bombfind.com` failed DNS resolution. The FCaptcha result itself completed; that service limitation is retained in the raw evidence.

SENTINEL marked paced mouse and keyboard metrics HUMAN in all three sessions. Its reported average inter-key delays were 203, 205 and 196 ms. Standard/restored `fill()` produced no per-key timing sample, leaving keyboard metrics WAITING, yet still received 100 overall. The page also began at 100 before interactions. Its [scoring source](https://github.com/Ronit-Grover/sentinel-bot-detector/blob/main/src/utils/scoringEngine.js) normalizes over available signals, and its mouse path measure covers the whole multi-target trace. The result should not be described as validated human realism.

Incolumitas's `abs.incolumitas.com/lib.js` was blocked by ORB and its `/get` and `/mockData` requests failed. The score stayed `...` beyond the published 15-second scoring window, and the sample basket never populated. Input completion does not establish challenge completion there.

## Protocol and scope

- Existing `retail.behavior` standard/paced methods, Chrome 154.0.8037.98, headless, current JavaScript fingerprint backend. Canvas, WebGL, WebGPU, audio, worker, font, navigator and screen options enabled.
- Temporary US account/profile, no production accounts, no proxy. A profile stayed fixed within each batch. These measurements do not test US proxy reputation or location matching.
- Three standard → paced → restored blocks per site, each with a fresh context. Browser profile and task were held constant within the batch; detector code and controller parameters were not changed.
- SENTINEL: fixed twelve mouse targets in its tracking area, followed by two synthetic text fields. WebDecoy: synthetic email/message and the invisible-mode demo submission. Detection Lab: sandbox email/password fields, visible Remember me label, then Report; no login submitted. Incolumitas: published sample form and an attempted basket update.
- Two-second common readiness wait after navigation. Detection Lab additionally had a common three-second collection interval before opening Report. No synthetic DOM events, score-global changes, forged requests or threshold tuning.
- Paced tasks took approximately 19–20 seconds on SENTINEL/WebDecoy and 13.6–13.8 seconds on Detection Lab. Standard task times were approximately 0.6–0.7, 4.5–5.9 and 3.4–3.5 seconds respectively. Site scoring/network waits are included. Elapsed time, movement and typing all changed together: this does not isolate their individual causal effects.
- No manual human control, one host/network, one small task per site and three repetitions. Public heuristic demos do not establish retail acceptance, universal detection rates or a new untraceable identity.

## Reproduction and evidence

```powershell
.\.venv\Scripts\python scripts/behavior_web_audit.py --sites sentinel webdecoy incolumitas --repeats 3 --profile modified --output artifacts/behavior-web-audit
.\.venv\Scripts\python scripts/behavior_web_audit.py --sites detectionlab --repeats 3 --profile modified --output artifacts/behavior-web-lab
```

The script exits nonzero if input fails **or a site returns no score**; a successful measurement does not require a human verdict. A failing detector verdict remains in the output. An unavailable site is never converted to a passing score.

Saved evidence:

- `artifacts/behavior-web-2026-10-06/runs/`: first 27 runs, page text, screenshots, browser errors and request failures, exact settings and source hashes.
- `artifacts/behavior-web-2026-10-06/detectionlab-verified/`: nine completed Detection Lab comparisons with individual signal scores and screenshots.
- `artifacts/behavior-web-2026-10-06/detectionlab-runs/`: incomplete initial selector probe, excluded from results.
- `artifacts/behavior-web-2026-10-06/summary.json`: compact aggregate and explicit measurement-availability checks.

The runner compiled successfully and `git diff --check` found no whitespace errors. The live runs validate the new audit harness. No runtime pacing or dependency changes were made during this audit; the earlier functional/regression results remain documented in [behavioral input](behavioral-input.md). See the [library review](behavior-library-review.md) for candidate mouse and keyboard packages and why none was adopted.
