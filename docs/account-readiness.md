# Restock readiness and checkout speed

The task runner can now retain a verified account browser while its shared monitor
waits for stock. **Settings → Browser & fingerprints → Keep accounts ready for
restocks** is enabled by default. Disabling it restores resource-saving standby
and the existing browser-free, headless or normal startup verification choice.
Explicit tracing preferences and Buy Now selections remain unchanged.

## Execution and limits

- A task owns its ready browser and account lock. Readiness uses the existing
  browser semaphore, with at most four reservations (one fewer than the configured
  worker count, except a one-worker installation allows one). This consumes more
  memory while waiting in exchange for avoiding a launch at restock.
- An idle browser yields to tasks needing its account or worker capacity, including
  compatibility-plan workers. Purchasing browsers cannot be evicted. An evicted
  task stays subscribed to stock and opens a fresh verified browser when needed;
  it does not repeatedly reopen browsers during a stockout.
- Prepared browsers skip the separate HTTP probe. Resource-saving tasks also skip
  that probe if an available stock observation is already present and recent.
  Anonymous stock remains only a signal: current account stock, session, seller,
  price and restrictions are checked before carting. No cached offer authorizes an
  order. Closed browsers and changed account/browser configuration invalidate
  readiness. Expired sign-in follows the existing authentication flow.
- Headless preparation followed by visible checkout keeps its existing separate
  browser lifecycle. It cannot reuse the headless instance as a visible window.
- The single scheduler prepares up to one minute before a scheduled start, with
  an explicit activation gate before carting, including when stock is already
  available. Unused account locks and worker slots are released during that wait.
  Stop cancels preparation. Occurrence records still prevent replay after restart;
  interrupted runs require user review rather than automatic purchase replay.
- Monitor admission acquires a group's concurrency slot before a global scan
  slot. A busy group's waiting scans no longer consume slots needed by other
  groups. Retailer polling intervals and backoff are unchanged.
- Account `last_used` is written at task start rather than at every status update.
  The scheduler avoids rewriting unchanged occurrence records each poll. Durable
  order intents, quotas, account isolation and uncertain-outcome handling remain.

The task row shows **Ready** while its browser is retained. Closing it or yielding
capacity removes that indication. A restart clears runtime readiness.

## Controlled measurements

`python scripts/benchmark_readiness.py --samples 10 --cost-ms 100`

Ten paired samples inject 100 ms each of browser setup and session verification
into a mocked retailer. Both modes still perform the account offer inspection.

| Restock signal to cart entry | Resource-saving | Ready browser |
| --- | ---: | ---: |
| Median | 233.87 ms | 8.70 ms |
| p95 | 245.18 ms | 14.74 ms |
| Full session preparations after signal | 1 | 0 |

This measures moving preparation out of the purchase path, not eliminating live
session checks. It excludes real retailer latency, browser startup costs and
checkout. Actual network responses, login challenges and worker contention remain
variable. Raw results: `artifacts/readiness/benchmark.json`.

`python scripts/benchmark_checkout_routes.py --samples 10 --network-ms 100`

This exercises the actual Amazon cart/Buy Now adapters through intercepted pages,
stopping at verified review. It verifies product, quantity, seller and total and
asserts that Place Order was never clicked. Buy Now uses one document request;
cart uses two on this fixture. Median time to verified review was 329.46 ms for
Buy Now versus 511.08 ms for cart (p95: 355.63 vs 556.95 ms). These include the
injected 100 ms delay per document, not live Amazon traffic. The benchmark excludes
input pacing and offer/session work.
Buy Now remains opt-in because actual offers and checkout paths vary. Raw timing
results: `artifacts/readiness/checkout-routes.json`.

Full tracing remains opt-in; existing saved choices are preserved. See
[offer-speed.md](offer-speed.md) for measured offer-reading and tracing overhead
and the bounded background notification worker.

## Verification

Readiness fixtures cover browser reuse, capacity eviction, same-account isolation,
configuration invalidation, closed browsers, expired authentication, scheduled
activation and gaps, occurrence deduplication, immediate stop, cleanup, bounded
reservations, monitor fairness and the settings API guard. Existing monitor,
session, checkout, task-group and workspace suites cover compatibility behavior.

Playwright MCP verified setting persistence, keyboard focus and activation, and
desktop/narrow layouts on an isolated empty app. Screenshots are stored under
`artifacts/readiness/`. No live carts or orders were changed. Production must be
restarted to load the new Python code.

Latest regression runs: readiness/monitors/workspace/settings/checkout navigation,
64 passed; after the final cancellation changes, readiness/core, 32 passed. Earlier
session verification/task-group execution/workspace overhaul/cart repair/session
speed coverage passed 94 tests with one optional native-binary test skipped. These
run counts overlap. JavaScript syntax checks, Python compilation and
`git diff --check` passed. The existing Starlette/httpx deprecation warning remains.
