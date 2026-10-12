# Signup startup: expired proxy evidence

The affected signup failed before an account window existed. `ProxyPool.choose`
rejected a previously healthy endpoint whose 15-minute health evidence had
expired. `create_account` replaced that exception with a generic browser error,
and the closed-browser snapshot then replaced the failure with "Ready to open
signup."

Signup now refreshes expired healthy proxy evidence on demand, using the
existing proxy tester, neutral diagnostic destination, transport timeouts,
concurrency limits, and destination cooldowns. It checks at most five candidates
once, joins existing group checks, and preserves established account routes.
The existing 90-second startup deadline includes this work. Fresh selections
and existing routes require no additional network check. Missing assigned groups
fail explicitly instead of silently using a direct connection.

Startup failures retain a safe reason and startup stage in both the API and
account-creation view. Browser exception text and connection credentials are not
exported. Retrying clears the old error; resume retains submission markers.

Validation uses controlled proxy observations and retailer fixtures, including
failed checks, cancellation, concurrent callers, missing/incompatible routes,
capacity release, account independence, and prior submission preservation.
Playwright verifies the persistent error and retry button at 1440px and 390px.
The real assigned proxy was rechecked successfully; its account browser then
opened a blank page with an owned CDP endpoint. No signup was submitted by that
live launch check. Total measured live setup and cleanup: 11.36 seconds, one
sample including variable network and browser initialization.

Performance gate: 200 alternating warmed selections of an existing route in an
encrypted local store, after 20 warmups. Baseline p95 0.1417ms; patched p95
0.1413ms. This isolates route selection and does not measure live signup speed.
Evidence is under `artifacts/signup-startup/`.

Regression results: 49 tests passed in the startup/proxy/account-creation run;
32 passed in the startup/resume/CDP takeover run (overlapping startup coverage).
The final focused startup run passed all 12 cases after the wording change.
