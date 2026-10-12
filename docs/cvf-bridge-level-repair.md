# Arkose level recognition repair

On 2026-10-12 the open signup page had a fully initialized Amazon CVF bridge,
but its ACIC action was `ARKOSE_LEVEL_3`. The adapter accepted only levels 1
and 2, so preparation raised the misleading "bridge is not ready" error
before requesting a paid solve.

Accept the numeric `ARKOSE_LEVEL_` family in the existing scoped adapter.
Amazon selects the level; the transport and native verification exchange do
not change. The exact action remains part of the session binding and signature,
so a level change during solving still rejects delivery. All original origin,
form, wrapper, endpoint, session and duplicate-dispatch checks remain active.
Non-Arkose actions do not become eligible. This fault requires no extra delay.

The new level-3 fixture failed with the exact reported error before the patch.
Afterward, 69 CAPTCHA tests passed, covering levels 1, 2, 3 and a synthetic
multi-digit level, a changed action, unsupported actions, the native Amazon
exchange, delayed/rejected responses, cancellation and duplicate prevention.
The full signup watcher also requests exactly one solve at level 3 and resumes
after Amazon verification. Paid requests are mocked in these tests.

A read-only check of the open live level-3 page confirms the patched binding
is available. No paid solve or live page mutation was performed for this check;
it does not establish provider success or completed account registration.

The 50-sample alternating local preparation benchmark passed its +10ms p95
gate: baseline median/p95 78.71/92.06ms; patched 78.26/90.40ms. This measures
only preparation, excludes provider/network latency and is subject to local
machine variability. Reproduce with `scripts/benchmark_cvf_bridge.py` against
the source snapshot in the deployment rollback directory.
