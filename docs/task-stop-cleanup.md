# Account ownership after stopping tasks

Overlapping task Stop requests previously called `cancel()` on the same runner twice. The second cancellation could interrupt its final browser cleanup before account-lock and browser-slot release. The request still removed the task from the active list, so account actions reported a busy account despite all tasks appearing stopped. A cancelled HTTP request could propagate cancellation into teardown in the same way.

Task Stop and Stop All now share an in-flight operation across callers. Requests await that operation through `asyncio.shield`; losing one caller does not interrupt cleanup. Start remains blocked until teardown completes. The first Stop still cancels immediately, preserving Start followed immediately by Stop without opening browsers. A runner already finishing naturally is drained without cancelling its cleanup. Nested cleanup finalizers close the account browser even if trace or monitor teardown fails and release only resources actually acquired by that runner.

Durable task-group claims, submission records and uncertain outcomes are unchanged. The fix does not clear account locks speculatively or discard reservations. Restarting the app clears stale in-memory locks created by the old implementation.

Validation (local fixtures, no retailer purchases):

`python -m pytest -q tests/test_stop_cleanup.py tests/test_original_task_workspace.py tests/test_account_readiness.py tests/test_redesign.py tests/test_task_group_execution.py tests/test_task_group_redesign.py tests/test_core.py tests/test_monitors.py tests/test_session_verification.py`

136 passed, 1 skipped before adding two additional overlapping/cancelled Stop All cases; all 8 focused `test_stop_cleanup.py` cases then passed. Coverage includes immediate cancellation, overlapping stops, cancelled request waiters, delayed browser close, natural completion, cleanup errors, account isolation, queued tasks, restart, and uncertain-outcome preservation. The existing Patchright dashboard fixture verified Start → Stop → Start and desktop/narrow screenshots were inspected. Tests use controlled adapters/simulation; live browser teardown latency is not benchmarked.
