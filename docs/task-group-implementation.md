# Task Group architecture and implementation

The `task-group-redesign` branch implements shared purchasing objectives with independent account settings and execution. This report supersedes the earlier architecture proposal. Existing legacy tasks remain available; migration requires an explicit preview and starts in simulation.

## Evaluation and decision

The previous branch already separated immutable plans from runs and attempts, reused authenticated account browsers, and journaled purchase intent. Those are useful correctness boundaries and remain. Its main weaknesses were:

- Accounts were only a list of IDs: every purchasing option was group-wide.
- A whole-group `gather` barrier made the next monitoring cycle wait for the slowest account.
- Browser creation and closure happened while holding a global pool lock, serializing independent account launches.
- Each progress read decrypted every attempt in the quota's historical runs; the scheduler also decrypted inactive runs.
- Read failures were keyed globally by account, contaminating otherwise independent groups.
- A cancellation before the executor's first coroutine step could leave its reservation unreleased.
- Final checkout facts were re-read but discarded, allowing a changed total to be charged against stale accounting. Success headings without retailer order numbers received synthetic IDs.

A targeted restructuring is preferable to replacing the working ledger or adding another scheduler, message broker, database, or execution service. SQLite transactions coordinate reservations; asyncio overlaps browser I/O; account locks isolate sessions. Retailer adapters still own DOM verification and browser actions.

## Data ownership and configuration

`Plan` owns products, goals, schedules, defaults, assigned account IDs, and a sparse `account_settings` map keyed by those IDs. Each entry contains `enabled` and `overrides`. Missing or null options inherit; explicit false and zero remain overrides. Disabling preserves overrides. Validation rejects settings for unassigned accounts and product selections outside the group.

Supported overrides are action, quantity per order, account unit/order/spending limits, maximum order total, unit-price cap, selected product IDs, seller/condition policy, read retry count/delay, read timeout, and checkout/review timeout. ASINs represent purchasable variants. Shipping and payment use the retailer account's saved defaults: the adapter does not implement arbitrary application shipping/payment-profile selection, so no nonfunctional controls were added.

Credentials, sessions, proxies, browser identity and fingerprints remain account-owned. They are not copied into ordinary group configuration. Revisions and execution snapshots are encrypted. Overrides apply only within their group and never edit global accounts. Editing requires a stopped/completed group and an expected revision ID; a running attempt always uses its frozen plan.

The existing `account_ids` field is retained for compatibility with account deletion guards, migration and existing integrations. The settings map stores only assignment configuration, not copies of account data. Old plans acquire defaults when validated; their existing unit-count goals retain their meaning.

## Execution flow

1. Create/save a validated immutable revision. Readiness resolves effective account options on the backend.
2. Start idempotently creates one run and quota identity. Timed runs prepare sessions before their execution window.
3. The coordinator dispatches each enabled account independently, rotating through its selected products. Each account has its own next-read deadline, retry state and timeout. A per-group semaphore bounds reads; global browser capacity bounds contexts.
4. Account-scoped observations can be reused briefly across groups. Other accounts' observations are never substituted. Authentication and a fresh product check precede checkout.
5. One SQLite transaction reserves the account, quantity, order slot and conservative final-order allowance. The executor applies the account's resolved settings.
6. Cart and checkout review verify product, quantity, seller, condition, currency and final total. Durable submission intent precedes the single automatic submit.
7. The adapter revalidates the same financial facts and the current authorization immediately before clicking. Changed facts stop submission. Model calls are forbidden during that final recheck; previously validated price evidence may be reused deterministically.
8. A retailer order number and verified total produce a deduplicated confirmation. The ledger updates account/product/group counters in the same transaction, and the group completes when its goal is satisfied.

Read errors retry with bounded exponential delays; exhausted retries require rechecking. Ordinary failures are group/account-local. A retailer-requested backoff applies across the account's groups and cannot be cleared by rechecking another group. No access-control or purchase-limit bypass is implemented.

Browser launches run outside the pool lock. Account locks still prevent concurrent use of the same authenticated session. Idle contexts are reusable and evictable; uncertain attempts keep their account ownership. Recovery budgets and checkout evidence are reset for each independent attempt.

## Goals, lifecycle and uncertainty

- **First confirmed order:** multiple accounts monitor and prepare concurrently; only one checkout receives an order reservation. This intentionally serializes purchase authorization to prevent two external orders for a one-order objective.
- **Multiple confirmed orders:** independent checkouts run concurrently up to the configured concurrency, order, account and spending limits.
- **Unit target:** preserves prior behavior, including separate targets for each product. Order-count goals use Any matching product; conflicting Each-product semantics are rejected.

Group states remain scheduled, preparing, watching, paused, stopping, stopped and completed. Account execution controls are running, paused and stopped; saved assignment enablement is separate. Attempt stages retain reserved, preparing, carting, reviewing, waiting_user, submitting and terminal outcomes. The UI shows the active attempt or latest observation alongside account controls.

