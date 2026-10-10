# Amazon action readiness

Amazon action delays now wait for observable states instead of sleeping for a
presumed page-update duration. `retail/amazon_states.py` holds read-only predicates;
`Amazon.wait_state` uses browser condition waits and records their duration. The
existing browser timeout bounds a stalled transition; readiness returns immediately.

| Operation | Required state |
| --- | --- |
| Add to cart | New cart acknowledgement/count increase or matching cart row, followed by a separate cart navigation verifying the exact product and quantity |
| Change cart quantity | Requested quantity in the active cart, followed by existing cart validation |
| Checkout navigation | A supported modal, continuation or final review; after clicking, a changed checkout state is required before another action |
| Submit order | Confirmation containing success evidence and an order ID, payment verification, or an explicit interruption |
| Confirm order | Success evidence and an actual order ID together; a heading alone or an ID alone is insufficient |
| Free shipping | Visible shipping charges verified as zero/free after selecting the option |
| Card verification | CVV field gone and confirmation, verification success, checkout review or a subsequent payment step visible |
| Manual login | Signed-in header on an allowed account page, then session verification and encrypted persistence |
| Login browser closes | Browser close event, with listener removed on completion/cancellation |

Removed Amazon action sleeps of 500ms, 1500ms, 1800ms, 700ms and 800ms, plus
250ms checkout/confirmation polling loops. Manual login readiness no longer polls
every second; task startup no longer polls an open login browser every 500ms.
Retailer polling intervals, Retry-After/backoff, simulation delays and opt-in input
pacing are separate policies, not page-readiness assumptions. Bounds on navigation
steps, review freshness and session lifetimes remain correctness safeguards.

Unchanged pages do not trigger repeated clicks. A missing submission outcome remains
uncertain; the durable submission journal continues to block a second order. Page
challenges, authentication and errors stop automatic progress. Unknown page layouts
can reach the timeout and require review rather than proceeding speculatively.

## Controlled measurements

`python scripts/benchmark_amazon_states.py --samples 5 --output artifacts/amazon-state-waits.json`
compares commit `1f637b4` against the current adapter using local browser fixtures.
All requests are intercepted. No retailer/provider calls or real orders occur.
Five samples, median milliseconds:

| Complete fixture action | Fixed waits | State waits |
| --- | ---: | ---: |
| Cart + verification | 1773.74 | 287.72 |
| Submit + confirmation | 1950.05 | 139.05 |

These fast-response fixtures isolate unnecessary wait overhead; they are not live
Amazon timings. Slower fixtures separately test cart acknowledgement after 1700ms,
quantity updates after 650ms, shipping updates after 900ms, CVV completion after
1000ms and an order ID arriving 2000ms after its success heading. Tests also cover
missing outcomes, unchanged controls, cancellation, challenge interruption, unknown
confirmation, journal-before-submit and no duplicate submissions.

Older checkout fixtures now provide actual cart acknowledgements/confirmation DOM
states instead of relying on a sleep to move past a non-updating page. The integrated
AI fixture expects recovery only in the account action path, matching inventory-only
monitor behavior introduced in the previous change.

Validation: the checkout/navigation/AI/workspace/login/monitor/execution/pacing
regression selection passed 96 tests. A follow-up state/monitor/performance/core
selection passed 51 tests, including login-close event cleanup; login/account UI
tests passed 10 tests. The final transition fixtures additionally verify login
recovery on a changed page and the existing bounded Continue shopping behavior.
No production purchase was initiated during this work.
