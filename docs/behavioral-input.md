# Behavioral input research and Patchright pacing

The [live website comparison](behavior-web-results.md) records repeated standard/paced/restored runs on public behavioral demos. The [library review](behavior-library-review.md) assesses mouse and keyboard packages against this application's async Patchright requirements.

The subsequent [input corrections and APIVoid audit](behavior-input-improvements.md) records physical Shift chords, field actionability checks, updated scores and the APIVoid access limitation.

Enable **Settings > Browser > Mouse, scroll and typing pace > Paced interactions** for new account browser contexts. The default is Standard Patchright input (`interaction_pacing: off`). Pacing adds latency and does not change fingerprint settings, proxies, account permissions or challenge handling.

## Research sources reviewed

| Source | Useful evidence | Limits and decision |
|---|---|---|
| [Balabit Mouse Dynamics Challenge](https://github.com/balabit/Mouse-Dynamics-Challenge) | Mouse coordinates, button states, and separate client/recording timestamps from remote-desktop work. | Authentication/identification benchmark, not human-versus-bot browser data. Analyze client timestamps in seconds; ignore nonpositive time deltas for speed. No standalone LICENSE found at the inspected revision; data stays in ignored local research artifacts. |
| [CMU keystroke benchmark](https://www.cs.cmu.edu/~keystroke/) | Key hold, keydown-to-keydown and keyup-to-keydown timing across 51 participants. | One password typed 400 times per participant, not arbitrary web forms. Negative keyup-to-keydown times represent overlapping keys. Use aggregate measurements, not a participant's biometric template. |
| [CITeR keystroke and mouse dataset](https://github.com/CITeR-Research/Dataset-for-Keystroke-Dynamics-and-Mouse-Movements) | Transcription/free typing, different keyboards, and mouse event data. | Requires a database release agreement; not downloaded or incorporated. |
| [BEACON logger](https://github.com/beacongui/beacon-logger) | Synchronized keyboard, mouse/scroll and other gameplay streams. | Gameplay differs from forms; current logger advertises CC BY-NC-ND 4.0. Not incorporated. Logger license does not establish the dataset's reuse terms. |
| [Ghost Cursor](https://github.com/Xetera/ghost-cursor) | Curved pointer paths, travel based on distance/target size, overshoot and configurable delays. | MIT; oriented toward Puppeteer. Useful design comparison, but no code/dependency is imported and its claims are not a measured guarantee. |
| [Humanization-Playwright](https://github.com/saksham-personal/humanization-playwright) | Existing Patchright-facing movement, clicking, scrolling and typing helpers. | MIT; adds its own launch/logging assumptions. Reviewed rather than installed so the current account context ownership and input checks stay intact. |

Balabit citation: Fülöp, Á., Kovács, L., Kurics, T., Windhager-Pokol, E. (2016), *Balabit Mouse Dynamics Challenge data set*. CMU citation: Killourhy, K. S. and Maxion, R. A. (2009), *Comparing Anomaly-Detection Algorithms for Keystroke Dynamics*, DSN.

## Reproducible dataset analysis

```powershell
.\.venv\Scripts\python scripts/analyze_behavior_datasets.py --output artifacts/behavior-research
```

The analyzer pins Balabit to `d00d6f779254a2a917deeab4a5b7a9e8643bd91e`. It downloads the first lexicographic training session for each of ten users, plus CMU's published CSV. Every download has a URL, byte count and SHA-256 manifest. Downloads are bounded, cached locally and never executed. No third-party traces, learned models or subject profiles ship with the application.

The inspected mouse sample contains 350,828 rows. It yields 248,112 positive-time moving intervals, 11,220 short stationary button holds and 8,805 scroll intervals of at most one second. The parser separately reports 36,384 nonpositive movement-time deltas instead of dividing by zero or manufacturing velocities. Click measurements require at most five recorded pixels of displacement and at most two seconds of hold. Scroll records provide direction and timing but no browser wheel delta. These filters select subsets, not the full range of human behavior.

| Measurement | Sample p05 | Sample median | Sample p95 |
|---|---:|---:|---:|
| Balabit movement interval, ms | 15 | 93 | 265 |
| Balabit speed, recorded pixels/sec | 9.17 | 180.25 | 1691.43 |
| Balabit click hold, ms | 62 | 94 | 156 |
| Balabit scroll interval, ms | 15 | 109 | 577.8 |
| CMU key hold, ms | 49.3 | 86.1 | 144.8 |
| CMU keydown interval, ms | 77.9 | 191.1 | 602.1 |

CMU contains 20,400 trials and 22,118 overlapping transitions. Remote-desktop timestamps are affected by capture/transport and their pixel units are not calibrated browser CSS pixels. Event-weighted pooled statistics also overweight more active participants; the output retains per-user mouse summaries. The sample is not a held-out human/bot classifier evaluation.

## Runtime behavior

`retail/behavior.py` uses the existing Patchright page/context and its public input APIs. Pointer travel uses a smooth acceleration/deceleration curve with bounded lateral variation and variable event intervals. Clicks have bounded dwell. Offscreen controls receive a limited sequence of wheel events followed by native scroll-into-view for nested or blocked scroll containers. Wheel events do not themselves guarantee scrolling has finished; the target is checked again before interaction ([Playwright mouse API](https://playwright.dev/python/docs/api/class-mouse)).

Simple ASCII text fields use individual locator key presses with variable hold and inter-key delays. Before typing, the controller checks editability and clicks the field through native actionability checks, so an overlay blocks the initial interaction. Every press targets the intended field. Uppercase letters and shifted US punctuation use explicit Shift chords, with paired modifier release. Complex Unicode, control characters, values over 128 characters and specialized input types use native `fill()` semantics. Existing field contents are cleared natively. The final value is checked after paced typing. The implementation deliberately does not introduce typos, press Enter, overlap consecutive character keys or replay any person's trace. Modifier timing is measured separately from character timing in dataset comparisons.

These are bounded engineering defaults, informed by research and compared with the data; they are not fitted biometric distributions. Production randomness is per controller/session, while tests can supply a seed. Mouse movement is capped at 48 points per segment, scrolling at eight three-event batches, preparation at 12 seconds, a click at 15 seconds and filling at 45 seconds. Operations on one page share a lock and respect asyncio cancellation. The layer does not retry failed clicks or record input values. Existing optional browser traces remain a separate application feature.

Locator and ElementHandle actionability checks remain responsible for actual clicks. Trials use the native path; a native trial can generate a mouse move. Manual dashboard input remains direct. Automated login, OTP, registration-field filling, cart/quantity actions, checkout navigation and supported card-verification fields use the opt-in controller.

For final order submission, preparatory travel happens **before** the existing last price/item/seller/quantity validation. The final submission click retains native timing, with no added paced delay after financial checks. The submission journal and no-retry behavior remain in force. Cancellation or failure after an actual click has started can still have an uncertain outcome, just as with ordinary browser input.

## Browser validation

```powershell
.\.venv\Scripts\python -m pytest -q tests
.\.venv\Scripts\python scripts/behavior_differential.py --repeats 3 --output artifacts/behavior-audit
```

The differential audit uses isolated synthetic accounts and a routed fixture origin; every request in its account context returns fixture HTML. It performs standard, paced and restored input in three fresh sessions each. It saves actual event timestamps, movement, wheel deltas, key codes (only fixture text), trust flags, completion count and duration. It checks exact field values, a single completion, the expected paced events and restoration of standard fill behavior. Runtime application sessions have no such event recorder.

Regression tests additionally cover literal symbols/Unicode/numeric fields, blocked targets, both Locator and ElementHandle controls, cancellation during preparation, settings validation, and a price changing during pointer motion. The latter must prevent order submission. Settings persistence is also checked in a temporary dashboard.

The measurements establish functional input behavior on the tested browser. They do not show statistical equivalence to humans or success against retail behavioral classifiers. Movement speed and event intervals differ from Balabit, and sequential typing omits overlap and long reading/thinking pauses present in real use. Mouse/keyboard pacing cannot establish a fresh identity or eliminate network, account-history or automation signals.

## Direct distribution comparison with a second keyboard dataset

[IKDD](https://github.com/MachineLearningVisionRG/IKDD) is a comparable keyboard repository from the authors of *IKDD: A Keystroke Dynamics Dataset for User Classification* (Tsimperidis et al., 2024). It groups observed key-hold and down-down digram times in milliseconds. The reference already excludes holds over 500 ms and digram delays over 3000 ms. The comparator applies the same limits to the browser samples. Header/demographic fields are ignored. No standalone repository license was found at the inspected revision; raw files stay in ignored local artifacts.

```powershell
.\.venv\Scripts\python scripts/behavior_differential.py --repeats 5 --output artifacts/behavior-comparison/browser
.\.venv\Scripts\python scripts/compare_behavior_datasets.py --browser artifacts/behavior-comparison/browser --research artifacts/behavior-research --output artifacts/behavior-comparison/comparison
```

The comparator pins IKDD to `5f271e3256554bcd819b54e96a287b6512fb02aa` and selects ten evenly spaced user IDs, using each user's first lexicographic session. Downloads have SHA-256 manifests. Browser key intervals are measured within a single field; mouse travel between fields is excluded. Empirical-CDF distance is reported without an iid significance test, bot-score interpretation or human-equivalence threshold. Consecutive events are correlated and the recording environments differ.

The 2026-10-06 run completed **15/15 browser sessions** and **8 focused tests**. The IKDD sample supplied 29,523 hold times and 30,527 digram delays. The earlier full repository suite passed 200 tests; this follow-up changes measurement scripts/tests, not runtime pacing.

| Timing metric | Reference median, ms | Paced median, ms | Empirical-CDF gap |
|---|---:|---:|---:|
| Balabit movement interval | 93.0 | 31.3 | 0.513 |
| Balabit scroll interval | 109.0 | 76.6 | 0.536 |
| IKDD key hold | 93.0 | 75.0 | 0.301 |
| IKDD consecutive-key delay | 217.0 | 184.4 | 0.429 |

The functional run succeeds while the timing distributions differ substantially in these samples. IKDD's 95th-percentile consecutive-key delay is 1251.7 ms versus 233.6 ms for paced input. CMU has 10.84% overlapping transitions versus 0% here. Five measured clicks cannot establish a click-duration distribution. Balabit speed is omitted from the statistical comparison because recorded pixels and browser CSS pixels are uncalibrated. Full standard/paced/restored comparisons are saved under `artifacts/behavior-comparison-2026-10-06/comparison/`.
