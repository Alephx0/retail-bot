# Amazon add-first cart flow

The old path opened the cart, checked/saved existing items, reloaded the product,
added the target, then opened the cart again. This imposed three document
navigations after account offer verification, even for an empty cart.

The revised path adds once on the verified product page, waits for observable
acknowledgement, and opens the cart once. If Amazon redirects directly to the
cart, that document is reused. Product-page recommendations are not accepted as
add acknowledgement. Target identity must be known before any cleanup.

Unrelated active-cart items are deleted through their uniquely scoped native
controls, as requested. Each deletion waits for its committed page state. Saved
for Later items are excluded. A pre-existing target can cause Amazon to increase
its quantity; the native cart quantity control normalizes it to the requested
quantity. Missing or ambiguous controls stop the task. Final cart and checkout
product, quantity, seller, condition, budget and order checks remain authoritative.

This deliberately changes existing-target behavior: adding first can take longer
than the old preflight when the target was already present. It prioritizes the
time to add a newly available product; it is not a speedup for every cart state.

Cart rows now use one read-only browser snapshot instead of several calls per
row. Product quantity options/current value are also read together, and an
already selected quantity does not trigger another selection action. No forced
clicks, parallel cart mutations, fixed settle waits, reduced rate-limit backoff,
or skipped account offer checks were introduced.

New nested timing stages expose add acknowledgement, opening the cart,
reconciliation and deletion in the existing timing view. Nested durations must
not be summed as independent work. Browser/network waits and optional interaction
pacing remain distinct from processing overhead.

Cart uncertainty remains distinct from a pre-mutation rejection. Once Add or
Delete may have executed, a missing acknowledgement pauses the task; it does not
repeat the mutation or clear the runner's mutation flag. Purchase submission
journals, account locks and reconciliation are unchanged.

## Reproducing the measurements

Run `python scripts/benchmark_amazon_cart.py --samples 25 --latency-ms 100` and
repeat with `--latency-ms 0` using a separate `--output` path. The script alternates
the cart implementation at commit `f479afc` and the current implementation in the
same warmed headless browser, with three warmups and 25 samples per scenario.
All requests are intercepted by `scripts/cart_fixture.py`; no retailer or model
calls occur. The initial verified product navigation is outside the timed region.
Browser startup, login, monitoring and optional-popup handler installation are
excluded: these measurements isolate the cart adapter. Final runs were performed
with the branch application's task stopped, without concurrent regression tests.

The artificial document latency models navigation cost, not an Amazon latency
prediction. The zero-added-latency run still includes local browser rendering,
protocol and fixture request handling. Machine load can affect either run. These
sample sizes support local comparisons, not a production p99 or an absolute
minimum achievable checkout time. The existing-target comparison includes an
extra add and quantity correction only in the new implementation. Mixed carts
used Save for Later in the baseline and Delete in the revised flow.

The practical stopping point is one add, at most one cart navigation, and the
minimum confirmed cleanup/quantity actions. Removing those remaining state and
transaction checks would trade correctness for speed.

## Final measurements

Milliseconds, 25 measured samples per cell; baseline → revised implementation:

| Added document latency | Initial cart | Median | p95 |
| --- | --- | --- | --- |
| 0 ms | Empty | 320.66 → 147.06 | 358.90 → 187.66 |
| 0 ms | Unrelated item | 463.66 → 254.69 | 542.79 → 293.41 |
| 0 ms | Target already present | 72.55 → 198.16 | 89.15 → 243.72 |
| 100 ms | Empty | 637.19 → 258.84 | 674.43 → 279.95 |
| 100 ms | Unrelated item | 770.59 → 350.10 | 819.18 → 380.85 |
| 100 ms | Target already present | 182.90 → 308.33 | 192.26 → 350.20 |

At 100 ms document latency, time to the fixture server receiving Add fell from
395.68 to 53.99 ms median for empty carts, and 523.21 to 52.80 ms for mixed carts.
Cart preparation p95 improved 58.5% and 53.5%, respectively. Navigation counts
fell from three to one. Existing-target p95 regressed by 157.94 ms in that run:
the add-first policy requires an add and a quantity correction there, where the
old flow only verified the existing item. No universal speedup is claimed.

Raw observations are retained locally in `artifacts/cart-speed/final-0ms.json`
and `final-100ms.json`. Earlier pilot runs ran alongside other desktop activity;
their differences must not be attributed solely to the quantity-read refinement.

## Validation

The expanded regression suite passed **201 tests in 183.88 seconds**, with one
existing Starlette/httpx deprecation warning. After the final recommendation and
missing-target guards, the focused cart, checkout, navigation and state-transition
suites passed **53 tests in 78.09 seconds**. Python compilation and diff whitespace
checks passed. All purchasing tests use controlled fixtures.

Coverage includes empty/mixed/existing-target carts, one-add ordering, direct cart
redirects, delayed acknowledgement and row hydration, exact quantity normalization,
unsupported and ambiguous quantity controls, ambiguous deletion, duplicate target
rows, lost add/delete acknowledgement, misleading product recommendations,
cancellation, independent account progress, and a target removed before checkout.
Existing uncertain-order and task-group lifecycle tests remain in the expanded run.
