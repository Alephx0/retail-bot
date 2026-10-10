# Monitor and task timing, 2026-10-10

The shared anonymous monitor now reports inventory without resolving purchase
actions or invoking AI recovery. Assigned tasks still verify their own signed-in
offer, delivery eligibility, seller, quantity and price before carting. Anonymous
inventory is a wake-up signal, not checkout authorization.

## Findings and changes

- Recent application logs showed completed stock scans of 9.812–12.236 seconds,
  followed by the configured 4.5-second pause. These older logs have no substep
  timings; they cannot establish which component consumed each second.
- Full product inspection previously ran purchase-control resolution in monitors.
  A generic header “Cart” button could trigger AI recovery even on pages explicitly
  saying “In Stock” or “Currently unavailable.” Inventory scans no longer use it.
- Product metadata and visible stock/delivery evidence now come from one browser
  evaluation. Seller checks and action validation retain their stricter rules.
- A verified account browser now waits up to one second for the concurrent first
  stock observation (previously 100ms). Stock arriving just after sign-in can reuse
  that browser. Out-of-stock releases it immediately; slow scans release it at the
  deadline. Monitor-only tasks retain the 100ms cutoff. This trades at most 900ms
  of additional initial worker occupancy for avoiding a second browser launch and
  sign-in. A controlled delayed-scan test verifies one context and one sign-in.
- No polling rate increases, challenge bypasses or transaction-guard changes.
  Retailer backoffs, account isolation, cancellation and submission journals remain.

## Measurements

Read-only Amazon B07ZLF9WQ5 canaries used an anonymous monitor identity, Chrome,
the configured fingerprint options, no custom extensions, no AI provider and no
cart/checkout. They reported “In stock” plus delivery/sign-in restrictions.

| Component | Earlier full inspection | Inventory scan before batching | Inventory scan after batching |
| --- | ---: | ---: | ---: |
| Navigation | 2.851 s | 1.878 s | 2.781 s |
| Page access checks | 0.787 s | 0.663 s | 0.767 s |
| Product details and controls | 2.080 s | 1.415 s | 0.892 s |
| Total scan | 5.718 s | 3.956 s | 4.439 s |

Each column is one observation, not a statistical comparison. The first two share
a browser; the third uses a fresh browser. Network/cache variation is material.
Cold monitor startup was 4.87–4.94s, including a 3.22–3.26s hardware probe. The app
already caches/prewarms this probe; these cold costs do not recur each scan.
Navigation is the largest remaining recurring component. No end-to-end live
account checkout was timed, since verification must not place real orders.

Controlled browser fixtures, five samples per case, median milliseconds:

| Page | Original full inspection | Final inventory scan |
| --- | ---: | ---: |
| In stock, sign-in required | 382.6 | 95.5 |
| Out of stock | 389.2 | 102.1 |
| Normal purchase control | 124.9 | 99.1 |

The first two fixtures deliberately simulate a **250ms failed AI lookup**. They
demonstrate removed work, not Amazon response times or production AI latency.
Browser requests are intercepted locally, and no provider requests are sent.
Run `python scripts/benchmark_monitor.py --samples 5` for the current full-versus-
inventory comparison. The current full path also benefits from batched metadata.
Original and intermediate captured samples are in the local artifacts:
`monitor-performance.json`, `monitor-performance-batched.json`,
`monitor-live-timings.json`, and `monitor-live-batched.json`.

The timing recorder measured 2.5 microseconds per empty stage across 20,000 stages
on this machine (single run, including loop overhead), retaining only 2,048 samples.

## Using the timings

Open a monitor's activity log or a task's details, expand **Step timings**, and
select **Refresh timings**. The table shows latest, average, longest and count;
in-progress stages show elapsed time. Includes setup, extension/session restore,
worker/account queues, navigation, access checks, product reads, stock waiting,
account verification, carting, checkout review, submission, confirmation and
cleanup. User-review waits and polling pauses have separate labels.

Timings are bounded, in memory and reset on restart. They contain IDs and stage
names, never URLs, arguments, credentials or exception text. They are fetched
on demand from `/api/performance?kind=task&id=…` (or `kind=monitor`), not added to
every dashboard state update. Task summaries can include multiple runs while their
samples remain in the shared 2,048-entry window. Parent timings include substeps;
do not sum them. Stock signal age is observation age at task selection, not a
standalone scheduler-latency measurement. An error in a substep can be recovered
by its parent and does not necessarily mean the whole task failed.

## Validation

89 tests passed with:
`python -m pytest tests/test_performance.py tests/test_monitors.py tests/test_browser.py tests/test_seller_layouts.py tests/test_login.py tests/test_checkout.py tests/test_checkout_navigation.py tests/test_core.py tests/test_task_group_execution.py -q`.
The only warning is the existing Starlette/httpx deprecation. JavaScript syntax
checks and `git diff --check` passed.

Controlled tests cover inventory/purchase eligibility separation, hidden and
disabled controls, unknown stock, no monitor AI/action resolution, challenges and
rate limits, account-specific rechecks, concurrent monitors, startup handoff,
cancellation, resource release, timing isolation/bounds and API filtering.
Checkout and execution regressions cover uncertain outcomes and duplicate guards.
Playwright exercises monitor/task timing dialogs, refresh, narrow layout, Escape
and focus restoration. MCP visual inspection used a fixture workspace with timing
API responses supplied locally; integration browser tests use the real API.
