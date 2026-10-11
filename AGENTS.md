# Retail Desk development standards

## Architecture

- The primary workflow is the original Group → individual Tasks model. Groups own retailer, monitor input and shared limits; tasks own account, quantity, behavior and optional overrides. Do not replace this with purchasing-goal configuration without a user request.
- A blank task order limit inherits its group limit. Editing a task must not modify its global account, siblings or another group. Preserve other saved options during focused edits.
- One engine scheduling loop services original tasks and compatibility purchasing plans. The coordinator retains its durable journal and recovery for saved plans; do not start a second scheduler in the application. Shared account locks and browser limits apply to both.
- Task execution captures its group/task configuration at startup. Stop tasks before changing their configuration or group settings.
- Preserve atomic quota reservations, account leases, durable submission intent, idempotent confirmation and uncertain-outcome reconciliation. Never retry a possibly submitted order. Respect retailer rate limits and purchase restrictions.
- Verify live task account sessions before purchasing; independent read-only stock scans may run during sign-in. Reuse the verified browser for immediately available stock, and release workers while waiting for restock. Treat anonymous inventory as a wake-up signal, then recheck purchase eligibility in the assigned account before carting. Respect Retry-After in full; never treat a sign-in or delivery restriction as an inventory stockout.
- Keep encrypted credentials in the existing vault. Group plans contain references, never copied credentials. Migrations must preserve original records and have tested recovery.
- Prefer bounded asynchronous operations and compact read models. Measure before adding infrastructure. Do not duplicate authoritative state in the frontend.
- Shared inventory monitors use read-only stock inspection without AI purchase-control recovery. Keep account offer checks before carting. Use bounded, on-demand stage timings to diagnose latency; distinguish nested timings, intentional waits, and live network variability from controlled benchmarks.
- Amazon action readiness must depend on observable page or lifecycle states, never fixed settle sleeps. Bound stalled waits using browser timeouts, preserve retailer polling/backoff intervals, and never retry an irreversible action because its expected state did not appear. Test immediate, delayed, missing and interrupted transitions.
- Amazon cart flow adds once from the verified product page, opens the cart once (or reuses Amazon's redirect), deletes uniquely identified unrelated active items, and verifies the exact target quantity. Do not restore the product/cart preflight round trip. A possibly completed cart mutation must pause on uncertainty; never classify a post-add failure as a pre-mutation rejection.

- Successful browser actions are always deterministic, including saved legacy agent mode. Keep AI, diagnostics and source repair out of the fast path. Recovery owns only the current task page, obeys its deadline, and cannot retry a possibly submitted mutation.
- Runtime recovery uses bounded direct Patchright calls. MCP is a development interface, not a second browser controller. Only explicit optional-dialog handlers may dismiss interruptions; verification, consent and purchase choices remain protected.
- Model proposals are unapproved candidates. Source repairs run offline in isolated worktrees with a reproducing fixture, regression tests, performance gate and review. Never edit or reload the running application's source. Export only allowlisted incident evidence, never page prose, raw URLs, account data or traces.

## Product and interface

- Justify every new setting with a concrete user need. Group creation has name, retailer, monitor input and order limit. Preserve the requested General, Monitoring, Checkout and Advanced group tabs and profile/profile-group assignment previews in Add tasks. Keep purchase goals, unit quotas and global purchasing inheritance out of the primary workflow.
- Use progressive disclosure for optional preferences. Preserve explicit settings when a form edits other values. Show whether an account inherits or overrides a setting.
- Use the existing native HTML component patterns: labelled inputs, native modal dialogs, confirmation with cancel focused, accessible buttons and tab navigation. The frontend is vanilla JavaScript; do not add React just to consume shadcn components.
- Use the local Iconify Lucide subset in `static/icons.js`. Verify icons through Iconify before adding them. Include accessible names for icon buttons. Do not introduce another icon family, emojis or runtime CDN requests.
- Reuse `static/style.css` tokens for color, spacing, typography, surfaces and controls. Use restrained dark surfaces and semantic statuses with text, not color alone.
- Prefer simplifying a workflow over adding navigation. Optional resources belong in Tools. Avoid redundant buttons, nested cards and repeated information.
- Preserve keyboard focus during runtime updates, useful loading/error/empty states, responsive layout and readable contrast.
- Follow the reference-based workspace: 70px icon-only desktop rail (accessible names retained), tall group cards, a bounded task workspace with independently scrolling sidebar and table, account/profile folders, proxy cards, and horizontal settings tabs. Keep preference switches distinct from selection checkboxes and restore navigation labels on narrow layouts. Extra operational capabilities live in Operations or contextual Tools tabs; do not reintroduce duplicate destinations.
- Search and status filters scope task bulk actions to visible tasks unless the user explicitly selects tasks. Keep the action label accurate, clear selection when its filter changes, and preserve focus while updating results.
- Reference applications inform presentation only. Keep the retail engine, encrypted vault, and real integration APIs authoritative; never import mock execution, authentication bypass, or licensing code.

## Verification

- Test account independence, live inheritance, frozen runs, start/stop races, cancellation, reservations, uncertain outcomes and migration when their behavior changes.
- Use controlled retailer fixtures or simulation; never place real purchases during development tests.
- Browser-test changed flows using Playwright. Inspect desktop and narrow layouts visually; a successful build is insufficient.
- Record reproducible performance measurements and their limitations. Keep reports concise and distinguish measured improvements from assumptions.
