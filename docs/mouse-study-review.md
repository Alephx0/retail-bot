# Mouse movement study

The October 11 click-target study completed 24 trials and saved 13,399 coalesced pointer samples. All trials passed recorder validation. Eighteen movements were used for fitting; the first was a warm-up and five contained pauses longer than 150 ms. The local recorder captured only pointer coordinates, timestamps, target/viewport geometry, misses and click holds. No keyboard, account data or screenshots were recorded from the user's session.

The accepted movements had median travel time 859.2 ms and median distance 644.8 px. At half their movement time, they had typically covered 81.7% of the distance toward the target. The old symmetric motion curve covers 50% at that point. This supports an earlier acceleration phase followed by a longer final approach.

## Applied profile

Account browsing blends the fitted model equally with the original movement model. Travel time depends on distance, with bounded duration; a smooth time-warp changes acceleration and deceleration while retaining zero endpoint velocity and continuous acceleration. Curvature uses the measured median bend ratio with bounded random variation. It retains center-weighted click destinations, native target checks, cancellation and the current frame scheduler. The runtime loads aggregate parameters from encrypted settings, not recorded paths.

Click holds combine the first browsing demonstration and the new study: 48 measured clicks, with 10th percentile 84.9 ms, median 107.7 ms and 90th percentile 153 ms. Existing keyboard calibration is preserved. Slow/Balanced/Fast/Aggressive still scale timing. Checkout and automatic sign-in do not receive the profile.

Settings > Browser & fingerprints > Input timing > Demonstration timing shows the fitted-path count. Account browsing only enables it for new browsing runs; Off keeps the sample but uses the standard motion. Queued runs capture their profile when started.

## Measurements

| Leave-one-movement-out comparison | Original model | Fitted 50% blend |
| --- | ---: | ---: |
| Mean absolute normalized progress error | 0.16553 | 0.09604 |
| Mean absolute travel-time error | 269.35 ms | 226.48 ms |

These measure agreement with this single session, not a general smoothness score. The duration baseline uses midpoint random speed. Held-out movements are not an independent day, mouse or browsing environment. Progress is projected monotonically toward the click; hesitation, overshoot and micro-corrections are not replayed.

A controlled 971 px Chromium move took 793.2 ms with the original model and 957.5 ms with the fitted blend. It reached 50.4% versus 66.7% of the distance at half-time. Median delivered frame gaps were 16.6 ms and 16.5 ms; 95th-percentile gaps were 17.7 ms and 17.8 ms. Both ended exactly at the requested point. This demonstrates the intended longer approach while maintaining similar browser cadence. It does not promise a higher refresh rate or eliminate OS/browser scheduling stalls. See `artifacts/account-care-review/pointer-motion-measurement.json`.

Reproduce analysis with `python scripts/analyze_mouse_study.py artifacts/mouse-study-20261011-164541`. The recorded aggregate fit is in that folder's `summary.json`. The click study can be repeated with `python scripts/capture_mouse_study.py --output artifacts/<fresh-study-folder>`; it finishes automatically after 24 targets, or can be paused/stopped early.

## Verification

Tests cover recorder connection, trusted/coalesced paths, pause/resume, completion, privacy allowlisting, fit exclusions and held-out error; valid model bounds, continuous/monotonic endpoint behavior, normal and stalled browser transport, all four speed profiles, scope isolation and frozen copies; real browser movement and cancellation with no click; target accuracy for tiny, clipped and bordered controls at desktop and narrow widths; settings save/reload and keyboard/browser input regression. No real retailer requests or purchases are used. Study and settings screens were visually checked at desktop and 390px widths.

Commands:

Final result: 34 distinct tests passed across the commands below and one targeted rerun. The rerun corrected a fixture that read wheel totals before the last wheel event arrived; it now waits for observable delivery. No production wait or retry was added for that test correction.

```text
python -m pytest tests/test_mouse_study.py tests/test_pointer_motion_browser.py -q
python -m pytest tests/test_mouse_calibration.py tests/test_pointer_smoothing.py tests/test_behavior.py tests/test_settings.py tests/test_browsing_calibration.py -q
```
