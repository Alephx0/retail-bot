# Signup MCP takeover and checkpoint return

The observed tab on port 8789 was Amazon's `Looking for Something?` error page at a bare `/ap/signin` URL. It contained a homepage link and no CAPTCHA, OTP, or sign-in fields. The old watcher mislabeled every unmatched state as verification and created a manual harvester entry. The previous read-only MCP inspection did not navigate to a usable checkpoint.

Changes:

- Resume and existing-account handoff enter through the account-history route, allowing Amazon to construct its sign-in redirect instead of inventing a bare authentication URL.
- `signup_recovery.recover_signup` owns the isolated browser's pinned Chrome DevTools MCP connection across a bounded navigation loop. The configured model proposes an observed navigation control. Local validation requires a unique same-region, same-tab homepage or sign-in anchor; the controller dispatches through MCP and waits on the document lifecycle, then checks the next checkpoint.
- Recovery stops at sign-in, registration, new-account, OTP, CAPTCHA, optional passkey, or session checkpoints. MCP ownership is released before deterministic credentials, signup, mailbox, solver or session handling resumes. Delayed DOM controls are awaited within the existing configured recovery deadline.
- Unknown pages and unexpected exceptions attempt recovery before requesting review. They no longer masquerade as human verification or create generic harvester entries. Exhaustion is displayed as Recovery paused, with categorical recovery status. Existing configured OTP/solver handling retains precedence on real verification pages.
- Prior submission intents remain unchanged during navigation. Negative account-existence reconciliation still requires this account's submitted identifier. Off-site links, ambiguous controls, popup/download links, repeated routes, timeouts and cancellation stop input. HTTP 403 and full Retry-After handling remain enforced.
- Agent, navigation and checkpoint evidence are bounded and categorical. Credentials, challenge contents, URLs with query strings and raw page snapshots are not sent to the model. Recovery has no arbitrary JavaScript or credential-entry tool.

Verification:

- Real Chrome DevTools MCP fixtures cover immediate/delayed checkpoints, CAPTCHA/OTP handoff, foreign links, ambiguity, loops, missing controls, cancellation and a 121-second Retry-After.
- An end-to-end interrupted-signup fixture traverses the error page, homepage, sign-in, explicit new-account response, registration and email verification. Registration submits once; no manual handoff occurs before the actual code checkpoint.
- A configured-model demonstration (`gpt-6-luna`) made two validated MCP navigation clicks, reached sign-in in the same context, released ownership, and produced zero manual handoffs. Measured elapsed time was 10,440.24 ms, including model calls. All retailer routes were intercepted locally; only the configured AI API was contacted.
- Desktop (1440 px) and narrow (390 px) dashboard states were browser-tested and visually inspected. Recovery and return-to-automation statuses remain visible during pending actions.
- 100 alternating local checkout samples: baseline p95 154.659 ms; current p95 153.771 ms (-0.57%). This is a warmed fixture benchmark, not live retailer or model latency. Evidence is in `artifacts/signup-takeover/`.

Reproduce:

```
python -m pytest -q tests/test_signup_takeover.py tests/test_signup_resume.py tests/test_signup_proceed.py
python -m pytest -q tests/test_account_creation.py tests/test_signup_verification.py tests/test_signup_email_code.py tests/test_signup_cvf_captcha.py tests/test_browser_agent.py tests/test_devtools_recovery.py
```

The controller is bounded recovery, not unrestricted browsing: unsupported or ambiguous retailer steps can still require review after recovery is exhausted. Genuine verification is handled by the configured service when its supported adapter is ready; uncertain submissions are never blindly replayed.


Live follow-up identified two additional adapter defects: the pinned MCP text page list is `id: title (url)`, while the old parser assumed `id: url`; Amazon's Proceed input uses `aria-labelledby` with no value or inner text. The transport now explicitly enables the pinned server's structured content and selects exactly one full URL match from structured `pages`. Missing/duplicate identities fail closed. Signup action cross-validation resolves referenced labels before comparing to MCP accessibility names. Titled pages, misleading titles, duplicate URLs and an empty submit input with a referenced label are covered by real MCP regressions. The fixed adapter was also attached read-only to the actual open Amazon tab: exact page match and one Proceed action were verified without dispatching input.
