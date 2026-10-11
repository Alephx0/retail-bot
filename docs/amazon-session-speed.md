# Session verification and product handoff

The running instance recorded one task with 14,174.73 ms in session verification,
6,291.04 ms in account offer verification, and 18,860.09 ms in total account
preparation. These are nested stage measurements from one live observation,
not independent durations to sum or a production latency distribution.

The exact-URL comparisons in `ensure_session` and `inspect` caused redundant
product navigations when Amazon decorated the URL with a query or a canonical
product-name path. The first comparison reloaded the start URL; the second
discarded the initial product-page handoff and loaded it again. This reproduces
the two-extra-load symptom in a controlled fixture; the existing timing data
does not itself record navigation URLs or prove every live reload had this cause.

Changes:

- Recognize the same product by HTTPS origin and ASIN in supported Amazon product
  paths. Reject another ASIN, country, host, credentials, or a sign-in return URL.
- Transfer the freshly verified product page to offer inspection once. Inspect
  still reads current price, seller and purchase eligibility from that document;
  it does not reuse a cached inventory result. Subsequent scans navigate normally.
- Wait for the visible account header or an authentication interruption using
  browser state, bounded by the existing timeout. Do not reload because the
  header is still rendering. Missing/ambiguous headers cannot verify a session.
- Skip the credential-form scan when the live header is already signed in.
  Reuse the product document reached by successful resumed authentication.
- Read access, CAPTCHA, password and MFA indicators in one browser snapshot.
  Re-observe after OTP submission; retain all challenge and consent behavior.
- Saved compatibility plans now verify their session on their target product,
  avoiding a separate Your Orders visit. Account leases and journals are unchanged.

One fresh navigation is still required when verifying a new session. A saved
`logged_in` flag or an old open page is not accepted as proof of current access.
Encrypted session persistence and browser-health checks remain in place.

## Measurement

`python scripts/benchmark_session_handoff.py --samples 25 --latency-ms 100`
alternates commit `d0d4660` and the revised implementation in a warmed browser.
Both versions run session verification, encrypted storage capture, browser-health
checks, and a fresh offer read. Browser creation is outside the timed interval.
All requests are served by controlled fixtures; URL decoration uses local
`history.replaceState`. No credentials or purchases are sent to a retailer.

The fixture adds 100 ms to each navigation to model network cost. Results are
local comparisons, not a promise that live Amazon sign-in will complete in the
same time. Actual network, extensions, browser startup, MFA and session-storage
size remain sources of latency. Raw results are in `artifacts/session-speed/`.

Final 25-sample comparison (three warmups, milliseconds):

| URL shape | Baseline median | Revised median | Baseline p95 | Revised p95 | Navigations |
| --- | ---: | ---: | ---: | ---: | --- |
| Exact product URL | 278.75 | 240.31 | 300.31 | 263.90 | 1 → 1 |
| Canonical path + query | 577.07 | 243.89 | 650.85 | 287.45 | 3 → 1 |

The reproduced extra-navigation case improved median duration by 57.7% and p95
by 55.8%. An already exact URL improved p95 by 12.1%. This fixture uses a small
empty session; it does not estimate the cost of capturing a large real profile.

## Validation

117 tests passed in 113.02 seconds, covering session login, canonical handoff,
delayed/hidden/missing headers, authentication interruptions, current offer reads,
single-use handoff, account task execution, monitors, browser recovery, cart and
uncertain checkout outcomes. Python compilation and `git diff --check` passed.
The existing Starlette/httpx deprecation warning remains.
