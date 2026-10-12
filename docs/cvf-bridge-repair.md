# Amazon CVF bridge and one registration retry

The previous adapter wrote a provider's Arkose token directly into
`cvf_aamation_response_token`. The live page's scripts show that these are
different tokens: the wrapper's `postChallengeEvent` forwards the Arkose answer
to Amazon's ACIC controller, which calls `/aaut/verify/`. Only after Amazon
accepts that response does its own completion callback submit the Amazon
session token through `/ap/cvf/verify`.

The adapter now uses that observed wrapper callback. It binds delivery to the
original document, wrapper, Arkose frame and ACIC session; validates the
same-origin Amazon endpoint and CVF form; and sends the answer once. It never
writes a raw Arkose token into the final CVF response field. Amazon retains
control of validation and final submission. Missing or changed bridges stop
before dispatch. No account cookies or credentials go to the solver.

A registration internal error, including its generic "try again later" wording,
now permits one automatic account check and continued signup when Amazon
explicitly confirms the identifier is unregistered. The check is recorded
before navigation. A repeated error stops rather than looping. Explicit
Retry-After deadlines are observed; HTTP denials/rate limits and explicit
"too many requests" responses retain their restriction. The delay comes from
the response, not a fixed settle sleep. Cancellation or a recreated watcher
cannot replay a dispatched registration check.

The mobile-number step observed after successful verification uses
`cvfPhoneNumber`, `cvf_phone_cc_native` and "Add mobile number". These controls
now use the existing account-owned SMS workflow, region selection, price cap
and durable purchase guard.

Live validation on 2026-10-12 used the user's open session and assigned 2Captcha
connection. One provider answer caused one Amazon exchange (HTTP 200), followed
by one CVF submission (HTTP 200) containing a different, Amazon-issued token.
The page advanced to email verification with no CAPTCHA or error. The existing
watcher then submitted the email code and reached the phone step. SMSPool
rejected the subsequent configured number request with HTTP 422; no phone
number was submitted, and no replacement SMS request was made. Full account
registration is therefore not claimed complete.

Fixtures distinguish provider and Amazon session tokens and require the
intermediate exchange before final submission. They cover delayed/rejected
answers, changed sessions, missing/foreign bridges, duplicate dispatch,
registration retries, repeated errors, Retry-After, cancellation, independent
accounts and the observed phone form. Paid solves and SMS requests are mocked
in development tests. The live evidence export contains categorical outcomes,
never tokens, account details, provider task IDs or raw response bodies.

Validation: 134 regression tests and 16 separate phone/SMS tests passed.
Focused bridge/control checks were repeated after the final batching change.
Desktop (1440px) and narrow (390px) retry-status layouts were browser-tested
and visually inspected.

Performance: the initial preparation implementation missed the local +10ms
p95 gate (first with concurrent regressions, then without them). Combining
the bridge binding read with the existing wrapper-state read removed the
extra protocol round trip. Final 50-sample alternating baseline/patched
median/p95 was 73.68/93.09ms versus 73.59/90.57ms, passing the gate. Small
samples and other browser activity limit precision; this is not a claim of
faster live solving. Reproduce with `scripts/benchmark_cvf_bridge.py` against
the deployment's baseline source snapshot.

Reference: [2Captcha's FunCaptcha API](https://2captcha.com/api-docs/arkoselabs-funcaptcha)
documents its provider response token. Amazon's additional exchange was
verified against the live page and network sequence rather than inferred from
that API documentation.
