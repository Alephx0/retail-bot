# Retail workspace overhaul

Historical report for commit `aac0172`. The subsequent [original-task workflow restoration](original-task-workflow.md) supersedes its primary task-group UI and scheduler-retirement decision.

Branch: `overhaul/retail-workspace`, created from `main` (`58f4509`). The original checkout and its uncommitted work were left intact. The previously tested group coordinator and concurrency repairs were carried over explicitly; unrelated fingerprint changes were not included.

## Architecture audit and decisions

The main application used persistent groups plus separately configured tasks, a legacy scheduler, encrypted SQLite records, and a vanilla JavaScript dashboard. Task and account forms repeated defaults; later scripts replaced earlier global functions. The first task-group branch introduced an encrypted execution journal, independent account assignments, quota reservations and a new scheduler, but exposed almost every implementation setting during creation and kept both workspaces active.

The strongest part was the transaction model. It was retained rather than replaced with another queue or execution layer. The main weaknesses were competing execution entry points, configuration duplication, serialized browser startup, repeated encrypted history scans, full-view replacement, and a mismatch between user intent and the task-centric interface.

There is now one execution entry point: a Task Group. Its immutable revision defines products, accounts, preferences and a goal. Starting creates a run with fully resolved settings. Each account receives an independent effective configuration and runtime state. The coordinator schedules bounded asynchronous reads and checkouts. Browser leases isolate accounts and prevent two groups from mutating the same account concurrently.

```
Workspace defaults
    -> sparse group preferences + account references
        -> sparse group-local account overrides
            -> frozen run configuration
                -> account operations -> journaled attempts -> verified outcomes
```

The legacy scheduler is disabled in the application. Existing plans remain readable and can be imported from Task Groups; migration backs up the encrypted database and preserves the original records. Legacy engine code remains for compatibility tests and shared services, but cannot execute legacy tasks in the application. The old task workspace and its stylesheet were removed.

## Configuration and workflow

The default creation form has six fields: optional name, retailer, product, order-total limit, run mode, and start time, plus account selection. A name can be derived from the product. Simulation, review before purchase, one successful order, safe seller/condition rules, and bounded concurrency are defaults.

| Setting category | Treatment |
| --- | --- |
| Product, retailer, accounts, order-total limit | Visible and essential |
| Scheduling | One selector; timing fields appear only when scheduled |
| Quantity, checkout behavior, goal, seller/condition | Optional purchase preferences |
| Group spending limit | Derived from order limit and goal unless explicitly set |
| Account quantity, cap, behavior, enabled state | Focused account editor with inheritance labels |
| Retry, timeout, variant and account purchase limits | Optional account preferences; supported existing values retained |
| Monitor/check-out parallelism and preparation lead | Bounded engine defaults; existing explicit plan values retained |
| Credentials, proxy, fingerprint, account session | Global account record; referenced rather than copied into plans |
| Browser and network details | Settings or contextual account profile editor |
| Shipping/payment profiles | Optional Tools resource; Amazon uses retailer-account defaults |

`inherited_fields` explicitly identifies group values inherited from workspace defaults. Those values are omitted from stored revisions and resolved by the backend. Old revisions without this field remain explicit, preserving their behavior. Only actual account overrides or a disabled assignment are stored. Resetting overrides removes redundant assignment values.

Changing defaults affects the next run. Active and paused runs keep their starting settings, and the detail API displays that same snapshot. Saving a group requires its expected revision and a stopped run. Browser settings and worker capacity are visibly locked while groups are active.

## Purchasing correctness and lifecycle

The retained coordinator supports first confirmed order, multiple confirmed orders, and explicit unit goals. All modes reserve account capacity, quantity and money before checkout. First-success mode permits only the necessary reserved order, while monitoring accounts concurrently; simultaneous final submissions would undermine that guarantee. Multiple-success mode permits bounded independent attempts up to the configured limits.

Durable intent precedes irreversible submission. Confirmation requires an order identity; duplicate confirmations are accounted for once. A possibly submitted or mutated attempt keeps its reservation after timeout, stop or restart until verified. Uncertain outcomes are reconciled in Activity using retailer order/cart evidence. Neither restart nor another Start command silently replays a purchase.

Start is idempotent. Group and account pause/resume/stop are independent commands. Repeated commands and stale generations cannot restart cancelled operations. Existing tests cover account failure isolation, retailer backoff, concurrent quota claims, cancellation and restart recovery.

Deletion stops active work and archives configuration; it retains execution history. Unresolved outcomes block archival. Archived groups cannot be edited or restarted. Simulations now appear in Orders with an explicit simulation flag; restart recovery reconstructs a missing history projection without double-counting.

## Interface and design system

Task Groups is the landing workspace. Overview, Accounts, Orders and Activity are primary destinations. Less frequent resources—profiles, connections, product lists, account utilities, verification services and diagnostics—are under Tools. Accounts expose Open, Browser profile/test, and More actions. JSON import and folders remain available.

Groups show the objective, lifecycle controls, order/spending progress and an account table by default. Account rows report inherited/custom preferences and offer contextual recovery. Narrow screens use stacked account rows. Advanced product targets and filters remain editable without exposing them in the default form.

