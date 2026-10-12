# Signup recovery after a retailer registration error

Observed on port 8789: 2Captcha returned a token and the existing CVF form was
submitted. Amazon subsequently returned `/ap/register`, a visible internal-error
banner, the customer-name field and empty password fields. The application
reported exhausted MCP recovery. This evidence does not establish whether
Amazon rejected verification or failed later in registration.

A later inspection of the full visible error found "try again later." This
matches the existing retailer cooldown classification. That exact response
now pauses directly with an accurate retailer-error message, preserving the
pause and submission guards without MCP or an automatic account check. The
bounded account check below applies only to internal errors without cooldown
or other restriction evidence.

The old watcher excluded forms with errors from deterministic form completion,
then called recovery. Recovery opened MCP before checking the page's known
controls. A supported registration page could therefore fail on MCP transport
or the already-attempted-document guard. A separate broad exception handler
could mark a successfully submitted solver response as failed when waiting for
the retailer's next state timed out.

The repair recognizes a visible internal error on the registration form and
checks account existence through the existing same-region sign-in entry. It
records the check before navigation and permits it once per explicit signup or
resume operation. It retains submission guards; only Amazon's explicit
unregistered response to the saved identifier permits continued registration.
An existing account signs in. Repeated errors pause with the retailer error
shown, without another paid solve, repeated registration, or MCP handoff.
Cancellation and reconstructed watchers retain the check guard. Cooldowns and
restricted-account responses retain their existing handling.

Recovery now checks supported pages before MCP connection and document-attempt
guards. Empty loading/redirect documents wait for observable content within the
existing recovery deadline. Signup state reads wait for DOMContentLoaded and
discard snapshots spanning different documents. No fixed settle sleep was
added. Solver request/submission phases remain separate from retailer outcome;
the public status records the registration error without claiming token
acceptance or exposing response tokens, provider task references or credentials.

Validation uses routed Playwright fixtures and mocked solver requests. Scenarios
include a submitted token followed by immediate/delayed errors, existing/new
accounts, repeated errors, failed account checks, cancellation, watcher restart,
hidden alerts, foreign origins, cooldowns, unavailable MCP, exhausted document
guards, and a slow retailer response after submission. Existing signup, CVF,
takeover, startup, lifecycle and browser UI regressions are also checked.

`scripts/benchmark_signup_error.py` compares baseline and patched state reads
using 50 alternating samples after 10 warmups. It also reproduces the old known
page failure with unavailable MCP. The performance gate permits at most 10ms
additional local p95 latency. This excludes retailer/provider latency and is
not a claim that live CAPTCHA solving is faster.

Measured local state-read median/p95: baseline 6.48/8.51ms, patched
9.73/12.50ms (p95 +3.99ms; gate passed). These measurements ran alongside
the regression suite, so browser/process contention limits precision. The
baseline reproduction failed at MCP connection; the patch returned the
`retailer_error` checkpoint without connecting.

Source work and tests run in an isolated worktree. Deployment checks the live
baseline hashes, stops the app before replacing tested files, and retains an
exact-byte rollback manifest. Account records and encrypted credentials are
not migrated or copied into plans.
