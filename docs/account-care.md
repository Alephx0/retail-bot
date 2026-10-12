# Account care

The Accounts → Sessions & relationships tab now prioritizes manually started, automated browsing routines. Configure one account or the selected/visible accounts, then Start. A blank product list visits Amazon home; a populated list visits up to the configured number of unique ASINs in saved order. Each page receives a fresh random reading duration between the saved minimum and maximum (15-120 seconds), with paced pointer and wheel input. The chosen duration appears in current activity and completed-page history. Each run visits at most 10 pages. Amazon US, UK and Canada use the account's region; other retailer adapters are not implemented.

The workspace includes account folders, search, attention/active/configured filters, explicit selection, bulk start/stop, per-account progress, latest-run page history, session open/verify/save/close, and existing folder/network/profile relationships. Filters clear selection. Bulk actions use visible accounts unless accounts are explicitly selected. Individual failures stay visible in a result dialog. Historical saved-session information is distinguished from current browsing status.

## Research applied

Reviewed AYCD's official materials on 2026-10-11:

- [AYCD](https://aycd.io/): isolated sessions, account tools and activity automation informed the single account-care workspace.
- [TabSentry tabs](https://aycd.zendesk.com/hc/en-us/articles/18233128854935-TabSentry-Tasks-Tabs): categories and per-session statuses informed folders, filters and progress.
- [Task Assistant](https://aycd.zendesk.com/hc/en-us/articles/18228383559831-TabSentry-Tasks-Task-Assistant): account-specific context informed the routine dialog's session and profile details.
- [TabSentry settings](https://aycd.zendesk.com/hc/en-us/articles/18345131620631-TabSentry-Settings): activity-generator concurrency controls informed reuse of Retail Desk's existing shared browser capacity.

This implementation uses Retail Desk's own browser factory, encrypted vault and APIs. It does not integrate AYCD, record/replay purchases, create a trust score or claim browsing improves retailer account standing. Repeated schedules, overnight activity and automatic restarts are not implemented; a user starts each bounded run.

## Execution and ownership

`retail/account_care.py` is owned by the existing Engine. There is no new scheduler. Runs acquire the same account lock and browser semaphore as tasks and saved plans. Idle cached browsers can yield capacity. An active task, reservation, open account browser, unresolved order submission or cooldown prevents starting a care run. Account browser actions also reserve the shared lock while opening, closing or verifying, covering concurrent start requests.

Each run freezes its routine and account configuration at startup. Routine changes are rejected while active. Editing the active account, its proxy list or global browser configuration is blocked. Saved-product mode navigates to canonical Amazon product/home URLs. AI mode uses the selected provider to choose searches and observed product links through process-local MCP tools and a CDP session scoped to the owned page. All runs explicitly open a visible browser and force paced input; AI searches use HumanTyping and clicks use the existing center-weighted movement controller. Neither mode invokes cart, checkout, credential typing, OTP or challenge solving. Sign-out, verification, denial and connection errors stop the run; HTTP cooldowns retain the full Retry-After and prevent an early restart. There is no automatic retry.

Successful runs save cookies and session storage back into the existing account vault without replacing unrelated account fields. Routine and latest-run records use separate `care-routine-` and `care-run-` ID prefixes because the vault's record IDs are globally unique. No credentials are copied into routine/run records. Existing accounts and settings require no migration. Interrupted runs are marked for review on boot and never restarted automatically.

Stop cancels queued work or closes the owned browser before releasing its account and capacity. Repeated stop requests share one cleanup operation. Stop arriving during natural teardown waits for teardown. If browser closure fails, ownership is retained and Stop retries cleanup. The global Stop all action and shutdown also stop account-care work.

## Verification

Run with the workspace Python environment:

```powershell
python -m pytest tests/test_account_care.py tests/test_stop_cleanup.py tests/test_redesign.py tests/test_matching.py tests/test_session_verification.py -q
```

Controlled tests cover record/account independence, frozen runs, shared capacity, duplicate start and stop, cancellation before startup and during cleanup, cleanup failure/retry, cooldowns, signed-out/challenge/foreign-origin documents, settings/account/network edit guards, interrupted-run recovery and desktop/narrow UI flows. Tests do not use live retailer accounts or place purchases.

Playwright screenshots and a reproducible measurement are generated under `artifacts/account-care-review/`. One local Chromium fixture sample took **16.307 seconds**, including **15 seconds of intentional reading**, with **one GET**, **56 scroll events**, and **zero purchase clicks**. This is a correctness/pacing measurement, not a live Amazon speed benchmark; it excludes real network variability and the normal identity-browser startup cost.


## Responsiveness and AI browsing update

The previous dashboard decrypted every diagnostic record, including 1.126 GB of encrypted traces in the live workspace, on every `/api/state` poll. This blocked the event loop even for static requests: observed samples were 27.286 seconds for state and 37.818 seconds for the account-care script. A compact encrypted diagnostic metadata index now supplies the dashboard. Old evidence is indexed in resumable background batches with a separate database connection; the original encrypted records remain untouched. New evidence and summaries are saved atomically. Detailed evidence is still loaded on demand. Account care uses a compact polling endpoint.

Start acknowledges the queued run; Stop returns HTTP 202 after requesting cancellation. UI buttons immediately show pending requests, with separate opening/loading/session/AI/input/saving/closing statuses and elapsed step time. Account ownership stays reserved until browser teardown completes. A Show browser action brings the owned window forward. Context setup reports actual startup stages rather than labeling all startup as session checking.

Choose **AI browsing (MCP/CDP)** in Set routine, enter interests, choose an AI connection (or inherit the Settings default), and save. Existing product routines remain the default. Product lists are preserved when switching modes. The provider receives interests and a bounded list of visible product titles/ASIN references, plus supported option labels, expanded product details (up to 1,600 characters), and aggregate review ratings/counts after inspection; not cookies, screenshots, account header text, reviewer identities or the whole DOM. Provider usage may incur charges. Model decisions have a 25-second deadline; page visits and tool names/arguments are bounded. Untrusted page content cannot grant new tools. The agent can search, open an observed product, inspect an allowlisted product interaction, or finish; it cannot execute arbitrary JavaScript or attach to other tabs. Its CDP session is created on the existing account browser, without opening a remote-debugging listener or a second browser controller.

This explicit account-care mode is separate from deterministic purchasing/recovery. It uses the installed MCP SDK's process-local client/server transport; checkout runtime remains unchanged. See the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) for transport and tool APIs. It does not require an external MCP server or the global external-CDP browser setting.

Additional checks:

```powershell
python -m pytest tests/test_account_care.py tests/test_care_agent.py tests/test_diagnostic_index.py tests/test_stop_cleanup.py tests/test_session_verification.py tests/test_settings.py tests/test_behavior.py -q
```

The controlled slow-close fixture measured Start acknowledgement at 20.39 ms and Stop acknowledgement at 5.59 ms with a one-second browser teardown. These numbers measure API responsiveness, not live retailer startup, model latency or complete teardown. See `artifacts/account-care-review/control-measurement.json`. Real-browser MCP fixtures cover typed searches, paced product clicks, cancellation during model decisions, unsupported tools, no purchase requests, private-header exclusion, and full Retry-After preservation. Diagnostic-index checks cover restart/resume, encrypted evidence preservation, replacement, bulk writes, pruning and deletion.

Validation results: the main regression command passed 80 tests with one skipped; the final account-care and MCP tests passed 19 tests after the last changes. The broader browser checks exposed a checkout fixture missing stock evidence. Adding its visible In Stock text restored the intended anonymous-monitor wakeup, preserving read-only monitoring and the account-side unfamiliar-control recovery test; both variants and the recovery suite passed (8 tests). Desktop and 390px-wide screenshots were inspected.

After restarting port 8789, five read-only local samples measured median state responses of 267.61 ms, account-care responses of 133.45 ms, and the static care script of 18.91 ms. The migration indexed all original diagnostic records without deleting their evidence, and the dashboard returns the latest 100 summaries. These samples were taken during/around background indexing and do not measure browser launch, AI provider or retailer latency. All live tasks and routines were stopped.


## Search compatibility and varied reading

The original search tool accepted only the exact `/s` form action and restricted punctuation in search phrases. It now also accepts same-origin GET search forms with `/s/ref=...` attribution suffixes and printable single-line phrases, including apostrophes and currency symbols. Unsupported methods/endpoints still stop before input. Observed product references retain the observed DOM element, so inserting other links cannot redirect a click to a different element. Changed or removed product links stop with a specific explanation.

Tool errors now identify missing controls, unsupported forms, changed links or the timed-out action without exposing raw browser/provider data. They do not mark the saved login invalid. No navigation click is retried. The previous live error discarded its underlying exception, so its exact cause cannot be recovered retrospectively; these fixes cover the reproduced search and link failures.

Set routine now has minimum and maximum reading times; maximum must exceed minimum. Both browsing modes sample a fresh duration for every visit from the frozen run configuration. Existing `seconds_per_page` records load as a bounded +/-25% range (30 becomes 22-38 seconds), without modifying original vault records. Explicit ranges survive save/reload. Reading waits remain cancellable and are not used to infer page readiness.

Regression command: `python -m pytest tests/test_account_care.py tests/test_care_agent.py -q`. Fixtures cover canonical and attributed search forms, punctuation, inserted/changed links, missing controls, immediate/delayed/missing/interrupted navigation, reading-range validation and old-record compatibility, per-visit variation, frozen ranges, and desktop/narrow UI editing. All retailer responses and AI decisions in these checks are fixtures; they do not measure live provider or Amazon reliability.


## Product interactions through MCP/CDP

AI mode now discovers supported product controls with CDP and exposes `inspect_product(ref)` through the existing account-owned MCP session. Available actions include clickable variation swatches, ASIN-backed native variation dropdowns, See more product details links/expanders, and customer-review sections. Unsupported controls, selected/unavailable options and off-site/purchase targets are excluded. References retain the observed element and are revalidated after scrolling. The model receives option labels, limited expanded product details and aggregate rating/count metadata; review authors and review prose are not exported.

The agent is instructed to explore available features before leaving a product. Each product visit permits up to four interactions, including at most two different variants; details and reviews are each inspected once. Variant navigation stays within the current product visit budget. The final product can still be explored when the page-navigation limit has been reached. Missing features are skipped. Product interactions do not modify the account's purchasing task configuration.

Pointer movement, clicks, dropdown keyboard input and targeted review scrolling are paced. Native dropdowns use mouse/keyboard input and commit once. Variant readiness observes selected-option/ASIN/URL state; expanders wait for visible detail content. Reading after an inspection varies from 3-8 seconds in addition to the saved page-reading range. All waits are bounded and cancellable. Navigation and AJAX retailer denials retain cooldown handling; no failed click is replayed. The existing 30-minute overall run limit remains.

Activity shows the current inspection; page history records completed interaction labels and reading durations. Up to four additional AI decisions per product increase token use; the routine dialog discloses those calls and the additional product information sent to the provider. Existing saved-product mode and purchasing behavior are unchanged.

Validation: `python -m pytest tests/test_account_care.py tests/test_care_agent.py tests/test_care_product.py -q`. The product fixtures cover immediate/delayed/AJAX/full-navigation changes, native dropdowns and disabled entries, both detail layouts, deep review scrolling, last-page interaction budgets, changed controls, missing transitions, cancellation and navigation/AJAX cooldowns. They assert no purchase requests and preserve other accounts and shared browser reservations. UI checks cover interaction history and routine explanations at desktop and 390px widths. `artifacts/account-care-review/product-exploration-measurement.json` records a controlled run with real browser input and simulated model decisions/reading pauses; it is not a live Amazon reliability or speed measurement.

## Inline variant timeout investigation

The October 11 Matte Black incident completed product details and review inspection, then timed out during a 22.09-second variant action. All five model decisions succeeded. The original trace did not distinguish click actionability from selected-state readiness, so it cannot prove which wait failed retrospectively.

Read-only inspection of the same product reproduced two layout mismatches: an inline radio input covers the visible label, and selection is marked on descendants rather than the swatch list item. Product exploration now chooses the visible enabled radio control and recognizes visible descendant selection markers, ignoring hidden fallback copies. It still clicks once. Diagnostic metadata records the click/selection/document/reading phase, control type, whether the click returned, and boolean selection evidence. These fields reset on a new run. Timeout messages identify the operation that stalled.

Validation: `python -m pytest tests/test_browsing_demo.py tests/test_care_product.py tests/test_account_care.py -q` passed 34 tests. The new inline-radio fixture proves the old label fails a non-mutating trial click because another element intercepts it, then verifies paced clicks and descendant-only selection without an ASIN/URL change. Existing delayed, missing, interrupted and retailer-denial transitions remain covered. These fixtures do not guarantee every live Amazon layout is supported.

## Local manual browsing demonstration

Run `python scripts/capture_browsing_demo.py --output artifacts/browsing-demo-<unique-name>` to open a visible isolated Amazon window. The panel connects before capture starts and provides Pause/Resume and Finish capture. It stops recording after 15 minutes; finishing leaves the window usable. No account cookies are copied from the application. Local JSONL contains only allowlisted timing, geometry, page categories and interaction categories. Search-key hold/flight times are captured without key values or text; passwords, sign-in/account/cart/checkout pages and challenge pages are excluded.

The initial recorder had an execution-world mismatch: Patchright bindings were unavailable to its main-world initialization script, resulting in zero saved events. The corrected recorder drains a bounded main-world buffer through its own CDP session. `status.json` reports connected pages and saved-event counts so a visible panel alone is not treated as proof of capture. Browser tests exercise real input, navigation, pause/resume, finish and excluded fields/pages. The panel was visually inspected at 1280px and 390px widths. Navigation can lose the final fraction of a second of buffered events; this is a short demonstration, not a complete interaction audit or a trained personal timing model.

The completed replacement capture informed optional browsing-only key/click calibration and varied reading scrolls. See [Browsing demonstration review](browsing-demo-review.md) for measurements, scope, configuration and verification. Calibration uses aggregate timings from encrypted settings, never the raw event file. Checkout and sign-in contexts do not load it.

## Pointer activity while reading

Both saved-product and AI browsing now move over visible headings, descriptions, product imagery and review content during page-reading pauses. Product inspection pauses use the same behavior while remaining at the inspected section. Each movement samples current visible geometry, skips covered elements and account/form/navigation controls, and uses the existing calibrated movement curve. Short pauses vary from 0.45–1.6 seconds within the existing reading budget. Pages without supported visible content keep their reading pause; this adds no clicks, model calls or extra navigation.

Browsing scrolls run a short calibrated pointer movement alongside the wheel burst. Both operations belong to one controller lock and are awaited or cancelled before it is released; failures and Stop leave no detached input task. Checkout and sign-in do not enable this browsing behavior. Model-decision/network waits are not reading pauses and do not launch ambient input.

Verification: 60 distinct tests passed across account care, product exploration, MCP browsing, calibration, pointer smoothing and the new reading fixtures. The dashboard fixture now explicitly holds its fake reading run until Stop; real reading DOM/input is verified separately. Desktop (1100px) and narrow (390px) fixtures were visually inspected. In one seeded 3.5-second local sample per layout, reading produced 66/52 mouse events; pointer movement during scrolling had median event gaps of 16.8/16.6 ms. Input cancellation measured 1.66/1.32 ms, excluding browser teardown. An already-dispatched browser event can arrive after cancellation; no additional commands are sent. These are controlled Chromium measurements, not live retailer or biometric accuracy claims.

Reproduce with `python -m pytest tests/test_account_care.py tests/test_care_product.py tests/test_care_agent.py tests/test_browsing_calibration.py tests/test_pointer_smoothing.py tests/test_pointer_motion_browser.py tests/test_reading_pointer.py -q`. Measurements and screenshots are in `artifacts/account-care-review/reading-pointer-*`.
