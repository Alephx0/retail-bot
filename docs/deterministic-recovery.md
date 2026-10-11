# Deterministic browser recovery

## Baseline and architecture

Checkpoint `d0800a9` on main preserves the stable application. Development lives on
`feature/deterministic-recovery` in a separate checkout. The initial selected
baseline suite passed **110 tests in 109.61 s**, with an existing Starlette/httpx
warning. The running stable workspace is not hot-reloaded from this branch.

The original Group ? Tasks model, single scheduler, account leases, browser quotas,
frozen run settings, monitor handoff, durable submission journal and reconciliation
remain authoritative. Anonymous stock inspection wakes a task; the signed-in account
still verifies purchase eligibility. Recovery never places orders itself.

The audit found four avoidable costs/risks: legacy agent mode bypassed deterministic
locators; live recovery opened a synthetic extra browser context for every proposed
control; failure capture awaited a screenshot and full accessibility snapshot; and
model-selected recipes were immediately reusable without review. Product inspection
also had a separate model path outside action recovery.

## Chosen implementation

`Amazon.resolve_action` first calls the existing semantic resolver in every mode.
Success requires no recovery pool, evidence capture, model request or MCP transport.
A single native locator handler per task page handles specifically recognized optional
dialogs before the pending action is dispatched. Its browser-side visibility test has
measured overhead, reported below. Normal account/session construction is unchanged.

On a missing control, `RecoveryController` uses the same owned page/context. It can
close an unambiguous optional newsletter/promotion/announcement dialog, then search
allowlisted role names in the page, shadow roots and at most four same-retailer frames.
A candidate must still be unique, live, enabled, on-domain and actionable. Changed
ownership, stale references, conflicting destinations, access verification and unknown
choices stop recovery. New tabs are never adopted implicitly. Disconnection never
recreates a checkout session or retries an uncertain submission.

Two recovery workers bound structural/model work. The queue waits at most 150 ms;
healthy actions bypass it. A page action gets one recovery attempt, with live
revalidation of a previously recovered handle. Internal stall caps are 3 s for
structural work and 15 s total, shortened by existing browser/provider settings and
inherited execution deadlines. These are maximum budgets, not settle sleeps. They
leave headroom above local fixture measurements; production provider/network latency
has not been benchmarked. Three failed recoveries pause AI for that site/route/action
for 30 s. Circuit entries, metrics, incidents and candidate history are bounded.

AI receives only allowlisted observed control names and route categories. It can ask
for bounded evidence and validate a reference through direct Python methods. It cannot
execute JavaScript, change account configuration or broaden an action policy. Unknown
semantics still require a source change or user attention; a model cannot authorize
new purchase behavior. Known renamed final totals are checked deterministically;
unknown or conflicting totals stop checkout, especially after submission intent.

Optional dialogs must have a recognized heading and exactly one safe dismissal.
Cookie/terms, security, region/store, shipping and payment dialogs are not auto-accepted.
There is no stored consent preference in this application to infer. Unknown overlays,
unowned tabs, unsupported frames and disconnected browsers require attention.

Recovery activity is a read model, separate from task lifecycle state. The task table
shows Recovering while active; existing task states resume afterward. Operations shows
Resumed/Needs attention/Stopped and durations. Step timings calculate p50/p95/p99 on
demand from the existing bounded sample buffer. No new configuration panel was added.

## Evidence and compatibility

Normal recovery stores no screenshots, full AX trees, raw exception bodies, URL query
strings, customer prose or credentials. Explicit user traces remain encrypted and are
excluded from source-repair exports. Old diagnostic data is sanitized again before
external diagnosis. Incidents deduplicate by schema/site/route/action/category, with
counts and handler version. This is intentionally a coarse failure signature; a
reproducible fixture may still be needed for a specific source repair.

Model-generated recipes have `status=candidate`. Historical recipes are retained but
are not implicitly approved. Explicit approval requires schema version, review commit
and test evidence, and every reuse still validates the live control. The main route
uses tested structural handlers rather than assuming model success proves a permanent
repair. No account/group migration or data deletion is required. Persisted `agent`
mode is accepted but now means exception-only assistance; the UI offers Off/When normal
navigation fails.

## Technology decision (checked 2026-10-10)

