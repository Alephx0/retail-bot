# Proxy, account lifecycle and recovery update

Target: `feature/deterministic-recovery`, port 8789. Implementation and verification used an isolated source checkout; application data and credentials were not copied into repair worktrees.

## Audit

The installed stack is FastAPI 0.141.1, vanilla JavaScript/native HTML, Patchright 1.63.0, HTTPX 0.28.1, MCP 1.30.0 and IMAPClient 3.0.1. There is no React frontend. Local Lucide icons and existing CSS tokens remain authoritative. The original group/task workflow, frozen task configuration, account leases, browser limits, single scheduler, submission journal and uncertain-order reconciliation remain in place.

Existing proxy code accepted colon-delimited HTTP connections only, eagerly created one coroutine per entry, treated HTTP 407 as reachable and overwrote health with the latest batch. Existing signup and sign-in handled individual forms but lacked shared existence/authorization states. Browser recovery already had semantic validation, bounded attempts, optional-dialog policies and offline repair tooling, but runtime AI used direct Python tools and repairs had no automatic incident queue.

The accessible `Refract Bypass/src/ui/views.mjs` and stylesheet were inspected for group cards, counts, previews, import/edit actions, spacing and responsive navigation. Its mock tests and execution code were not imported.

References inspected:

- [FOGLDN](https://www.fogldn.com/): proxy-test presentation and responsiveness; no source implementation was available there.
- [ProxyBeast source](https://github.com/Z3NTL3/ProxyBeast/blob/main/src-tauri/src/commands.rs): bounded workers, protocol negotiation, cancellation and progressive IPC. Its logging of complete proxy URIs was deliberately excluded. The current Rust/Tauri source differs from the older [Go-oriented documentation](https://proxy.z3ntl3.com/).
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp): pinned npm version 1.10.1, with actual installed tool schemas checked against a fixture browser.

## Connections

`proxy_protocol.py` parses HTTP, HTTPS, SOCKS4 and SOCKS5, colon credentials, percent-encoded URI credentials and bracketed IPv6. Canonical identities deduplicate equivalent entries and retain historic ordinary HTTP fingerprints. Existing encrypted records are preserved. Chromium cannot use authenticated SOCKS routes; those remain available for network tests and are excluded from new browser selection. SOCKS4 supports a user ID, not password authentication.

`proxy_probe.py` uses bounded asyncio streams and h11: TCP/DNS connection, proxy TLS, CONNECT/SOCKS negotiation, destination TLS, request-to-first-byte and total latency are separate observations. TLS certificate validation stays enabled outside local fixtures. A throughput check reads at most 1 MiB; this is a short sample, not a sustained-bandwidth guarantee. The default ipify diagnostic can report exit IP; geo lookup is not added. Destination 403/429 is distinct from proxy authentication or transport failure. No redirects, cookies or challenge solving occur. Retry-After blocks further tests to that destination for the requested duration; an in-flight request cannot be recalled.

`proxy_testing.py` uses a shared concurrency limit, at most three queued group jobs, bounded workers, immediate endpoint persistence, progressive batch results, cancellation, one transient-transport retry, a 20-observation history and 15-minute freshness. Healthy, Degraded, Unreachable, Authentication Failed and Untested remain independent of account authentication. Three recent transport failures are needed for Unreachable; a single failure produces Degraded. Existing session bindings retain their route. Fresh observed health governs selection after measured history exists; legacy untested pools retain their previous initial-selection behavior.

Optional idle health refresh uses the existing scheduler, checks at most five stale entries, and stops when account/task work appears. It is disabled by default. No health test runs just because the UI refreshes. Settings expose concurrency, timeout and idle refresh under advanced network limits.

The proxy workspace has selectable group cards, assignment counts, import/replacement/removal, scoped selection, sorting, measurements, test cancellation and advanced diagnostic options. Credentials never appear in its read model. Replacing a connection requires the full replacement; credentials are not revealed to the frontend. Active account-inherited proxy assignments now also block edits.

## Account lifecycle

`account_lifecycle.py` records categorical state/evidence, never alert prose or credentials. Explicit account-existence errors and actual sign-in forms can hand an authorized signup to the same browser's sign-in routine once. A reliable unregistered-account response can enter signup only in a context already authorized for account creation. Ordinary sign-in reports the required action instead of creating an account silently. A saved logged-in flag or a Create Account button is not existence proof. Successful state is recorded alongside existing live session verification. Verification, invalid passwords, recovery pages, foreign origins and retailer cooldowns retain stops rather than blind retries.

## Recovery and repair

`RecoveryRequest` provides reusable action, origin, policy, failure and mutation-boundary contracts. Deterministic actions do not invoke AI. Exception recovery has two model workers, bounded steps/deadlines and one attempt per checkpoint. Protected verification and uncertain/submitted mutations cannot enter generic recovery.

An account browser with AI recovery enabled exposes an isolated loopback debugging endpoint. `ScopedMCP` attaches the pinned local server, selects the unique owned page and disables telemetry, CrUX and arbitrary JavaScript tools. Raw snapshots stay local. Only policy-matched control labels and references reach the model; local uniqueness, origin, ownership and actionability checks remain mandatory. Explicit generic reversible workflows can dispatch through MCP and verify a trusted expected-outcome callback. Amazon purchase adapters retain their existing mutation guards and dispatch; AI cannot submit orders. Live dashboard input and optional-dialog handlers yield while MCP owns the page.

Verified successful recovery enters a deduplicated encrypted repair queue. The existing scheduler launches a separate worker only while idle, at reduced priority. The worker snapshots current source into a distinct worktree, uses the configured model to read the permitted component and propose a targeted change plus a regression, then checks:

1. Only one non-submitting action regex changes; functions, roles, fallback selectors and transaction guards remain unchanged.
2. New regression source passes capability restrictions and fails on the original component.
3. The repaired regression and checkout/state/transaction integration suite pass.
4. The existing alternating local benchmark passes its 5% p95 threshold.

Validated output is a content-hashed package marked **ready for review**. Automatic source generation never hot-patches the running app. `scripts/promote_repair.py` checks the application's exclusive workspace lock, destination source and package hashes before activation; `--rollback` restores the original component. Review is required by repository deployment policy. Failed/interrupted workers become Needs developer review; no endless repair loop is started. Broader workflow/state repairs remain developer work rather than unrestricted automatic code changes.

Commands:

```powershell
python scripts/benchmark_proxy_testing.py
python scripts/demo_self_healing.py --data-dir <configured-data-dir> --checkout <new-isolated-checkout>
python scripts/repair_worker.py --data-dir <configured-data-dir> --incident <queued-repair-id>
python scripts/promote_repair.py --package <validated-package.json> --repo <stopped-source> --data-dir <stopped-data-dir>
# Same promotion command with --rollback restores the original component.
```

Install Python requirements and run `npm ci --ignore-scripts` before using MCP recovery. External/shared CDP sessions without an owned endpoint require attention; they are not silently attached to an unrelated browser.

## Measured verification

Initial audit baseline: 63 tests passed. Proxy protocol fixtures exercise real local HTTP/SOCKS4/SOCKS5 exchanges, HTTP and nested HTTPS CONNECT/TLS, authentication failure, destination 403/429, batch failures, cancellation, cached results and stable bindings. A real Patchright UI test covers import/deduplication/filtering/selection and desktop/narrow layouts; screenshots were inspected.

The 100-entry synthetic 20 ms probe benchmark includes encrypted SQLite persistence: 3,139.95 ms at concurrency 1; 637.53 ms at 5; 337.90 ms at 10. Maximum 5 ms heartbeat intervals were 21.15–22.39 ms on Windows. Python peak traced allocation was under 650 KB. These measurements exclude live DNS/proxy/provider variation and browser/Node memory.

The real configured `gpt-6-luna` acceptance run used an intercepted storefront, with no retailer/account/payment requests. “Add this product to cart” failed the original deterministic rule in 47.93 ms. The AI recovered through Chrome DevTools MCP in 5,421.40 ms/two model steps, retaining the context. A separate model-authored regex repair and browser regression failed before/passed after; 49 integration checks passed. The repair's 100-sample p95 changed from 151.293 to 151.737 ms (+0.29%). Deterministic rerun completed in 70.23 ms with zero repeat AI calls. An earlier proposal was rejected for its test filename, and the first isolated validation exposed missing npm dependency setup; neither was promoted. These were corrected before the successful gate.

Artifacts: `artifacts/overhaul/proxy-performance.json`, `self-healing-demo.json`, desktop/narrow screenshots and the separate repair checkout's `artifacts/source-repair` report/package. A broader intermediate run passed 105 tests. Final suites: 158 account/sign-in/IMAP checks passed with one optional native-Chromium skip; 76 transaction/recovery/task checks passed; 46 focused UI/core checks passed; all 25 proxy checks and all 6 real-MCP ownership/cancellation checks passed. Suites overlap and are not summed. The only warning is the existing Starlette HTTPX TestClient deprecation.

Limitations: the demo proves one semantic-control repair, not arbitrary website adaptation. Unfamiliar action semantics, missing item/price evidence, authentication challenges and irreversible uncertainty still stop. No real purchase, live Amazon signup, paid CAPTCHA solve, or sustained live-proxy load was used. Overall production recovery percentages, per-account failure rates and full browser CPU/RAM per concurrent task require longer operational observations; action counts, incident histories, timing and validated repair status are exposed without claiming those unmeasured rates.

Final 100-sample comparison against `82cf59a`: baseline p50/p95 134.967/151.075 ms; updated 134.951/150.846 ms (p95 -0.15%). The original harness gave only the updated side optional-dialog handlers and reused the current resolver on the baseline. The corrected harness installs supported handlers on both and loads the baseline resolver from its revision. The failed unequal comparison remains in `final-fast-path.json`; the corrected result is `final-fast-path-equivalent.json`. Ten exception-validation samples had a noisy 82.992 ms tail; that small sample is not claimed as an improvement.