Group/account pause prevents subsequent mutations at gates. Already-dispatched browser input cannot be recalled; cancellation after a possible cart or order mutation is uncertain. Stop cancels reads and checkout tasks, including reservations whose coroutine never started. Checkout timeouts include manual-review and paused time, so reservations cannot silently hold an active executor forever.

Uncertain outcomes retain account ownership, units, money and order slots. They never trigger automatic submission replay. Restart pauses active runs and quarantines possible mutations. Activity retains older unresolved attempts and requires retailer-history/cart verification, notes, order ID, actual quantity and actual total to reconcile. User reconciliation is recorded evidence, not an independent retailer API verification. Actual totals are recorded truthfully even if they exceed configured limits.

Confirmed purchases persist across restarts and ordinary stop/start. Duplicate creates a new explicit objective. Per-window quotas remain available for recurring schedules. A browser success heading alone is insufficient to confirm an order.

## Persistence and frontend

The encrypted attempt ledger remains authoritative. Encrypted group/account/product counters replace historical scans on the reservation and progress paths. Counter deltas, attempt state, account claims and order deduplication commit together under `BEGIN IMMEDIATE`; rollback restores all of them. Plaintext indexes contain operational IDs and states, never credentials or payment data.

Schema version 2 builds counters and state indexes transactionally from existing encrypted records. It first writes `task-groups-before-v2.sqlite3` beside the database. Original revisions and attempts remain intact. Subsequent starts do not reapply counters. Existing legacy migration and its backup workflow remain supported.

The scheduler queries indexed active runs. Detail views read only recent attempts/runs plus unresolved outcomes. API summaries omit duplicate run plans and repeated full effective plans for each account. The Task Groups screen polls its compact endpoint rather than also fetching the entire application state every cycle. Configuration dialogs preserve drafts while monitoring updates continue.

The Accounts tab displays inherited/overridden settings, effective purchasing limits, enabled status, observations and active attempt state. Settings are available during creation and after stopping. Individual pause/resume/stop controls operate without altering other accounts.

## Measurements

Windows local measurements, encrypted temporary SQLite databases, 30 progress samples per history size:

| Historical attempts | Previous median | Redesigned median |
| --- | ---: | ---: |
| 10 | 0.261 ms | 0.0367 ms |
| 100 | 2.168 ms | 0.0329 ms |
| 1,000 | 19.297 ms | 0.0335 ms |

A controlled browser-pool benchmark compares the previous committed implementation (`fc64488`) with the redesign using identical 50 ms mocked launches and capacity 10:

| Accounts | Previous elapsed | Redesigned elapsed |
| --- | ---: | ---: |
| 10 | 613 ms | 63 ms |
| 50 | 3,114 ms | 310 ms |
| 100 | 6,228 ms | 619 ms |

Peak parallel launches changed from 1 to 10. These measurements demonstrate reduced local coordination overhead, not retailer acceptance or real-browser throughput. Run `python -m scripts.benchmark_task_groups`; machine-readable results are under `artifacts/task-group-build/`.

Remaining costs include browser startup/memory, retailer response time, synchronous SQLite writes, readiness checks, and the quarter-second scheduler tick. Browser and monitor limits intentionally bound concurrency. A single-process local service remains appropriate; no distributed scaling is claimed. Broad AI cost accounting and repair-recipe promotion remain separate adapter work, not solved by this Task Group change.

## Verification

The full regression run passed **260 tests, 4 skipped**. Subsequent targeted checks passed **78 affected tests**, including **41 Task Group tests**, including additional stress and adapter integration tests added after the full run. The optional skips are existing browser-backend dependencies. A Starlette/httpx deprecation warning remains.

Coverage includes inheritance, explicit zero/false, group/global isolation, disabled assignments, quantity overrides, invalid product selections, 16 competing reservation writers, concurrent starts, order-count goals, duplicate orders, account pause/resume, early cancellation, read/checkout timeouts, retry exhaustion, retailer backoff, restart uncertainty, transactional rollback, counter migration and API consistency. A 100-account simulation confirms 100 orders with at most 10 concurrent checkout executors and no leftover claims. An intercepted real-browser fixture verifies the new executor supplies evidence required by the modern Amazon checkout layout.

Playwright MCP exercised the actual interface in an isolated fixture app: create one group, select two accounts, override one account to two units, inherit one unit on the other, run to two confirmed simulated orders/three units, then disable an account while preserving its overrides. It reported no application JavaScript errors. `scripts/smoke_task_groups.js` contains the repeatable interaction sequence.

Live checkout remains Amazon US only; UK/Canada are simulation-only. No real purchase was placed. Fixture success does not guarantee compatibility with every retailer layout or account-specific checkout flow.
