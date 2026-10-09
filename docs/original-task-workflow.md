# Original task workflow with the modern interface

The user's preferred reference is the original `main` Group → individual Tasks workflow, not the goal-based redesign. This revision restores that model on `overhaul/retail-workspace` while keeping the newer visual system and selected reliability improvements.

## Everyday workflow

1. Create a group: name, retailer, monitor input and maximum order total.
2. Add tasks: choose accounts, simulation/live, item quantity and task behavior.
3. Start all, selected tasks or one task. Each task has independent status and controls.

The desktop workspace again has group settings on the left and a task table on the right. The task table includes selection, account, quantity, status and actions. Monitor preferences and schedules have focused editors. Task options retain individual connections, Buy Now, shipping requirements, payment-browser behavior, retry delay and individual start times. Existing saved options outside these focused editors are preserved.

Purchase-goal modes, unit targets, reservation metrics, global purchasing defaults and fourteen-option account override forms are removed from the primary workflow. A blank task order limit inherits the group's current limit at the next start. An explicit limit changes only that task. There is no need to configure a separate account-assignment layer.

Each task normally attempts one purchase; multiple tasks can purchase independently. This is intentionally different from a first-success race. Existing looping groups retain their configured behavior. In manual browser-review mode, users still verify the final checkout total themselves.

## Compatibility and execution

Original groups and tasks work directly, with no data conversion. Plans created in the newer goal-based workspace remain available under **Tools → Saved purchasing plans** for viewing, stopping and reconciling outcomes. Their APIs, schedules and journal remain compatible. They are not converted to individual tasks because that could silently change purchase limits. Previously migrated original groups remain protected from duplicate execution.

One application scheduling loop services both record formats. The original task runner again handles independent tasks, while saved purchasing plans keep their coordinator. They share account locks, browser capacity and account reservation checks. This is a compatibility boundary, not two competing schedulers for the same group.

Restored tasks on the same account queue behind its lock. Different accounts execute independently. A pending submission blocks another live task on the account, including after deleting the original task. The guard is rechecked after acquiring the account lock. Repeated start is idempotent; start during stop cleanup is rejected. Group edits are rejected while any member task runs. Existing submission intent, retailer backoff, validation and cooldown behavior remain intact.

Task creation batches are atomic. Group duplication atomically copies group and tasks, clears schedules and execution state, and switches copies to simulation. Delete retains order and submission history. No destructive migration was introduced.

The interface retains local Iconify Lucide assets, design tokens, native dialogs, focus restoration, responsive layouts and non-overlapping polling. `/api/task-workspace` returns only groups, tasks and active IDs during routine polling. Runtime patches preserve task identity and unsaved group drafts; drafts are replaced when navigating to a different group.

## Verification

`tests/test_original_task_workspace.py` covers task independence, inherited limits, atomic batch validation, safe duplication, configuration locks, idempotent start, effective limits during execution, pending submissions after deletion, and start/stop races. Existing goal-based execution tests remain applicable to compatibility plans.

`scripts/smoke_task_workspace.js` runs against isolated fixtures on port 8783. Its 15 scenarios cover four-field group creation, multi-account tasks, independent settings, individual/bulk/selected controls, drafts, keyboard focus, isolated errors, schedules, simulated completion, safe duplication, deletion, navigation and a 390px layout. No live purchase is placed. Screenshots and logs are in ignored `artifacts/overhaul/`.

Validation: **285 passed, 4 skipped** in 382 seconds, with one existing Starlette/httpx deprecation warning. All **15 Playwright scenarios passed**, with no JavaScript errors. The full suite also covers existing browser/fingerprint functionality. Logs are in `artifacts/overhaul/restored-full-suite.log`.

This revision makes no new throughput claim. Original tasks maintain separate monitors; shared monitoring optimizations from the goal coordinator apply only to saved purchasing plans. Original-task submission recovery still uses saved retailer evidence and the existing reconciliation utility; there is no new automatic retry or general-purpose outcome-resolution form for these tasks. Pending submissions appear in Orders and continue to block the account until verified.

The updated preview runs at **http://127.0.0.1:8784**, with an isolated encrypted copy in `data-restored-tasks/`: two accounts, two groups and seven tasks. Automatic schedules are disarmed in this copy. Execution policy blocked stopping the older port-8782 process, so that instance and its data were left untouched. Use port 8784 for this workflow.
