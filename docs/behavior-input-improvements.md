# Input corrections and APIVoid audit — 2026-10-06

The updated input and navigator profile improve measured results, but do not achieve near-perfect results across all detectors. APIVoid now reports **5/100 risk, Likely Human, Tampered: No** across three sessions. Nine CreepJS runs passed the strict zero-lie, zero-warning and worker-consistency checks.

## Runtime changes

Paced ASCII typing now checks editability and performs a locator/element click before entering text. This makes the initial interaction respect overlays, disabled controls, stability and pointer actionability. Previously, `press()` could focus the field without those checks. The distinction follows [Playwright's actionability documentation](https://playwright.dev/python/docs/actionability). Each later press still targets the requested field, and the final value must match exactly.

Uppercase letters and shifted US punctuation now use physical key chords, such as Shift with a letter or digit key, including paired modifier releases. Previously, a literal uppercase character could be emitted without a Shift event. Unicode and specialized input types retain native fill semantics. No typos, extra submission keys, detector changes or new dependencies were introduced.

The dataset measurement script now reports modifier holds separately. Overlap between Shift and a letter must not be counted as overlap between two consecutive typed characters.

The US navigator-profile option now derives the User-Agent from the installed browser and supplies it to the owned account process with `Chrome/` replacing `HeadlessChrome/`. Chromium's launch option applies the identity across headers, pages and workers while retaining native Client Hints. The browser stays headless and the other transformations stay enabled. A context-only override was rejected after it caused two CreepJS lies in service-worker identity; the final process-level implementation passed the regression checks.

Regression coverage checks that an overlay prevents typing, read-only fields reject input, every tested US shifted symbol produces the correct text, key/modifier events balance, Shift is released, ElementHandle input works, and final purchase details are revalidated after pointer preparation.

## Final detector comparison

| Test | Earlier result | Final result |
|---|---|---|
| APIVoid, lower risk is better | HTTP 403; no score | 5, 5, 5 / 100; Likely Human, Tampered: No |
| WebDecoy, lower bot score is better | 0.21632, 0.23232, 0.21632; allowed | 0.08912, 0.08912, 0.10512; allowed 3/3 |
| Detection Lab, higher human score is better | 68.0, 65.0, 66.4; MARGINAL 3/3 | 70.4, 70.9, 69.5; PASS 2/3, MARGINAL 1/3 |
| SENTINEL, higher human score is better | 100 in both standard and paced modes | 100 in all 9 final sessions; does not distinguish the modes |
| CreepJS strict checks | Previous profiles had zero lies | Zero lies, warnings and captured errors in all 9 final checks |

The CreepJS sample covers navigator-only, all eight transformations combined, and restored-off, each repeated three times. It is not a fresh exhaustive test of every configuration. The APIVoid sample uses one fresh context for each input mode; the other behavioral sites use three contexts per mode. All transformations remain enabled for the full-profile tests. No detector code, reported result, or challenge response was altered.

Detection Lab's final paced fingerprint and hover-before-click metrics were 100 in all three runs. Keyboard timing was 99, 75 and 75. Its event-ordering and stealth-artifact metrics remain low. WebDecoy still reports a CDP-related experimental category. Accepted sessions are not equivalent to zero detections.

## Input-only intermediate comparison

The task and modified fingerprint profile match the previous audit: two synthetic sandbox fields, Remember me, then the site's Report. There is no login submission. All runs use headless Chrome, the JavaScript fingerprint backend, all eight fingerprint options enabled, and no proxy. Standard/paced/restored modes each get three fresh sessions. The account ID deterministically selects the same profile across batches.

| Measurement | Earlier paced controller | Updated paced controller |
|---|---|---|
| Overall human score | 68.0, 65.0, 66.4 | 68.7, 68.6, 67.5 |
| Verdict | MARGINAL, all three | MARGINAL, all three |
| Hover before click | 47, 47, 47 | 100, 100, 100 |
| Keystroke timing | 85, 95, 75 | 75, 75, 75 |
| Event ordering | 0, 0, 0 | 0, 0, 0 |

These small samples support a better hover-before-click result, not a broad or statistically established detection-rate improvement. Keyboard timing did not improve on this metric. Source hashes and raw observations are retained; the two controller versions were measured in separate batches, not randomized contemporaneous trials.

The [public collector](https://www.detectionlab.app/static/js/instrumentation.js) describes keydown/keyup pairs but the downloaded implementation registers only a keydown listener. This limits interpretation of an event-ordering check described as validating key pairs. It does not prove that missing keyups are the sole cause of the low server score. Our browser regression checks independently verify balanced key and modifier events. The site's collector and server result are left intact.

The short task does not exercise scrolling, reading, dragging, retry behavior or tab switching. Some corresponding metrics are missing/defaulted; an overall score includes those along with browser properties. Adding irrelevant activities to maximize a demo score would not establish correctness on real application tasks.

## APIVoid final result

[APIVoid's test](https://www.apivoid.com/tools/bot-detection-test/) assesses browser/network fingerprints automatically. It is not a keyboard or mouse realism benchmark. Its published description says the score combines server-side rules and IP information, with individual rules not exposed.

The original headless User-Agent received `403 Forbidden` before the detector loaded, with modifications disabled or enabled. An isolated Chrome User-Agent diagnostic received HTTP 200, implicating the User-Agent marker in that access behavior. The initially tried context override scored 17/100 with the modified profile, but introduced service-worker inconsistencies on CreepJS. It is not the final implementation.

The final process-level identity produced **5, 5, 5 / 100 risk** in standard, paced and restored input modes. All three reported **Likely Human**, **Tampered: No**, and one hardware/display warning. Network/request, browser/engine and browser-integrity categories passed. APIVoid does not reveal the individual server rule responsible for the remaining warning, so it is not assigned a speculative cause or hidden. Pacing cannot explain this improvement: APIVoid performs an automatic browser/network assessment without a typing task.

APIVoid is now an allowlisted target in `scripts/behavior_web_audit.py`. The runner records HTTP failures and exits nonzero when the test cannot produce a score:

```powershell
.\.venv\Scripts\python scripts/behavior_web_audit.py --sites apivoid --profile modified --repeats 1 --output artifacts/apivoid-audit
```

The score parser was validated against these live successful responses. HTTP failures and timeouts remain failed measurements, never passing scores.

Evidence is under `artifacts/behavior-improvement-2026-10-06/`: `apivoid-final/` contains final captures; `creepjs-verified/` contains nine strict fingerprint checks; `after/` contains the input-only intermediate measurements; and `final-behavior/` contains the final behavioral comparisons. Initial access failures and rejected context-override experiments are retained separately. These tests do not establish US proxy compatibility or retailer acceptance.

Final regression validation completed on 2026-10-07: **205 tests passed**, with zero failures, errors or skipped tests (`final-pytest.xml`). Coverage includes input actionability, balanced key events, worker identity and service-worker restart consistency. The JavaScript syntax check and `git diff --check` also passed. These regression results verify the tested behavior; they do not establish universal detector acceptance.
