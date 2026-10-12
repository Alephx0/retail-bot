# Offer inspection and optional notifications

Offer inspection now reads product fields, seller evidence and Add to Cart
availability in one browser snapshot. Playwright still supplies semantic control
matching; duplicate visible controls are rejected, hidden/disabled copies are
excluded, and conflicting seller evidence remains unknown. The same seller parser
serves product and checkout reads. No stock, session or price cache authorizes a
purchase. Mutation-time control resolution and final checkout verification remain.

Explicit stockouts and delivery/sign-in restrictions no longer invoke purchase
control recovery. Unexpected controls on otherwise eligible offers retain recovery.
Anonymous monitors never invoke purchase recovery.

Optional webhooks use one background worker with a 32-message queue, a reused HTTP
client and a 10-second delivery deadline. Slow/failed webhooks cannot delay offer
selection or checkout. Overflow records a delivery error instead of blocking.
Delivery is best-effort: shutdown cancels delivery and discards pending notices.
Submission journals and checkout records remain durable and synchronous.

## Controlled measurements

Run `python scripts/benchmark_offer.py --samples 25 --trace --output artifacts/offer-speed/trace.json`
and repeat without `--trace` using a different output path. The benchmark alternates
commit `82cf59a` and the current offer reader in one warmed browser, with three
warmups and 25 measured samples. It checks that product eligibility/evidence results
match on each fixture. Requests are intercepted; recovery is a deterministic failing
stub rather than a model call.

| Tracing | Fixture | Median before → after | p95 before → after |
| --- | --- | --- | --- |
| On | Normal offer | 61.20 → 6.87 ms | 72.64 → 8.92 ms |
| On | Legacy seller layout | 57.56 → 6.98 ms | 63.56 → 9.59 ms |
| On | Out of stock | 67.32 → 21.06 ms | 80.58 → 24.82 ms |
| Off | Normal offer | 38.68 → 5.34 ms | 45.71 → 6.73 ms |

These measurements isolate local offer-reading overhead. They exclude navigation,
browser startup, authentication and checkout; they do not predict live Amazon
latency. Production tracing settings were not changed. Browser tests verify fresh
price changes, seller ambiguity, fulfillment-versus-seller evidence, disabled and
duplicate controls, restrictions and lost sessions. A blocked webhook fixture
verifies that a real task runner reaches mocked checkout review before delivery
finishes; queue overflow, delivery failure and shutdown are covered separately.
Playwright MCP also exercised the production snapshot against a changing price and
disabled Add to Cart control. No real cart or order was changed.

Validation: 128 distinct tests passed across offer/notification, seller, monitor,
core, browser, checkout, session-handoff, performance, cart and state-transition
suites. The three offer/notification tests passed again after adding recovered
control uniqueness coverage. The only warning was the existing Starlette/httpx
deprecation. `git diff --check` passed.
