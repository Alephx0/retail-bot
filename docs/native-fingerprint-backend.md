# Native fingerprint backend

The optional native backend uses [Anti-Fingerprint Browser](https://github.com/pppi21/anti-fingerprint-browser), a separately distributed Chromium fork, version `153.0.8010.47-1`. Graphics transformations run in the browser engine. The JavaScript compatibility backend remains available.

Install the pinned portable build on Windows:

```powershell
.\.venv\Scripts\python scripts/install_native_browser.py
```

The installer verifies the release ZIP's pinned SHA-256 before extraction and places the executable under `browser-data/native-chromium-153.0.8010.47-1/`. It does not register a system browser or change application settings. An existing ZIP can be supplied with `--archive PATH`.

In **Settings → Browser → Fingerprint profiles**, select **Native Chromium profiles**. Leave the executable field blank to use the installed build. Stop active tasks before changing backends. External CDP attachment cannot be combined with native profile configuration because the app must supply profile switches when it launches the process.

**Security and compatibility tradeoffs:** upstream disables Google Safe Browsing even with `--use-chromium-defaults`. It also modifies V8 inspector behavior, including console-message forwarding and bindings; normal CDP error observations may therefore be incomplete. This is a third-party browser, and passing a fingerprint test does not establish its security, complete web compatibility, or retailer acceptance. The source is BSD-3-Clause; the executable and bundled resources are optional downloads, not repository source. [Upstream documentation](https://github.com/pppi21/anti-fingerprint-browser#security-tradeoff-safe-browsing-is-disabled)

## Profile behavior

- Canvas changes affect rendering operations, so native readback, image copying, and exports observe the rendered bitmap. The engine algorithm differs from the JavaScript backend's gradient/Bezier drawing offsets.
- Enabling either GPU identity option applies one identity to **both WebGL and WebGPU**, including dedicated, shared, and service workers. WebGL shader perturbation is enabled only by the WebGL option. This coupling prevents contradictory identities across APIs.
- Worker propagation is automatic in native mode; the worker checkbox controls constructor interception only in JavaScript mode.
- Audio retains the existing JavaScript fallback policy: valid native values and explicitly requested sample rates are preserved. The native build's audio override is deliberately unused because it overrides page-requested sample rates.
- Each graphics-enabled context owns a separate browser process with its account seed. Closing the context closes that process. Profiles never share one process's seed across accounts. Expect more memory use than the shared stock-browser mode.
- Settings with no graphics transformations use an unseeded instance of the same native build. Missing native executables raise an error; there is no silent fallback to JavaScript mode.

The upstream rendering policy includes size/backend thresholds, including preservation of small canonical canvas shapes. Its source explicitly discusses CreepJS checks. Zero measured lies should therefore be interpreted as a result on that benchmark and the independent probes, not proof that the browser reproduces all physical GPU behavior or is indistinguishable from stock Chrome.

## Differential validation

Run all 32 combinations of the five flags, plus headed/headless and restoration baselines, three times each:

```powershell
.\.venv\Scripts\python scripts/fingerprint_differential.py --backend native --all-configurations --repeats 3 --sites creepjs --require-zero-lies --output artifacts/native-audit
.\.venv\Scripts\python scripts/summarize_fingerprint_differential.py artifacts/native-audit
```

Use `--account-id` to exercise another deterministic seed, and omit `--sites creepjs` to include BrowserLeaks and AmIUnique. Native audit baselines use the same fork with transformations disabled. Stock Chrome results must be compared separately rather than treating different browser versions as identical baselines.

Keep lie counts, CreepJS's warning bin, its internally captured errors, transformation deltas, worker agreement, and headless heuristics separate. A zero lie counter does not imply every heuristic is zero. The tests also check genuine API receivers, exact pixel writes, copying, repeatability, account isolation, and browser-process cleanup.
