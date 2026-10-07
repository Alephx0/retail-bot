# US profiles on the current JavaScript backend

Under **Settings > Browser > Fingerprint profiles**, select **JavaScript compatibility mode**. The new font, navigator and screen switches are independent opt-ins; saved settings are not enabled automatically. They require a US account and an app-managed Chromium browser. They can be combined with the existing graphics/audio switches. Native and fingerprint-suite modes ignore these new options.

## What changes

| Setting | Behavior |
|---|---|
| Limit explicit local-font enumeration | `queryLocalFonts()` returns a deterministic subset of actual installed families, keeping core Windows families. The native permission check runs first. Returned FontData objects and their font bytes are genuine. |
| Vary CPU/memory and use the current Chrome browser identity | Main pages, iframes, dedicated/shared/service workers and restarted workers receive the same seeded values. CPU choices do not exceed the native count. Memory choices are 8, 16 or 32 GB, bounded by the native report; native reports below 8 are preserved. The owned account process uses the installed browser's User-Agent with `Chrome/` replacing `HeadlessChrome/`. |
| Vary desktop screen resolution and scaling | Chromium applies a seeded display/viewport/DPR combination, including CSS media queries. Viewport height is 120 CSS pixels below screen height. |
| Match US profile location to the selected proxy | Enabled by default when any new US surface is active and a proxy is selected. Resolves the proxy exit, applies its location, then checks the actual account context before returning it to the task. |
| US profile timezone | Manual fallback without a proxy, or when automatic proxy matching is explicitly disabled. |

Fonts inferred through rendering remain visible: CSS font selection, FontFace, glyph widths and font bytes are unchanged by the font switch. Existing canvas perturbation remains separately controlled by its own switch. CPU/memory properties do not change the physical machine. Navigator profiles retain the installed browser version, OS and native UA Client Hints, while replacing the headless marker in the User-Agent through Chromium's process launch option. This includes service workers and their restarts. The browser remains headless, and other automation indicators can remain. See the [APIVoid and input audit](behavior-input-improvements.md).

The screen presets are **1366×768 @1**, **1440×900 @1**, **1536×864 @1.25**, **1600×900 @1**, **1920×1080 @1**, **2048×1152 @1.25**, and **2560×1440 @1**. Sizes are CSS pixels. The same account seed chooses the same preset. Accounts can share a preset; uniqueness is not guaranteed. Fonts, CPU/memory and screen choices are independent of proxy city because an IP location does not identify those properties.

US profiles set both process language and context locale to `en-US`, aligning native document and worker language lists and HTTP Accept-Language. They own a browser process per context. Existing legacy profile records remain intact, so disabling these options restores the earlier context settings. Health checks use a separate observation baseline and preserve the selected screen dimensions when repairing viewport size.

## Proxy location and failure behavior

The effective task/account proxy is selected first, including an existing pool binding. A temporary Chromium context makes a fresh HTTPS request to `ipwho.is` **through that proxy**, with no account cookies or storage. The account context receives the same proxy configuration. It then performs a second lookup through its own route before task navigation. Neither lookup uses a direct HTTP client or a direct-network fallback.

Both responses must report the same public US exit IP and IANA timezone. Invalid JSON, lookup failures/rate limits, a non-US exit, an invalid/non-US timezone, unavailable account routes, or a changed exit stop setup. There is no gateway-hostname location cache. Subsequent contexts resolve again, allowing a deliberate move between US regions. Proxy credentials are not sent as API parameters or exposed in lookup errors. The lookup provider necessarily sees the proxy exit IP and browser request headers. The account health record retains city, region, timezone and check time, without exit IP or credentials.

The provider's IANA timezone identifier is passed directly to Chromium so daylight-saving rules remain browser-managed. Approximate IP coordinates are configured with 50 km accuracy. Sites still require ordinary geolocation permission; the application does not automatically grant it. These coordinates are an IP-derived estimate, not GPS. Provider behavior and fields are documented in the [ipwho.is API documentation](https://ipwhois.io/documentation). `tzdata` supplies IANA identifiers on Windows, where system zone data may be unavailable ([Python tzdata](https://pypi.org/project/tzdata/)).

Use a **sticky proxy session**. Two startup checks can catch rotation during setup but cannot pin a provider's exit, detect every mid-session change, or guarantee that a destination-dependent proxy uses the same exit for a retailer. Different IP databases can also disagree on city or region. Proxy reputation, TLS characteristics, WebRTC routing and behavior-based detection are not changed by this work. The implementation does not establish zero overall detections or retailer acceptance.

## Differential verification

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pytest -q tests
.\.venv\Scripts\python scripts/fingerprint_differential.py --backend javascript --us-profiles --repeats 3 --sites creepjs --require-zero-warnings --output artifacts/us-profile-audit
```

The US matrix contains 17 cases: disabled, legacy switches enabled, seven nonempty combinations of the three new switches alone, those same seven combinations with all legacy switches enabled, and restored-off. Three repetitions produce 51 sessions. This is not an exhaustive 256-combination test of all eight switches. Use a fresh output directory and `--account-id` to test additional seeds; use `--timezone` for a different manual timezone. The public audit uses synthetic accounts and no production proxies, saved logins or retailer pages.

Strict checks require complete CreepJS captures, zero lies/warning-bin entries/captured errors, matching GPU worker identity, and matching page/worker/iframe navigator properties plus screen/media-query consistency for US profiles. Legacy disabled cases retain an observed extra worker language fallback (`en-US, en`); this pre-existing baseline difference is recorded but exempted from the US-specific language assertion. Other findings are not suppressed.

`tests/test_proxy_location.py` additionally runs Chromium through an authenticated local HTTPS CONNECT proxy fixture. It checks both lookups use that route, location permission stays at prompt, granted coordinates match, main/iframe/worker timezone agrees, a new context resolves afresh, rotation and non-US responses stop setup, and disabling matching restores the manual timezone. The fixture serves synthetic IP-location responses and never forwards to retailers or an external proxy. Only the fixture test trusts its generated certificate; production TLS verification remains enabled.

Local run artifacts, screenshots and the measured report are under `artifacts/us-profile-2026-10-06/`. The new profile hooks run on stock Chrome; this implementation is not a modified native Chromium engine.