The centralized stylesheet defines dark surfaces, typography, spacing, controls, focus rings and semantic status colors. Status always includes text. A local Lucide subset retrieved through Iconify replaces navigation glyphs and task-action symbols; no icon CDN or frontend framework was added. Icon licensing is in `docs/icon-license.txt`.

Native dialogs provide focus containment, Escape handling, labelled headings, destructive confirmation with Cancel focused, and focus restoration. Tabs support keyboard navigation. Optional settings retain unsaved drafts. Browser regression tests cover profile editing, generated fingerprint controls, test websites, sign-in behavior, settings secrets, validation, and narrow layouts.

The public Refract site informed hierarchy, restraint and proportions only. It was a light marketing site, not an inspected private application. No private interface or branding was copied. shadcn's accessible dialog patterns were inspected; installing React components in a vanilla frontend would have added unnecessary infrastructure, so equivalent native controls were used.

## Performance evidence

Measurements are local controlled tests, not production retailer throughput claims.

| Measurement | Earlier baseline | Current measurement |
| --- | ---: | ---: |
| 100 mocked account browser launches, 10 slots | 6,186 ms | 619 ms |
| Peak simultaneous mocked launches | 1 | 10 |
| Encrypted quota read, 10 / 100 / 1,000 attempts | Previously scanned history | 0.033 / 0.034 / 0.033 ms median |
| Loaded JavaScript + CSS, prior group branch | 213,437 bytes | About 179 KB (16% smaller) |
| List polling payload, same fixture data | Full state: 35,529 bytes | Group summaries: 10,801 bytes (70% smaller) |
| Unchanged group detail over 4.5 seconds | Full replacement on poll | 0 DOM mutations; focus preserved |
| Chrome DevTools local reload | No matched baseline | LCP 44 ms; CLS 0.01; no throttling |

Launch measurements compare the prior committed pool (`fc64488`) against the retained repaired pool, using identical fake launch delays and limits. Run `python -m scripts.benchmark_task_groups` to reproduce. The overhaul does not claim that UI changes caused those earlier engine gains.

The UI uses non-overlapping, visibility-aware two-second polling with compact group APIs. Unchanged responses do not rerender. Changed detail responses patch nodes, preserving unaffected account rows and keyboard focus. An asynchronous click handler snapshots its command before awaiting, preventing a Start button that turns into Pause from executing both actions.

## Validation and limitations

`tests/test_workspace_overhaul.py` adds coverage for sparse defaults, explicit legacy compatibility, account override reset, frozen execution/read models, invalid inheritance, active deletion, uncertain reservations, legacy scheduler retirement, and simulation history recovery. Existing group suites cover concurrent starts, atomic goals, retry/cancellation, account isolation, migration and stress scenarios.

`scripts/smoke_overhaul.js` is a repeatable Playwright MCP workflow against an isolated fixture app on port 8780. It exercises account creation, multi-account group creation without advanced changes, one-account overrides, enable/disable, group/account controls, unchanged-poll focus, keyboard navigation, a mocked account failure, edit/duplicate/delete, all navigation destinations, mobile layouts, and connection loss/recovery. No live purchases are made. The browser-only failure scenario intercepts the read model; backend failure isolation has separate controlled execution tests.

Final full suite: **279 passed, 4 skipped** in 361 seconds. The skips cover optional browser integrations unavailable in this checkout. One existing Starlette/httpx deprecation warning remains. The final Playwright workflow passed **15 scenarios**, including scheduled release cancellation and two independent simulated orders with exactly one confirmation per account. It measured six visible non-account fields, zero idle mutations and no JavaScript errors. A separate virtual-account review check confirmed one simulated order and a disabled completed-goal Start control. Settings browser regressions validated all five tabs at 390px.

Before/after screenshots, test logs and benchmark JSON are under ignored `artifacts/overhaul/` and `artifacts/task-group-build/`. Playwright, Iconify, shadcn and Chrome DevTools MCP were used. The browser servers restrict file reads to the original workspace, so test scripts from this worktree were supplied as code; screenshots were captured through Playwright.

Live checkout remains Amazon US only. Other regions support simulation. Production retailer acceptance, unfamiliar checkout layouts, external verification providers and real signed-in purchases were not validated. The interface reports these capabilities rather than implying universal retailer support. Groups are capped at 100 accounts; browser and checkout concurrency remain bounded. Long history is paginated/capped in detail responses; authoritative purchase counts come from the ledger, not visible rows. The UI still polls rather than subscribing to pushed events.

The branch preview uses an isolated encrypted backup of the main workspace. Original accounts and saved plans are preserved; legacy schedules do not auto-run. Browser installations and profile directories are not duplicated by the database backup. Existing saved plans can be imported explicitly in Task Groups. Development principles are recorded in the root `AGENTS.md`.

The launched preview is `http://127.0.0.1:8782`, using this worktree's ignored `data/` directory (two accounts and two saved legacy plans copied from main). Test fixtures use port 8780 and separate ignored data. Neither workspace is merged back into main automatically.
