# Reference-based workspace overhaul

## Checkpoint and scope

The original repository's local monitor/fingerprint work was committed as `5266fbe`.
The existing `overhaul/retail-workspace` UI branch was then integrated, tested and
pushed to `main` as `f09561a`. The reference overhaul starts from that combined
checkpoint on `overhaul/refract-reference`.

The reference is the independent local application in
`../Refract Bypass/src`, inspected through its source and its running public UI on
localhost:4173. Its `analysis` binary/source-map material, authentication providers,
licensing mechanisms, mock checkout engine and simulated services were not imported.

## Comparison and decisions

| Area | Reference implementation | Retail Desk decision |
| --- | --- | --- |
| Frontend | Vanilla ES modules, page functions, local selection and form state | Retain vanilla frontend, native dialogs and existing API-backed forms; no framework migration |
| Shell | Narrow icon rail, 50px frame, restrained dark surfaces | Adapt proportions, compact typography, blue accents and flat tables; retain original branding and local Lucide icons |
| Task overview | Searchable group cards, group selector, aggregate counts | Implement search, retailer filter, cards, one-pass counts and direct group switching |
| Task detail | Information sidebar, statistics, search and bulk commands | Preserve General/Monitoring/Checkout/Advanced forms, profile assignments and independent task limits; add task/monitor tabs |
| Accounts/profiles | Folder groups, searchable tables, focused editors | Use existing folder membership API, add sidebar and search; retain real encrypted account/payment/profile fields |
| Settings | Horizontal tabs and description/control rows | Adapt layout; preserve all saved values, validation, drafts and explicit save behavior |
| Scheduling | Local simulated tasks and local-date windows | Keep existing scheduling ledger and the engine's single scheduler; no reference scheduler imported |
| Execution | Bounded local simulation, synthetic accounts/cards | Keep real retail adapters, account locks, shared monitors, browser limits and purchase submission journal |
| Persistence | Versioned JSON for synthetic data | Keep encrypted SQLite, DPAPI-bound vault key and existing relationships; no schema migration needed |
| Ancillary services | Mock IMAP, harvesters, proxy tests, analytics | Keep real retail endpoints, availability indicators, diagnostics and confirmed-order analytics |

Examined reference entrypoints, `src/ui/app.mjs`, `views.mjs`, `forms.mjs`,
`platform-form.mjs`, `schedule-form.mjs`, `ui-kit.mjs`, `style.css`,
`workspace.mjs`, `task-control.mjs`, `schedules.mjs` and architecture/parity notes.
The corresponding Retail Desk audit followed `app.py`, `engine.py`, `runner.py`,
`monitors.py`, the compatibility coordinator/repository, encrypted store, resource
APIs, browser agent, task assignment forms and shared frontend rendering.

## Problems addressed

- A wide sidebar used space while frequently used profiles and proxies required a
  trip through Tools. They now have direct navigation destinations.
- Task overview recomputed a full task-array filter for every group. A single
  pass now aggregates counts into a map and uses a set for active task membership.
- There was no task/account/group search, and task bulk actions ignored the
  displayed status filter. Commands now target selected tasks, or visible tasks
  when no selection exists. Buttons explicitly say all, visible or selected.
  Changing a task filter clears its selection.
- Folder selection required a dropdown. Accounts, profiles, proxies and product
  lists now show their folders, memberships and counts beside the actual records.
- Monitoring information had been lost at the UI branch boundary. The checkpoint
  integration restored assignments, ASIN normalization and logs. This overhaul
  adds a dedicated monitoring workspace without creating a second monitor system.
- Runtime log refresh replaced DOM nodes on every poll. It now uses the existing
  keyed patcher, preserving unchanged controls and focus.
- Settings combined a vertical menu with long full-width controls. Horizontal
  tabs and description/control rows improve scanning and collapse to one column.
- Small screens now show all eight primary destinations in a compact two-row
  navigation grid, with task/resource tables becoming readable record rows.
- Dialog inputs now have unique IDs and matching label references, and closed
  editors release their form contents. Labels cannot accidentally target a hidden
  previous editor or the group configuration underneath a dialog.
