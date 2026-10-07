# Settings

Settings has five sections. The Save changes and Discard changes controls remain
visible while scrolling. Saving applies the changed fields across all sections.

| Section | Controls |
|---|---|
| General | New-group monitoring interval, browser worker limit, advanced proxy-check limits, retailer status link |
| Browser | Managed or existing browser, visible or headless window mode, installed browser choices, page timeout, advanced options |
| Notifications | App sounds, percentage volume, Discord preferences and explicit webhook management |
| Connections | API credentials, connection tests, opt-in AI assistance, advanced limits, email and solver links |
| Data & privacy | Encrypted backup download, restore instructions, imports, order-history link and diagnostic recording |

Troubleshooting is a separate destination for logs, monitor observations, local
browser-recovery tests, recent AI activity and diagnostic evidence.

## Defaults and when changes apply

New workspaces use a **visible browser**. To run without a desktop window, select
Browser → Window mode → Headless. Existing saved window-mode choices are retained.
This is a visibility preference, not a detector-acceptance guarantee. Advanced
profile transformations remain opt-in; this redesign does not tune them for evasion.

Browser connection, window mode, browser application, AI connection/mode and worker
limit changes require stopped tasks. The dashboard locks these controls while
tasks run, and the API independently enforces the restriction. Browser settings
apply to new task sessions; an existing external browser controls its own window mode.
Account sign-in and registration use a separate visible managed browser regardless
of task window mode. They do not open a dashboard Take Control dialog.
The default monitoring interval affects newly created groups only. Time fields
display seconds, while the API retains its existing millisecond fields.

## Editing and saving

- Drafts survive section changes, navigation to other application pages, connection
  tests and connection editing. They are held only in memory, not browser storage.
- Reloading or closing the dashboard with a draft prompts the browser's unsaved-
  changes warning. Drafts are not recoverable after a forced reload or crash.
- Validation errors remain visible and reveal the affected section, including
  fields inside collapsed advanced controls. Save failures retain the draft.
- Save sends only changed fields. If another window has changed one of those
  fields, the API rejects the stale edit. Discard reloads the saved configuration.
- API connection edits save independently and are labeled accordingly. Adding a
  key does **not** select a connection or enable assistance. Choose a connection,
  select when AI may assist, then Save changes. Tests can incur provider charges.
- An empty untouched secret preserves its saved value. Discord has explicit
  Add/Replace/Remove choices; removal also turns off Discord notifications. Sending
  a test message is an explicit action against the **saved** webhook.

## Backup and restoration

Download an encrypted backup before changing workspace storage. It contains
`retail.sqlite3` and `vault.key`; it is distinct from record imports and CSV exports.
On Windows, the vault key is tied to the Windows account that created it.

To restore, stop tasks and close the application, retain the current data folder,
and extract the backup into a **new empty folder**. Keep both files together. Start
the application using its virtual environment with:

```powershell
.\.venv\Scripts\python run.py --data-dir "C:\path\to\restored-workspace"
```

Use the same Windows account. Restored scheduled groups can resume scheduled work.
This is an offline restore workflow; the dashboard does not overwrite a running
workspace. Browser binaries and other files outside the database/key are not part
of the backup.

## Validation

`tests/test_settings.py` exercises a real browser against an isolated local test
application, with provider responses and browser availability supplied by fixtures.
It covers draft retention, explicit AI enablement, hidden-field validation, units,
window-mode persistence, webhook replacement/removal, keyboard tabs and narrow
layouts. API tests cover partial saves, secret preservation, stale edits, runtime
locks and an explicit mocked Discord test. No production accounts, providers or
retailer sites are used by these tests. Screenshots are saved under
`artifacts/settings-redesign/`.

The final affected regression set passed **50 tests** with no failures. JavaScript
syntax checks and `git diff --check` passed. Desktop screenshots and 390px-wide
layouts were reviewed; the browser test asserts no horizontal document overflow
across all five sections. The test report is
`artifacts/settings-redesign/pytest.xml`.
