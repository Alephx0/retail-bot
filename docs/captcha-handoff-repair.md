# CapMonster signup handoff

The previous signup watcher set `captcha_submitted` before calling CapMonster.
A failed request, timeout, changed page, or rejected solution therefore looked
like a retailer submission. Subsequent watcher passes erased the original
reason with "automatic attempt did not finish verification."

Token attempts now keep separate durable request, processing, solution,
submission-intent, and submitted phases. Safe provider error codes survive
watcher updates. Provider task references stay in the encrypted attempt record;
the public signup state never contains tokens, blobs, credentials, or raw
provider response text. Interrupted requests cannot create duplicate paid tasks.

For new Amazon signup pages assigned to CapMonster, an account-scoped handler
captures `/fc/gt2/public_key/` initialization before Arkose consumes its blob.
Only the observed Amazon CVF form and matching initialization are eligible.
The normal browser request waits for the bounded solver operation. On failure
it resumes so the ordinary puzzle remains available; after response dispatch
it is aborted. A cancelled or closed page releases the hold. Fresh initialization
can reconcile a previous CAPTCHA attempt without clearing registration or OTP
submission guards. Loading a different origin never activates this handler.

The captured initialization may precede the inner frame's expansion. Readiness
accepts a collapsed inner frame only while this handler still owns the fresh
initialization; the retailer form and frame parameters remain revalidated.

CapMonster's token API is not an image-by-image puzzle click API. Its documented
flow requires an unconsumed blob, so clicking Start Puzzle before requesting a
token is not the automatic token handoff implemented here. Reference:
https://docs.capmonster.cloud/docs/captchas/funcaptcha-task/

Live diagnosis clicked Start Puzzle in the user's existing session and submitted
one task to the configured provider. CapMonster accepted the task, then returned
`ERROR_CAPTCHA_UNSOLVABLE`, with no token to apply. The initial fresh-capture
check exposed the collapsed-frame issue without sending another paid request.
A subsequent GET of that verification URL no longer produced a challenge.
These observations do not establish that CapMonster can solve every challenge.

Controlled tests cover immediate/delayed/collapsed initialization, provider
failure, cancellation, timeout, changed blobs, off-origin requests, single
submission, account isolation, and redaction. Existing CVF, signup, lifecycle,
resume and takeover regressions remain part of validation. Performance evidence
is under `artifacts/captcha-handoff/` and excludes provider/network latency.

Measured local CVF validation (50 alternating samples after 10 warmups):
baseline median/p95 64.99/78.81ms; patched 64.48/85.55ms. The p95 increase
was 6.74ms (8.6%), within the 10ms/10% local regression gate. Concurrent
fixture activity and the small sample limit interpretation; this is not a
claim of faster live solving. No fixed browser settle sleeps were added.
