# Task group stock monitors

Enter an Amazon ASIN or full product link in Monitoring, one product per line.
Links become uppercase ASINs on blur and on save. Invalid product input prevents
starting. Existing per-product price/offer suffixes remain supported to preserve
saved limits. Input lists also normalize links when saved.

Start a task (or Start all) to save the visible group settings and subscribe to
its monitors. Editing an input alone does not start a browser. Each task can
watch any group product or select one ASIN in its Product dropdown or task editor.
Stop the task before changing its assignment. Removing an assigned product makes
Start fail with an explanation until the task is reassigned.

Tasks in the same group, region, execution mode, product/offer and monitor
connection share a watcher. Simulation and live observations are isolated.
Each live watcher gets a fresh fingerprint-suite identity and an isolated,
anonymous context with no purchasing account cookies, credentials, or extensions.
No monitor proxy selected means the device connection; selecting an unusable
proxy group fails rather than silently switching to direct. The existing Node
fingerprint-suite dependencies (`npm ci`) and an app-managed browser are required
for live monitors; external CDP attachment is incompatible with that backend.

The task stays on standby without an account lock or checkout browser until a
monitor observes stock that meets its filters. Checkout then acquires the account,
prepares its saved session, and re-inspects stock and limits in that session.
Public availability is a signal, not a guarantee of account-specific availability.
Account exclusivity, purchase cooldowns, final checkout validation, and durable
order submission records remain enforced. Legacy Skip Monitoring no longer
bypasses this stock gate.

Each product's status button opens its latest 100 status changes. Logs update
while the popup is open. The application retains the last 100 completed monitor
runs in memory until restart. Last-subscriber stop closes the watcher; stopping
one task does not stop a monitor used by another. Challenges and explicit access
denials stop automatic monitoring. Transient errors and retailer cooldowns get
bounded retries without changing fingerprint or proxy.

Monitor inspection concurrency is bounded to 10 globally and the group's saved
monitor concurrency within each group. Up to 50 active monitor contexts are
allowed. These are separate from the configured checkout worker slots so stock
waiting cannot exhaust checkout capacity.

Validation covers shared subscriptions, direct networking, identity/session
isolation, task assignment, cancellation and lock ownership, removed inputs,
challenge handling, API validation, and the browser input/start/log workflow.
Real Chromium context checks use intercepted local product fixtures; no live
retailer purchase is needed for the tests.
