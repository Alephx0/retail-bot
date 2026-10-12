# Interrupted signup and paced input repair

Target: `feature/deterministic-recovery`, local service port 8789.

Observed failure: the open account browser was at `/ax/claim/intent`, with an explicit new-account heading and a recognized Proceed button, no CAPTCHA frames and no CVF response field. The dashboard still reported the prior Arkose readiness timeout. Durable preliminary/submission flags selected Open sign-in, but ordinary sign-in neither carried signup authorization nor recognized that interstitial as an immediate lifecycle transition.

Resume signup now uses the existing account lock and browser capacity, reuses an owned open browser where possible, and checks the saved identifier through Amazon sign-in. Only an explicit negative account-existence response for the identifier submitted in that context reconciles the old registration flags. Prior flags remain in bounded encrypted attempt history. Existing accounts sign in. Unknown responses, ambiguous controls, foreign destinations and cooldowns retain submission guards. Ordinary sign-in still cannot silently register an account.

The sign-in readiness predicate recognizes the new-account heading immediately. Registration follows the observed Proceed control in the current flow. The watcher records current lifecycle evidence, clears stale CAPTCHA status after navigation, and distinguishes transient frame loading from manual verification. A readiness timeout invokes one bounded read-only Chrome DevTools MCP inspection per document. Provider solves still require the existing validated adapter; missing frames are not guessed and uncertain paid requests are not replayed.

Recovered signup action controls are cross-checked against the pinned Chrome DevTools MCP accessibility snapshot on the isolated account connection, then revalidated locally before native paced input. Raw snapshots stay local. This does not grant an AI unrestricted CAPTCHA, consent or registration submission control.

Paced pointer paths have a signed 12-22% curve (80 px cap), with existing time-based smooth movement and exact endpoints. Eligible ASCII text/email/search values of at least five characters contain one immediately corrected letter slip. Passwords, verification codes, payment fields and specialized types are excluded. The final field value must match exactly; no Enter presses or automatic submit retries are introduced.

Verification uses intercepted local browser fixtures and mocked paid providers. No live registration or purchase was submitted during development. Resume fixtures cover new/existing accounts, unknown responses, ambiguous controls, existing browsers, identity mismatch and cancellation after submission. Real pinned Chrome DevTools MCP attachment and single-inspection behavior are exercised. Dashboard screenshots at 1440 and 390 px were visually inspected; interrupted accounts show Resume signup.

Performance: 100 alternating warmed local checkout samples against the pre-change source: baseline p95 151.288 ms, current 150.962 ms (-0.22%), within the 5% gate. This measures the unchanged deterministic checkout path, not live Amazon, network/provider latency or intentionally paced typing. Raw evidence is in `artifacts/signup-resume/performance.json`.

Reproduce focused checks with:

```
python -m pytest -q tests/test_signup_resume.py tests/test_account_lifecycle.py tests/test_signup_cvf_captcha.py tests/test_paced_corrections.py tests/test_pointer_smoothing.py
python -m pytest -q tests/test_account_creation.py tests/test_signup_proceed.py tests/test_behavior.py tests/test_input_timing.py tests/test_humantyping_profiles.py tests/test_login.py
```