- Sparse legacy task records now receive the runner's model defaults in both
  full and compact API read views. Existing explicit values are preserved and
  saved records are not rewritten; old tasks display their actual default behavior.

## Functionality map

| Destination | Preserved capability |
| --- | --- |
| Tasks | Original group-to-independent-task model, four group tabs, scheduling, account/profile groups, previewed assignments, monitor selection, task overrides, start/stop/resume, live view, duplicate/delete |
| Accounts | Credentials, saved sessions, connection and verification options, fingerprint overrides/generated profiles, extensions, browser testing and starting websites |
| Accounts → Sessions & relationships | Bulk login verification, account/profile relationships, account network/profile assignments |
| Profiles | General, shipping, billing and payment tabs; encrypted storage; account/email matching |
| Proxies | Lists, connection/browser tests, endpoint health and latency |
| Operations → Orders | Confirmed results, CSV export and uncertain submission journal |
| Operations → Monitors | Real shared monitor status, waiting task counts, connection and bounded activity logs |
| Operations → Activity / Product observations | Existing task events and observed product availability/prices |
| Operations → Saved plans | Compatibility plans, progress, stop/delete and outcome reconciliation |
| Tools | Product lists, IMAP/codes, verification connections and queues, diagnostics, retailer integration availability |
| Settings | General, Browser & fingerprints, Notifications, Connections, Data & privacy |

No operational setting or persistent field was removed. Advanced browser/network
controls remain optional; the new search fields are transient presentation state.
Group creation still has four inputs. Repeated resource shortcuts in Tools were
removed after promoting those resources to the rail. Existing schemas, IDs,
account relationships and per-task inheritance are unchanged.

Execution remains: task start → capture configuration → shared anonymous stock
monitor → account lock and browser slot → authenticated account recheck → guarded
cart/checkout → durable submission intent → confirmed or uncertain outcome.
Saved purchasing plans retain their separate journal, but share the same engine
scheduling loop, account exclusion and resource bounds. None of this was replaced
with reference simulations.

## Measured performance

Reproduce with `node scripts/benchmark_workspace.mjs f09561a`.
Node v24.13.1, four warmups, fifteen samples, up to fifty active task IDs:

| Groups / tasks | Checkpoint median | New median |
| --- | ---: | ---: |
| 10 / 100 | 0.071 ms | 0.106 ms |
| 100 / 10,000 | 13.491 ms | 5.235 ms |
| 500 / 50,000 | 281.752 ms | 24.765 ms |

This benchmark measures pure overview HTML generation with lightweight shared
helper stubs. It excludes DOM layout, encrypted reads, API transport and checkout.
The new cards produce more HTML, so small workspaces have slightly more generation
overhead. The improvement at larger sizes comes from removing repeated full task
scans, not from increasing retailer request rates. Large task tables still render
all matching rows; pagination/virtualization was not added without a measured need.

## Verification

Playwright MCP ran against a separate fixture instance on localhost:8785. All
purchase tests used simulation or intercepted retailer fixtures. No live orders
were placed. Iconify MCP verified the Lucide search icon; existing local SVGs were
reused. Native HTML fits this frontend; no React/shadcn dependency was introduced.

Reproducible browser suites:

- `scripts/smoke_task_workspace.js`: 15 workflow checks, including creation,
  independent overrides, group inheritance, concurrent/selected start-stop,
  locked running settings, error isolation, schedule clearing, simulated checkout,
  duplication/deletion, preserved drafts, dialog focus and mobile layout.
- `scripts/smoke_profile_tasks.js`: 9 checks for all four settings tabs, keyboard
  navigation, hidden-field validation, profile/email matching, profile/account
  groups, stable random assignment preview, editing and 390px forms.
- `scripts/smoke_assignment_recovery.js`: 5 checks for stale/out-of-order previews,
  network recovery, duplicate submission prevention and connection inheritance.
