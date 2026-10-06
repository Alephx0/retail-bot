# Retail Desk

A local retail automation workspace inspired by the public Refract and Stellar workflows. Version 0.4 adds a Home dashboard, canonical resource folders, profile/account assignment previews, contextual tabs, account session management, structured task states, and independent monitoring/cart/checkout services. This is an independent implementation, **not full parity with either commercial bot**.

## Run

Browser automation uses [Patchright Python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) 1.63.0 with Chromium and its Playwright-compatible asynchronous API. Runtime, CDP inspection, recovery replay, and browser tests use the same library; there is no silent fallback to another driver. Existing account storage and checkout safeguards remain in place. This change does not guarantee retailer acceptance or successful checkout.

Python 3.11+ on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m patchright install chromium
.\.venv\Scripts\python run.py
```

Open **http://127.0.0.1:8765**. Subsequent launches can use `start.ps1`. Keep the process running for schedules. Use `run.py` rather than Uvicorn reload mode on Windows, so Patchright has a subprocess-capable event loop. Optional arguments: `--port 8766 --data-dir artifacts/test-workspace`.

## Implemented

| Area | Features |
|---|---|
| Retailer catalog | Amazon US, Best Buy US, NVIDIA, B&H Photo, Costco US, GameStop, Newegg, Pokemon Center, Walmart US, Sam's Club, Target. Retailer-scoped account/group/input setup is shared; Amazon is the first live adapter. Other checkout adapters are marked planned. |
| Accounts | Encrypted credentials, automatic task-start login, session persistence, groups, account proxies, personal/business labels, linked mailboxes and solvers, TOTP secrets, JSON import, browser-assisted Amazon registration. |
| Profiles | Contact, shipping, billing, validated payment-card numbers and masked display; JSON import. Amazon uses account defaults, not these profiles. Profile CVVs are not stored; an optional Amazon CVV belongs to its encrypted account record. |
| IMAP and codes | TLS connection test, read-only retrieval, recipient and sender-domain matching, arrival-time cutoff, OTP-specific parsing and UID replay protection. Local RFC 6238 TOTP generation and supported Amazon OTP field filling. |
| Solvers | Manual task-browser harvester queue; CapMonster, 2Captcha, Anti-Captcha and CapSolver balance tests and image-text adapters, connected to supported Amazon image challenges. |
| FlareSolverr | Local service connection test and explicit retailer request diagnostic. Requires a separately running service. Not an Amazon image solver or Target Shape integration. |
| Proxies | Encrypted and deduplicated lists, stable task assignment, separate monitor/checkout pools, concurrent per-retailer health checks, HTTP status and latency. |
| Tasks | Group/task CRUD, selection, batch controls, schedules, reusable multi-input lists, concurrent checks, price/offer/seller/condition/deal filters, bounded retries, account-group batch creation, global stop (including schedules) and cancellation. |
| Amazon checkout | Monitor only, cart plus review, or optional automatic order submission with strict checkout validation and a durable submission record preventing automatic resubmission. |
| Settings | Browser choice/timeouts, running-task limit, monitor defaults, proxy test limits, checkout and attention sounds with volume/style, Discord event preferences. |
| History | Product observations, task events, order confirmations and CSV export. Simulation is labeled throughout. |

## Amazon setup

1. Try **Load simulation -> Start all**. Simulation does not make Amazon requests or invoke paid solvers.
2. If needed, add an IMAP mailbox using your provider's host and app password. Test the TLS connection. OAuth-only mailboxes are not supported yet.
3. Add an account with Amazon US, email, optional password and authenticator secret. Link its mailbox and CAPTCHA provider. **Login** opens Take Control in the dashboard on that account's isolated headless page. Complete remaining verification there; a fresh account-page check saves the session automatically. **Save session** remains a manual fallback. Native OS/passkey dialogs require a separate visible browser and cannot be exposed from a headless process in place.
4. Set default shipping and payment methods on Amazon. Profiles do not override them.
5. Create a task group with ASINs or a reusable input list. Formats: `ASIN`, `ASIN;max_price`, `ASIN;offer_id`, `ASIN;max_price;offer_id`. Amazon product URLs and decimal caps are accepted.
6. Optionally assign separate monitor/checkout proxy pools. Test health against Amazon; this measures reachability and latency, not guaranteed retailer acceptance.
7. Create a task, or choose an account group to create one task per matching retailer account. Accounts with saved credentials can prepare their own session at task start. **Monitor only** never carts. **Review** opens the cart for manual checkout. **Automatic** may place a real order once started, when all checks pass.

Automatic mode requires Amazon US and a recognized checkout layout with exactly the target ASIN, verified quantity, unit price, allowed seller/condition, an order total within the group budget and a recognized place-order button. The engine writes its submission intent before clicking. An uncertain response is never retried automatically. A task with a submission record cannot restart live; review Amazon order history before intentionally creating a new purchasing task.

Unknown checkout layouts and mixed carts fall back to browser review. In review mode, budgets gate the observed item subtotal; **verify the final tax/shipping-inclusive total yourself**. Supported Amazon CVV forms can use the encrypted account CVV. Bank 3DS and unsupported verification layouts require interaction. Order confirmation is not proof of successful payment or fulfillment. When an observed payment prompt follows confirmation, the task stays open for approval and records payment verification separately. The order-attempt journal remains visible even if no confirmation was received.

Only one live task per account runs at a time; additional tasks queue and acquire the account session when the previous task finishes. Stopping closes the task browser but does not clear a server-side cart or cancel an order. Active tasks stop after an app restart unless their group has auto-start enabled; future schedules remain scheduled. Loop Checkouts is off by default. When enabled, verified successes may continue up to Maximum Checkouts Per Run. Uncertain submissions never loop or retry.

## Current limits

Amazon automation was validated with intercepted browser fixtures, **not a real signed-in account or purchase**. Conservative validation may require manual review when Amazon serves a different layout. No real mailbox credentials, proxy subscription, paid solver account or FlareSolverr instance was supplied; integration contracts were tested with mocks.

Still unimplemented: the other ten checkout adapters; Target Shape generation/extension integration; general token CAPTCHA injection; unattended account registration; mailbox OAuth; SMS retrieval; automatic bank 3DS; hidden-offer enumeration and direct Offer-ID carting; private deal feeds; coupon discovery; raffles; desktop packaging and licensing.

Offer IDs filter the visible offer. Deal filters apply to supplied products and observed item/reference prices; coupons, shipping, tax and membership eligibility are not inferred. Prime is a user-provided account label. Profiles are stored and assignable for future adapters; they do not change Amazon account defaults.

## Storage and operations

All record payloads in `data/retail.sqlite3` are Fernet-encrypted, including passwords, authenticator secrets, mailboxes, payment numbers, proxy credentials and browser state. On Windows the encryption key uses the current user's DPAPI protection. Other platforms use a key file with mode 0600. Back up the data directory together with the appropriate Windows user keys.

Credentials are redacted from dashboard state. Codes and email bodies are not written to activity logs. Optional Amazon account CVVs are encrypted with the account and redacted from API responses. The loopback server checks Host and write Origin and requires a dashboard request header for writes; do not expose it to the internet.

Sounds require an open dashboard and user interaction to unlock browser audio. Browser settings reconnect after saving when tasks are stopped. Paid providers may charge for challenges encountered by assigned live tasks. Simulation does not invoke providers or send Discord messages.

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

Tests cover parsing, filtering, persistence, request protection, secret redaction, references, scheduling, cancellation, TOTP reference vectors, OTP scoping/replay prevention, provider/proxy adapters, checkout-layout validation and uncertain submission responses.

For a live differential fingerprint audit on Windows with Google Chrome installed:

```powershell
.\.venv\Scripts\python scripts/fingerprint_differential.py --output artifacts/fingerprint-audit-1
.\.venv\Scripts\python scripts/summarize_fingerprint_differential.py artifacts/fingerprint-audit-1
```

Use a fresh output directory for each audit. The audit visits CreepJS, BrowserLeaks, and AmIUnique in three fresh sessions per configuration, using an isolated synthetic account and fixed seed. It compares direct Chrome headed/headless launches, the bot with all transformations off, each transformation independently, everything enabled, and a final disabled run. Direct Chrome baselines are inspected over CDP, not uninstrumented manual browsing. It saves JSON exports, screenshots, and a differential report; these contain browser/device fingerprints. No retailer account or production settings are used. Audio metadata may legitimately remain unchanged; workers-only tests interception without enabling other transformations. Results measure browser consistency, not retailer acceptance.

Add `--sites creepjs --all-configurations --require-zero-warnings` for the strict CreepJS regression check. It requires zero lies, warning-bin entries and captured errors, matching main/worker renderers, and successful worker initialization; missing captures fail. Results are saved in `zero-warnings-check.json`. The narrower `--require-zero-lies` checks only capture validity and lies. Neither check suppresses findings. Canvas uses seeded gradient/Bezier drawing offsets, preserving exact pixel writes and native read/copy/export behavior. Callable proxies use a local invalid-receiver adapter while the global Function.prototype.toString remains native.

See [JavaScript fingerprint backend behavior and limits](docs/javascript-fingerprint-backend.md) for the drawing algorithm, callable receiver adapter, and cross-realm coverage.

The current JavaScript backend also supports [US font, navigator, screen and proxy-location profiles](docs/us-fingerprint-profiles.md). Enable the three new surface switches under Settings > Browser > Fingerprint profiles. Explicit font enumeration is restricted without changing rendering, CPU/memory choices repeat per account, and seven screen/scale presets are available. With proxy matching enabled (the default for these US profiles), two browser-routed checks verify a stable US exit and apply its IANA timezone and approximate geolocation. Location permission is not granted automatically. A lookup failure, non-US exit or exit change during setup stops the session. Use `--us-profiles --sites creepjs --require-zero-warnings` for the new differential matrix.

An independent [Apify fingerprint-suite backend](docs/fingerprint-suite-backend.md) is available for comparison. Install its optional pinned Node packages with `npm ci --ignore-scripts`, then select it in Fingerprint profiles. It applies a complete generated profile instead of the custom transformations; no injectors are stacked. Use `--backend fingerprint-suite` with the audit script. JavaScript compatibility remains the default, and suite worker/engine inconsistencies remain visible in the results.

Graphics compatibility: WebGPU adapters, devices, metadata, limits and feature sets retain native object identities. `GPU.requestAdapter` and Object/Reflect intrinsics stay native. Device-request iterables are consumed once in native dictionary-conversion order. JavaScript graphics profiles automatically initialize dedicated/shared/service/nested workers before startup, including service-worker restarts. They use a separate stock browser process per account context and require app-managed browsers. These repairs do not make the hooks undetectable: saved native accessors and inspection of drawing hooks can still expose differences. The bootstrap's idempotency symbol remains visible through normal reflection.

An optional [native fingerprint backend](docs/native-fingerprint-backend.md) moves graphics transformations into a pinned Chromium fork and gives each active graphics profile its own process. Install it with `scripts/install_native_browser.py`, then select Native Chromium profiles in Settings → Browser → Fingerprint profiles. The build disables Safe Browsing; review the linked tradeoffs before selecting it. Add `--backend native --all-configurations` to the audit command to test all 32 flag combinations. The JavaScript backend remains the default until explicitly changed.

WebGL profiles retain seeded model aliases within the real hardware family and pixel transformations. Vendor, capabilities, extensions and precision remain native. Either GPU flag applies coherent identity policy across both APIs; WebGPU vendor/architecture remain genuine and model fields are redacted. Unknown/mobile GPU families keep native renderer identity. Equivalent enum arguments use the same transformed path with one native conversion. This changes earlier profile values and avoids arbitrary cross-vendor claims; it does not emulate another GPU or alter the network stack.

For dashboard tests, start an isolated workspace:

```powershell
.\.venv\Scripts\python run.py --port 8766 --data-dir artifacts/test-workspace
```

Then in another terminal:

```powershell
$env:RETAIL_TEST_URL='http://127.0.0.1:8766'
.\.venv\Scripts\python scripts/smoke_ui.py
.\.venv\Scripts\python scripts/smoke_features.py
```

These create labeled fixtures in the isolated workspace and screenshots in `artifacts/`. No external services or purchases are used.

## References

- [Refract Amazon setup](https://help.refractbot.com/modules/amazon/amazon-setup-guide)
- [Refract multi-input monitoring](https://help.refractbot.com/general-setup/task-creation/monitor-setup-and-multi-input)
- [Stellar setup](https://guides.stellaraio.com/stellar)
- [Stellar AmazonV3](https://guides.stellaraio.com/stellar/retailers/amazonv3)
- [Stellar IMAP](https://guides.stellaraio.com/stellar/navigating-stellaraio/what-is-the-identities-tab/imap-overview)
- [Stellar Shape](https://guides.stellaraio.com/stellar/retailers/shape)
- [CapMonster image-text API](https://docs.capmonster.cloud/docs/captchas/ImageToText/image-to-text/)
- [2Captcha image-text API](https://2captcha.com/api-docs/normal-captcha)
- [Anti-Captcha image-text API](https://anti-captcha.com/apidoc/task-types/ImageToTextTask)
- [CapSolver image-text API](https://docs.capsolver.com/en/guide/recognition/ImageToTextTask/)
- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)

Public documentation informed the workflows. No proprietary code, private feeds or licensed backends from either commercial product are included.

## Compact workspace and group schedules

Create a group with a name and site, then configure its monitor in the scrolling left panel. Double-click a group to reopen it. The default monitor interval is 4500 ms. Product URLs are shortened in task rows; hover or open task details for the complete input. Statistics filter tasks without deleting them; Show all tasks clears the filter.

Account creation supports Single Input and Mass Input. Paste `login:password`, optionally `login:password;proxy;secret`, or on Amazon `login:password;proxy;secret;cvv`. Imports validate all lines before saving. Proxy lists and direct proxy input are optional. An account proxy is used for login; Use Account Proxy also applies it to task browsing. IMAP, solver, and account-group fields are under Account group & verification.

Tasks support per-account task counts, item quantities, retry delay, free-shipping verification, and browser focus for payment approval. Live tasks on the same account queue automatically to prevent shared-cart conflicts. Headless Chromium is the default; View live is read-only, while Take Control sends input to the same page when a task is paused. A visible-from-start browser remains optional in Settings for OS-level verification. Amazon CVV automation only acts on recognized Amazon-hosted verification forms and never bank forms or Place Order buttons. Unknown layouts need manual review.

Group schedules use the engine device's local timezone. Selected weekdays repeat weekly; with no selected days each slot runs once at its next start. A stop time earlier than or equal to start stops the following day. Occurrences are persisted before starting tasks, so polling and restart do not resubmit a slot. Overlapping slots keep the group active until the last slot ends. The engine must remain running. Changing a schedule establishes new occurrences.

Monitor delay can be changed while tasks run. Access-denied responses pause for user review rather than rotating identity or retrying through an explicit block; ambiguous cart or order results never auto-retry. OOS is treated as unavailable stock, not assumed to be a soft ban.

## Headless workers and intervention

Configured accounts do not keep browser contexts open while idle. Live tasks use isolated account contexts in a shared headless Chromium process, or separate processes when graphics profiles are enabled, with encrypted cookies, local storage and bounded account-scoped session-storage snapshots restored on the next task. A configurable worker limit queues excess tasks before they open contexts; one live task per account holds the account lock. Account sign-in sessions are capped and expire after 30 minutes if unfinished.

When a task pauses for authentication or verification, open **Take Control** on that task. Click the screenshot and type, paste or use the key controls; the input goes to the same paused Playwright page. Then press **Resume**. The app verifies sign-in and re-inspects the product and cart before retrying the interrupted pre-submission step. Input is rejected while automation is running, so the user and bot cannot click the page concurrently. Order submissions with an existing attempt record are never replayed. Closing Take Control returns to headless operation because no GUI browser was launched.

Native passkey/OS dialogs cannot be shown through a headless screenshot. For accounts that require those, select a visible browser before starting work; changing browser mode requires tasks to be stopped and creates a new browser process, so in-memory page state cannot be preserved across that switch. CAPTCHA and access-denied pages are surfaced for legitimate user handling, not bypassed.

Reference behavior reviewed: [Refract monitor inputs](https://help.refractbot.com/general-setup/task-creation/monitor-setup-and-multi-input), [delays](https://help.refractbot.com/general-setup/task-creation/delays), [Amazon setup](https://help.refractbot.com/modules/amazon/amazon-setup-guide), and [Stellar guides](https://guides.stellaraio.com/stellar). Retailer adapters beyond Amazon, including Walmart queues and Target Shape, remain planned. Live retailer compatibility has not been validated with a real purchase.

Run `python -m pytest -q` for fixture tests. `scripts/smoke_workspace.py` targets an isolated test server on port 8766 and creates simulation fixtures; do not point it at a production workspace.


## Version 0.4 architecture and workflows

The eight sidebar destinations are Home, Task Groups, Accounts, Profiles, Proxies, Input Lists, Account Manager, and Settings. IMAP, solvers, retailer status, observations, and logs are contextual Settings destinations. Profiles use General/Shipping/Billing/Payment tabs. Task groups use General/Monitoring/Checkout/Advanced tabs.

`retail/resources.py` owns canonical resource memberships and account/profile relationships. `All` is a virtual view. Existing named account/profile groups migrate once without copying resources. Create New inside a folder adds membership; Import Existing adds references to existing records. Deleting a folder preserves its resources.

Task creation previews every assignment before committing. Sequential and seeded random distribution cycle through the selected resources; one-to-one requires matching group sizes. Match Accounts to Profiles is enabled by default. A unique saved relationship for the selected retailer takes priority; otherwise a unique matching email assigns the account automatically. Email comparison ignores letter case and surrounding whitespace, but preserves dots and plus aliases. Names and shared folder labels are not identity evidence. The preview shows the matching reason and blocks missing or ambiguous matches. Turn matching off to choose an account or account group manually. Inferred matches do not create permanent relationships. The server recomputes and compares the submitted preview, rejecting stale or ambiguous assignments.

`retail/adapters.py` defines the common contract and independent MonitorService, CartService, and CheckoutService. MonitorEvent is the normalized observation boundary. Partial product failures no longer discard other successful observations. `retail/engine.py` owns lifecycle, scheduling, account locks, events and browser handoffs; `retail/runner.py` coordinates stages. `retail/task_state.py` defines states and validates progression. Amazon remains the only live adapter; changing these boundaries does not implement the other retailers.

`retail/interactions.py` resolves semantic operations through accessibility roles/labels, then stable attributes. Ambiguous controls require review. Carting now enforces exact quantity and verifies the cart result. Checkout still requires recognizable product, seller, condition, quantity, price and total evidence; unfamiliar checkout layouts require manual review. Skip Monitoring requires ASIN plus Offer ID and skips monitor fan-out, but still validates a visible offer before carting. It does not enumerate hidden offers or directly submit an Offer ID to undocumented endpoints.

Home analytics derive spending/checkouts/savings from order records and failures from terminal checkout failure events. Pending verification and ambiguous results are not successful orders. Unknown totals are excluded from spending and disclosed. Currencies and simulations are separate. Legacy orders without account/profile/reference-price metadata show unavailable fields rather than fabricated values.

Proxy endpoints are normalized and deduplicated beneath the existing pools. Resource folders organize those pools; the Connections table shows individual host, port, protocol, health, latency and test time. New routes prefer a healthy tested connection. Existing account routes stay sticky; a health test does not silently move a logged-in session. HTTP proxies are supported; SOCKS and per-endpoint editing are not implemented.

Account Manager provides explicit open/verify actions, group/profile/network assignment, and saved session state. Refresh Status refreshes stored state; Verify Login makes the browser check. Address/payment health audits and scheduled account browsing are not implemented. Sessions preserve encrypted cookies/storage; optional fingerprint profiles apply the transformations documented above.

Diagnostics store masked screenshots, semantic control snapshots, accessibility output, stage and previous locator encrypted locally. Optional browser traces are also stored encrypted and downloaded from Settings. Traces can contain account details. The app connects its local MCP tools to the Playwright task page, using CDP for accessibility inspection. Attaching to an existing Chrome or Edge debugging session is optional under Settings > Browser > Advanced.

### AI browser assistance

1. Open **Settings > Integrations** and choose **Add OpenAI API key**. Get a key from [OpenAI API keys](https://platform.openai.com/api-keys), paste it, and select a model. GPT-6 Sol is the starting choice; Luna costs less per token, while Astra is the most capable. The model list reflects the [OpenAI model catalog](https://developers.openai.com/api/docs/models) as of September 2026. Model access depends on your OpenAI account. Other compatible API services are available under Advanced in the connection editor.
2. Save the connection. A first connection is selected automatically with **Automatically recover checkout navigation** enabled. Click **Test connection** to check its key, model, and tool calling. Then click **Test browser recovery** to check the model, actual MCP tools and CDP on an isolated sample page (API charges may apply; no Amazon account or purchase is used). You can choose **At each cart and checkout step** for more model involvement or **Off**. The app starts local MCP browser tools itself, so there is no MCP server, CDP endpoint, or local AI service to install for this path.
3. Create or run an Amazon task as usual. AI may select cart and checkout controls. The adapter checks the product, quantity, seller, condition, final order total, and submission journal before a single submission. The model never writes scripts or changes these checks. Unknown layouts pause for review. Model calls may incur API charges.

Live checkout tasks now show specific filter failures instead of a generic stock loop. Seller detection supports Amazon's `Shipper / Seller` label as well as `Sold by`. If a product-page seller is unreadable, a checkout task may add the item to the cart and attempt checkout, but the seller must still be verified on the checkout page before an order is submitted. Known disallowed sellers remain blocked. Monitor-only tasks continue to require the seller filter before reporting an eligible offer. Recent AI activity and task setup instructions appear in Settings > Integrations; recovery mode does not call the API when standard controls already work.

For an existing dedicated browser only, start Chrome with `--remote-debugging-port=9222` and a separate `--user-data-dir`, then enable **Use existing Chromium debugging connection** under Settings > Browser > Advanced. The endpoint must stay on localhost. Normal operation uses the app-managed browser; CDP still supplements Playwright internally.

The [OpenAI Responses API](https://developers.openai.com/api/docs/guides/function-calling) supplies tool calls. The MCP SDK connects the task's restricted browser tools in memory. Provider requests contain button/link names and a URL without query parameters. They do not include passwords, cookies, screenshots, or payment fields. Saved API keys are encrypted and never returned to the dashboard.

Task startup now opens the first product directly, verifies the saved session there, and reuses that page for its initial observation. Separate account login management may still open Your Orders. Checkout navigation supports up to five intermediate steps, including the `/checkout/byg` recommendations page and explicit refusal of optional modal offers. Recovery can select a validated checkout continuation link; it cannot add recommendations, accept paid offers, or submit an order during navigation. Unsupported final review layouts still pause: AI cannot invent missing item, quantity, seller, condition or price evidence.

Before adding a product, the live adapter inspects the active cart. It reuses an exact matching target, adjusts a supported native quantity control when possible, and rejects unrelated cart items rather than silently ordering them. The current Amazon SPC review can omit ASIN attributes; the adapter can instead bind the exact cart title to the already verified ASIN, then cross-check the review quantity, item count, seller, unit price and total. A saved product condition must also be available. Identical duplicate order controls are treated as one semantic action after review validation. Successful AI control repairs are stored as encrypted data and replayed only after live validation on a matching page. The app never rewrites source files during checkout; unknown layouts without enough verifiable facts still pause.

### Legacy local diagnosis endpoint

This remains optional under Settings > Integrations > Advanced AI settings. A saved AI connection can diagnose failures directly. To use the older local service instead, configure `diagnosis_endpoint` to accept a POST JSON object with `instruction`, `failure`, `expected_action`, and `controls`. It must return:

```json
{"probable_cause":"Button label changed","action":"ADD_TO_CART","method":"role","locator":"Add to Cart","confidence":0.8}
```

Allowed actions are ADD_TO_CART, BEGIN_CHECKOUT and SUBMIT_ORDER; methods are role, label and css. Diagnosis responses are validated as data, never executed as code. Validate Offline checks candidate uniqueness on a synthetic replay with all network requests blocked. This is not proof of checkout correctness: candidates are not auto-promoted. A developer must run adapter regression tests and review a patch before adoption.

### Verification and remaining work

Account consistency: supported locale, viewport, screen and scale settings are stored per account in the encrypted vault. Verified login snapshots preserve genuine cookies, localStorage and IndexedDB; bounded sessionStorage is restored separately. Post-login health checks report observed browser drift and correct viewport size only, without modifying navigator or WebGL identities. Edit an account to see its last health result.

Account editors offer an optional 2–7 day purchase cooldown (default Off for compatibility). It uses this app's recorded live orders, including orders pending payment verification; it cannot see outside purchases. The account lock protects the check before browser allocation. A blocked task stops with an eligible UTC timestamp and must be started again later. Successful loop tasks end after one order when cooldown is enabled. Simulation, monitoring and quote tasks are exempt. This policy is an explicit purchase limit, not a way to disguise order activity.

The 0.4 acceptance script is `scripts/smoke_redesign.py` against an isolated server on port 8766. The contextual integration smoke check is `scripts/smoke_features.py`. Neither places purchases.

A read-only public Amazon check on 2026-09-25 received a Continue shopping interstitial before product content. That condition now produces an explicit manual-action handoff instead of an unsupported-layout error. Live signed-in monitoring, carting and checkout remain unverified. Optional fingerprint profiles do not establish retailer acceptance; fabricated trust activity and bulk account registration are not implemented.

References for the architecture: [Playwright locators](https://playwright.dev/python/docs/locators), [CDP connection limitations](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp), and [trace capture](https://playwright.dev/python/docs/trace-viewer). The linked Reddit discussion and browser signal demonstration pages were reviewed; they do not establish that an automation stack is undetectable.
