# Task-group redesign branch

The `task-group-redesign` branch implements the group workspace and execution
coordinator described in the [architecture proposal](task-group-architecture.md).
Existing task groups remain under **Legacy tasks**. No legacy group is silently
converted, and migration starts with a stopped simulation plan.

## Using the workspace

Open **Task Groups → Create group** and configure four sections:

1. Products: paste Amazon links or ASINs, or import a saved input list. Set each
   product's unit-price cap and quantity target when using Each product.
2. Accounts: select accounts or a folder. The selection is saved as explicit
   account IDs. Browser identity, fingerprint and connection remain account-owned.
3. Goal: Notify, Prepare for review, Automatic checkout, or Get checkout total.
   Choose Any matching product or Each product. Configure units per order,
   per-account unit limits, maximum final order total and group spending limit.
4. Timing: manual/continuous, one dated window, or weekly windows, with an IANA
   timezone and preparation lead time. Goals persist across windows by default;
   per-window quotas require an explicit selection.

Simulation is the default and never opens a retailer browser or submits an order.
Without selected accounts it uses one virtual account, so its per-account limit
still applies. Review simulations wait for **Check outcome** in Activity.
Automatic simulations complete after the deterministic product observation.

The group workspace shows **Products / Accounts / Activity**, confirmed and
reserved units, spending, account readiness and timing. Start is idempotent. Pause
blocks the next automatic mutation; Resume continues with the same reservations.
Stop cancels waiting work and retains uncertain outcomes. Duplicate creates a new
simulation plan with a fresh goal, rather than resetting old purchase history.

Advanced filters include minimum price, discount, savings, free items, seller and
condition. Discount rules require a verified reference price. Activity retains
unresolved attempts from earlier runs and offers recorded plan/browser details.

Live execution currently supports **Amazon US**. The existing adapter's final
checkout snapshot is USD-specific; UK and Canada remain simulation-only in this
new workspace until their final-total validation is implemented. Other retailer
adapters are not added by this branch.

## Execution and persistence

- `retail/task_groups/domain.py`: validated plans, products, integer money,
  qualification reasons and timezone-aware execution windows.
- `repository.py`: encrypted SQLite run/attempt/revision/event payloads, atomic
  account and quota reservations, unique command/order identities, generation
  checks and durable submission intent. Operational indexes contain opaque IDs;
  retailer order identities are hashed in the deduplication index.
- `browser_pool.py`: reusable bounded account sessions, account locks, shared
  browser capacity with the legacy runner, idle eviction and profile invalidation.
- `coordinator.py`: monitoring, account-scoped observation caching, fair account
  rotation, preparation, recurring windows, read-error cooldowns and dispatch.
- `execution.py`: account-specific revalidation and the existing Amazon cart,
  final-total and submission methods. Automated submissions are never retried
  after crossing the durable intent boundary.
- `api.py`: readiness, commands, history, reconciliation, migrations and an SSE
  event endpoint. The dashboard currently uses compact group detail polling;
  server event streaming is available for a later UI transport migration.
- `workspace_lock.py`: OS-held lock preventing two updated engines from owning
  the same data directory. The pre-redesign app does not participate in this
  lock; do not point both old and new app versions at one live data directory.

The current Amazon adapter is account-bound. Monitoring shares an observation
only for the same account, region, product and offer within its freshness interval.
It does not assume that different accounts see the same price or stock. Wider
public-stock sharing requires a separately validated adapter capability.

Browser capacity limits account contexts. A browser driver/base process and
temporary hardware probes may also exist; this is not a strict OS process-count
limit. Accounts with an active manual checkout remain exclusive. Idle monitor
sessions can be evicted for checkout work or legacy tasks. A monitor observation
is always revalidated inside the purchasing account before carting.

## Outcomes and recovery

Reservations hold a conservative maximum order allowance, including room for
shipping/tax. Final review reduces that reservation to the verified total before
automatic submission. Confirmed orders consume quota once. Proven pre-mutation
failures release quota; uncertain cart/submission results keep both the account
claim and reservation.

**Activity → View browser** opens uncertain outcomes for inspection without
restarting automation. **Resolve outcome** requires verification notes and either
a verified no-order/cart result or the actual order number, matching product,
quantity and final total. Reconciliation records user-verified evidence; it is not
an independent retailer verification. Larger actual quantities/totals are recorded
truthfully and count against subsequent limits rather than being clamped to caps.

Review mode never submits automatically. After the user completes checkout,
confirmation is checked and the actual final total is reconciled explicitly,
because the user could have changed the checkout during manual review.

On restart, pending live effects become reconciliation-required and other runs
pause for explicit resume. A missing display/history projection for an already
confirmed order is rebuilt from the durable ledger. Purchase cooldowns also read
confirmed ledger entries. A timeout between clicking and confirmation does not
release a reservation or create another automatic order attempt.

Scheduled windows have stable occurrence identities, skip nonexistent DST wall
times, and choose the first occurrence of a repeated wall time. Timed preparation
checks sessions without carting or submitting. Missing a window does not trigger
a late purchase. An interrupted window does not automatically resume after restart.

## Migration and development

The migration preview imports products and account assignments and asks the user
to choose purchase goals. It does not reinterpret a task-count multiplier as an
instruction to buy that many units. Mixed legacy task behavior is intentionally
not silently reproduced: the new plan starts with review behavior in simulation.
Choose the new behavior explicitly after checking the preview.

Applying migration requires stopped legacy tasks and no unresolved legacy
submission record. It creates an encrypted database backup in
`data/migration-backups/`, retains legacy task IDs/history, disables old schedules,
and prevents the old runner from starting the migrated group. Backups rely on the
same workspace vault key and Windows account protection as the original database.

For an isolated local instance:

```powershell
.\.venv\Scripts\python run.py --port 8770 --data-dir artifacts/task-group-workspace
```

Use the standard README installation first in a new checkout. Browser binaries,
optional fingerprint-suite packages and account data are not Git source. The
development checkout is separate from the original application's data directory.

## Validation

New tests cover concurrent reservation writers, quota persistence across restarts,
idempotent starts, duplicate order identities, uncertain cart/submission effects,
manual review, durable intent before submission, account-specific monitor sharing,
browser reuse, scheduled preparation/window closure, DST and migration.

Browser UI verification creates a group, imports two products, checks readiness,
persists the plan, starts simulated execution, and verifies automatic two-unit
completion and Activity history. Screenshots and machine-readable regression
results live under ignored `artifacts/task-group-build/`.

The full regression run passed 241 tests with four optional browser-backend tests
skipped because their dependencies are absent from the isolated checkout. After
subsequent recovery and filtering refinements, the final affected workspace,
settings and account checks passed (58 tests, including 20 task-group tests). Chrome UI
checks also exercised review, pause/resume, duplication, timing fields and details
without application JavaScript errors.

Live execution is validated with deterministic adapter fixtures, not a real
retailer purchase. The existing Amazon layout and checkout limitations continue
to apply. No throughput or retailer-acceptance gain is claimed from fixture tests.