- Keep installed [Patchright 1.63.0](https://pypi.org/project/patchright/1.63.0/), also
  the release returned by PyPI during this audit. It already owns Chromium sessions
  and supplies the needed locator/CDP interfaces. No runtime dependency was added.
- [mcp-patchright](https://github.com/maestrojeong/mcp-patchright) 0.1.11 offers CDP
  attachment and targeted tools. It is useful for development, but a second controller
  adds ownership and transport concerns with no demonstrated runtime benefit here.
- [Browser Harness](https://github.com/browser-use/browser-harness) offers editable
  browser helpers. Its offline reuse idea informs the repair workflow; generated
  browser code is not injected into active retail sessions.
- [Playwright MCP](https://github.com/microsoft/playwright-mcp) 0.0.83 is retained for
  development UI inspection. The existing MCP-compatible evidence tools remain usable
  for developer drills, while production proposals call their methods directly.
- [Browser Use](https://github.com/browser-use/browser-use) 0.13.11 provides a larger
  agent-driven browser loop. Replacing the current adapter would add a second ownership
  model and routine inference costs; it is not installed for production.

The upstream [locator-handler API](https://playwright.dev/python/docs/api/class-page#page-add-locator-handler)
checks overlays during actionability and charges handler time to the original action
budget. Only known optional dialogs are registered. Browser actions stay self-contained.

## Performance

Reproduce with the project Python environment:

```powershell
python scripts/benchmark_recovery.py --samples 100 --baseline d0800a9 --output artifacts/recovery-overhaul/benchmark.json
```

Alternating warmed local fixtures, 100 samples per implementation:

| Three deterministic fixture actions | Baseline | Branch |
|---|---:|---:|
| p50 | 116.345 ms | 135.222 ms |
| p95 | 147.606 ms | 151.225 ms |
| p99 | 150.642 ms | 151.525 ms |

Final p95 changed **+2.45%**, within the <5% target. Median/mean increased; the handler's
normal-path check is a real tradeoff, not a general speedup. Validation-only recovery
(10 samples) changed p95 from **166.781 ms to 64.169 ms**; extra contexts fell from
**10 to 0**. The combined benchmark used 2.297 s Python CPU over 30.079 s wall time.
An earlier independent run measured +0.28% fast-path p95; both raw runs are retained locally.
Chromium/Node CPU is excluded. These are local fixtures, not a live checkout or provider
latency guarantee; 100 samples provide limited p99 confidence.

## Offline source repair

`python scripts/repair_incident.py --incident incident.json --worktree ../retail-repair`
prepares an isolated branch/worktree from a clean committed baseline. Export incident
records from the read-only `/api/recovery-incidents` endpoint. Extra fields are discarded.
Repeated signatures cannot create the same branch twice.

Add `--run-codex` to invoke the installed Codex CLI with a read-only sandbox, no inherited
user configuration/MCP servers, a minimal environment and a strict output schema.
The host applies only allowlisted source files plus new `tests/test_recovery_*.py` files.
Every proposed file is checked before any write. If evidence is insufficient, the
correct result is `needs_evidence`, with no fabricated patch. This follows the
[official iterative repair workflow](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex).

`python scripts/repair_incident.py --validate <worktree>/artifacts/source-repair`
requires the new regression to fail against the original component and pass with the
patch, runs checkout/recovery/transaction tests, then enforces the p95 benchmark gate.
It writes a review report and never merges, pushes, switches production branches or
promotes a runtime recipe. Failed tests/performance leave the patch isolated for review.
Validation executes proposed code locally: use a disposable development environment
with no account vaults or production credentials. Git worktrees are source isolation,
not an OS security boundary. Live Codex/provider calls are optional and were not used
for fixture validation; no API spend or real purchase is needed to test these gates.


The controlled concurrent load drill (`python scripts/benchmark_recovery_load.py`)
used 2, 8 and 16 independent contexts with half assigned a simulated 300 ms provider
failure. At 16 contexts, all eight healthy tasks completed three control reads within
150.79 ms; two AI calls ran and six additional failures received backpressure. The
whole concurrent batch completed in 447.14 ms. Session storage stayed isolated, all
contexts closed, and the recovery pool returned to idle. Python working set changed
from 70.70 to 70.90 MB during that batch. Browser/Node memory is excluded; the small
per-account sample count is not evidence of production tail latency or capacity.

Browser validation used the Playwright MCP on the isolated application at port 8789:
AI mode choices, task recovery/resume display through the compact refresh endpoint,
keyboard focus preservation, desktop layout and 390px layout without horizontal page
overflow. Screenshots are in `artifacts/recovery-overhaul/`. The recovery-display drill
intercepted only the fixture app's telemetry; no production task or order was started.


The expanded suite exposed a pre-existing headless-capacity test race on `main`:
its one-second polling allowance expired during the existing one-second stock handoff
window. The fixture now waits for the actual Waiting status event (bounded by five
seconds), without changing the engine's behavior or weakening its capacity assertions.

Final regression validation: **187 passed** in 146.83 seconds across browser recovery,
checkout, account/task execution, task groups, monitoring, persistence, settings,
login and resource isolation. After the final direct-popup handler recursion fix,
the focused recovery, checkout navigation, performance and offline repair suites
passed again: **38 passed** in 32.69 seconds. Both runs reported the existing
Starlette/httpx deprecation warning. Changed JavaScript files passed `node --check`,
Python compilation passed, and `git diff --check` was clean.

Tests use controlled fixtures and simulated provider responses. Real provider calls,
end-to-end Codex-generated repairs and live retailer checkout latency remain unverified.
No real purchases were placed. The branch preview uses a separate data directory;
the existing application and its saved accounts remain on the stable checkpoint.