- `scripts/smoke_refract_workspace.js`: group/task search and visible-only execution,
  shared monitor tabs/logs, folder filtering, fingerprint editor access, profile
  field persistence, every supplemental tab, settings drafts/keyboard handling,
  unique dialog field IDs, and eight primary destinations at 1024px, 760px and 390px.

The older Python entrypoints were brought forward rather than left with obsolete
selectors. `smoke_ui.py` / `smoke_workspace.py` delegate to `smoke_redesign.py`;
`smoke_redesign.py`, `smoke_matching.py` and `smoke_features.py` passed against the
isolated workspace. They additionally cover saved profile relationships, ambiguous
email rejection, manual verification provider health and encrypted backup download.
They accept `RETAIL_TEST_URL` and default to the isolated port 8785.

Before/after and reference screenshots are in the ignored `artifacts/` directory.
The full baseline regression run found 296 passes and two outdated browser-agent
trace assertions: shared monitoring adds one read-only availability resolution.
The assertions now include that resolution and explicitly check exactly one cart
addition. Both fixture variants and all nine monitor tests passed on rerun.
The final full run passed **298 tests in 416.49 seconds**. After adding the sparse
legacy-task read compatibility fix, all **16 task-workspace/monitor tests** passed,
including the new read-consistency/no-rewrite regression. This is 299 distinct
test cases covered across the full run and focused verification. The only warning
is the existing Starlette TestClient/httpx deprecation. JavaScript syntax checks
and `git diff --check` passed. All four MCP browser suites passed without page
errors, as did the three maintained Python browser smoke entrypoints.

## Launch and limitations

`data-refract-reference-final/` is a SQLite-backup snapshot of the original `data/`
workspace, with its vault key and installed extensions copied. Extension records
point to the copied files. It contains two groups, seven tasks, two accounts and
one profile. It had no automatic group starts, scheduled windows or scheduled
tasks. The original workspace is unchanged; edits in the new instance are separate.
Launch it with `python run.py --port 8787 --data-dir data-refract-reference-final`
using the repository virtual environment. The verified instance runs on
localhost:8787. An earlier preview remains on port 8786 because automatic approval
review rejected stopping/restarting that process ("blocked by policy"). The final
workspace was copied from that preview so its saved data is retained.

The new UI does not make unimplemented retailer adapters operational. Live checkout
correctness still depends on supported retailer layouts, authenticated sessions
and human verification where required. Real purchases and external provider
credentials were not exercised. No production-readiness or checkout-throughput
claim is inferred from a passing UI test or the overview benchmark.


## Closer reference layout (October 10 follow-up)

Matched the local reference's actual proportions: 70px icon-only desktop rail,
225px group cards with 66px initials, 290px task sidebar, compact table rows and
borderless row actions. The group title now sits inside the task pane; status
filters sit in the sidebar. The workspace fits the available desktop height,
including any trace-recording notice, with independent sidebar/table scrolling.
Mobile restores navigation labels and stacks the workspace without page overflow.

Shared colors, outlined controls, preference switches, dialog sizing, assignment
tabs, settings columns and dashboard metric cards now follow the reference more
closely. Proxy lists use cards backed by existing APIs and public host/port data;
credentials stay in the vault. Existing operations, four group settings tabs,
profile assignment, monitor controls, and additional navigation remain available.
No engine, persistence, authentication or purchasing logic changed.

Validation: all four Playwright MCP suites passed (15 task-workspace scenarios,
9 profile/settings scenarios, 5 assignment-recovery scenarios, and 6 workspace
scenario groups). Checks include simulated start/stop, independent task settings,
frozen runs, preview races, keyboard behavior and 1024/760/390px layouts. Added
an icon visibility assertion after hydration. A controlled proxy fixture also
passed card edit/delete and narrow-layout checks without external network tests.
Desktop, modal and mobile screenshots were visually inspected; artifacts use the
`closer-` prefix. JavaScript syntax and whitespace checks passed. The backend
regression suite was not repeated for this presentation-only follow-up.

The running instance on localhost:8787 serves these static assets after refresh;
its saved records were not modified during the visual review.
