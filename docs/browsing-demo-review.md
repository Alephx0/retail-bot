# Browsing demonstration review

This describes the initial free-browsing calibration. The subsequent [mouse movement study](mouse-study-review.md) adds a fitted movement profile and combines the click samples from both sessions.

The completed October 11 capture saved 9,584 events: 6,892 pointer samples, 53 clicks, 48 key releases, 335 wheel events and 2,241 scroll events. Only timing, geometry and broad interaction/page categories were recorded. The first recorder saved no samples; all results below use the corrected replacement capture.

| Measurement | Samples | 10th percentile | Median | 90th percentile |
| --- | ---: | ---: | ---: | ---: |
| Character key hold | 36 | 51.1 ms | 83.3 ms | 103.0 ms |
| Character gap under one second | 32 | 39.4 ms | 80.6 ms | 179.8 ms |
| Click with a measured pointer hold | 24 | 76.0 ms | 110.4 ms | 161.4 ms |
| Scroll burst distance | 112 | 100 px | 300 px | 600 px |

The recording includes variants and product-details interactions. Clicking at varied positions supports retaining center-weighted click destinations. Pointer bursts included exploration, reversals and pauses; their median speed is not a reliable estimate of deliberate point-to-point travel. The existing smooth motion curve remains in use. This is one short demonstration, not a trained personal mouse model.

## Applied behavior

Per the user's selected scope, demonstration timing applies only to account browsing. Settings > Browser & fingerprints > Input timing > Demonstration timing selects Account browsing only or Off. Turning it off keeps the saved aggregate sample. The profile is stored in the encrypted settings record and captured when browsing starts, including runs queued for browser capacity. Existing sessions keep their timing. Purchasing and automatic sign-in do not load this profile.

For each character, half of the timing comes from HumanTyping and half from a bounded sample distribution built from the measured percentiles. Click holds use the same conservative blend. Slow/Balanced/Fast/Aggressive still scale those durations. Raw input traces are never replayed or loaded by the runtime. Center-weighted target positions and native actionability checks remain in use.

Account browsing no longer repeats +360, +360, -160 pixels at three evenly spaced intervals. Reading pauses and scroll distances vary within the saved page-reading duration, adapt to page bounds, and sometimes revisit content. A pointer already within the central viewport is retained when scrolling. Wheel-burst duration varies, and all waits remain cancellable. The AI prompt asks for a useful mix of supported interactions based on interests and observed content instead of a repeated ordered checklist. This prompt change is not evidence of improved live model choices.

## Verification and limits

The local Chromium comparison typed the same 29-character fixture phrase and made five clicks per mode. Measured median key holds were 72.8 ms before and 75.3 ms with calibration; click holds were 100.8 ms and 104.8 ms. These samples include browser/OS overhead and are not live retailer speed measurements or a guarantee of matching a person's timing. The conservative blend intentionally produces a modest adjustment. Results are in `artifacts/account-care-review/calibrated-input-measurement.json`.

Reproduce the capture summary with `python scripts/analyze_browsing_demo.py artifacts/browsing-demo-20261011-v2`. Pointer bursts split at 200 ms gaps and wheel bursts at 250 ms. Page markers are not unique visits. Navigation may lose the last fraction of a second of buffered input. Only search-box key timings are available, and incomplete click pairs and long typing pauses are excluded from the relevant timing summaries.

Regression command: `python -m pytest tests/test_browsing_calibration.py tests/test_settings.py tests/test_account_care.py tests/test_behavior.py tests/test_pointer_smoothing.py -q`. Checks cover scope isolation, frozen queued runs, sample validation, focused settings edits, unchanged standard typing, speed-profile scaling, real trusted browser input, varied reading schedules, cancellation, pointer placement, account independence, and desktop/narrow settings flows. All retailer traffic and inputs in tests are controlled fixtures; no purchases or live AI calls are made.

Final validation: 42 distinct tests passed across that command and the settings UI rerun. The initial UI assertion tried to inspect an intentionally collapsed section after tab navigation; opening it before inspection fixed the test. The additional `tests/test_care_product.py tests/test_care_agent.py` run passed 29 tests, including the inline-radio fix, immediate/delayed/missing/interrupted transitions, cooldowns and unsupported actions. Total: 71 passed. Desktop and 390px screenshots were inspected. The saved live settings select browsing-only calibration and retain every unrelated settings value.
