# Account session verification

Settings → Browser & fingerprints → Startup verification selects a global mode.
Existing workspaces without an explicit choice inherit **Browser-free check**.

| Mode | Startup behavior |
| --- | --- |
| Browser-free check (default) | Bounded, read-only HTTPS request using saved account cookies on the task's selected proxy connection. A protected orders page must positively identify an authenticated session. Missing cookies, a sign-in redirect, an inconclusive page or a network timeout falls back to the existing task browser and sign-in flow. |
| Headless browser | Runs the existing browser verification/sign-in flow without a desktop window. Uses the same account seed, fingerprint implementation, overrides, extensions, saved storage and proxy selection. View live remains available for user intervention. |
| Task browser | Preserves the previous startup browser flow and follows the existing Window mode setting. |

This setting controls preliminary verification, not checkout window mode. Tasks always verify the live account browser and its current product offer before carting. A successful HTTP probe never changes the stored logged-in flag or authorizes a purchase. Anonymous monitoring remains separate. Scheduled compatibility plans use the same choice during preparation; actual account inventory reads and checkout still use their browser.

Headless preparation can retain its browser when execution also uses headless mode. Otherwise the original task runner closes preparation before the normal execution browser opens. Pooled scheduled sessions replace the preparation context when the execution window mode differs, draining the old process before reusing its persistent profile. Account fingerprint settings are not modified. Headless Chromium can still expose different browser-intrinsic surfaces; preserving configuration is not a promise of identical detection scores. Externally attached browsers cannot be switched to headless by the app, so that combination is rejected.

HTTP probes have a five-second overall network deadline, at most four read-only requests and a one-MiB decoded response limit. Cookies are restricted to the account's Amazon region; redirects cannot leave that HTTPS origin or reach arbitrary action paths. Responses and cookies are never logged. Probe concurrency is bounded by the worker setting without occupying browser slots. HTTP 429/503 retain the full retailer cooldown; access denial pauses for review instead of switching transports. Cancellation releases account ownership.

Scheduled preparation checks at most one account session per minute rather than one request per product/monitor interval. Opening the execution window immediately enables stock inspection, including when a preparation check was still in flight. The existing one-hour `Retry-After` truncation was removed so longer retailer cooldowns are respected.

Validation uses controlled HTTP transports, simulated task execution and local browser pages, never real orders. Tests cover each mode, sign-in fallback, redirects, unusable cookies, cooldowns, cancellation, scheduled preparation, fresh verification before carting, settings persistence and runtime locks. Real JavaScript Chromium checks cover headless CPU/memory, screen, timezone and worker consistency plus a subsequent visible launch. Native launch options are checked for fingerprint parity; the optional native binary test skips when the binary is absent. Settings are exercised through Playwright at desktop and 390px widths. Live Amazon acceptance and latency remain dependent on its responses and the account connection; no live speed claim is made.
