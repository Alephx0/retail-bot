# JavaScript fingerprint backend

Select the **JavaScript compatibility** backend to use the configured stock Chrome/Chromium executable. The optional native Chromium fork is not required. Each fingerprint flag remains an explicit setting; new browser contexts receive the selected account's seeded bootstrap.

## Canvas drawing profiles

Canvas perturbation applies small deterministic offsets to gradient coordinates and Bezier control points. It changes the rendered bitmap. Native `getImageData`, PNG/blob export, `drawImage`, `createImageBitmap`, and OffscreenCanvas transfer therefore observe the same pixels. Integer pixel writes, rectangles, circular/elliptical primitives, path endpoints and text metrics retain their original behavior. Numeric objects and other coercible inputs are delegated to native WebIDL conversion once.

This replaces the earlier canvas readback perturbation algorithm. Existing account seeds remain stable, but their canvas fingerprints change when upgrading. Two separately drawn canvases with the same seed and drawing commands remain repeatable. The profile does not modify every possible canvas drawing: a canvas containing only exact pixel writes can correctly remain unchanged.

## Callable compatibility

Modified methods remain JavaScript callable proxies. A local receiver adapter handles `toString` calls on non-callable objects directly inheriting a wrapper by constructing the native TypeError using an equivalent underlying-function prototype. It preserves repeatable property lookup, explicit call receivers and native error types. The shared `Function.prototype.toString`, Object/Reflect descriptor functions, and error stacks themselves are not rewritten.

The adapter is observable through sufficiently detailed reflection and does not establish universal native equivalence. The bootstrap marker is visible through normal reflection. Saved original methods and uninitialized realms can expose the underlying behavior. [CreepJS's callable checks](https://github.com/abrahamjuliot/creepjs/blob/master/src/lies/index.ts) are one finite set of observations, not a proof of indistinguishability.

## GPU coherence

Either GPU flag enables a shared identity policy. The WebGL renderer gets a seeded model alias within the observed hardware family, preserving its actual vendor, backend, capabilities, extensions and shader precision. WebGPU retains its real vendor and architecture, with device/description redacted. WebGL readback perturbation and WebGPU feature/limit restrictions remain controlled by their respective flags.

The old cross-vendor model selection and independently clamped WebGL capabilities could produce combinations absent from real hardware. Those transformations have been replaced. GPU identity values can change on upgrade even with the same account seed. Unsupported families and mobile/laptop GPUs preserve their original renderer identity; other enabled transformations still apply. This policy does not emulate another physical GPU or guarantee compatibility on untested hardware.

## Worker startup and restarts

When any graphics flag is enabled, each account context launches its own stock browser process. A loopback DevTools connection initializes dedicated, shared, service and nested workers before application scripts run. Instrumentation also initializes fresh service-worker globals after stop/restart, including module dependencies. Worker URLs, service-worker registration, fetch interception, caches and CSP remain intact. Account contexts use separate processes so one account's seed cannot be applied to another account's workers.

This propagation is automatic even when the worker checkbox is off. It costs an additional browser process per active graphics context and requires an app-managed browser; external CDP attachment with graphics profiles is rejected. Constructor interception is still available for standalone scripts and non-graphics settings, but has narrower coverage and does not initialize service workers. Merely copying `build_scripts()` into another application does not supply the managed worker integration.

Worker initialization failures are recorded and cause the affected target to close; navigation surfaces recorded failures. The strict audit treats them as failures. Local regression tests exercise classic/module workers, original script URLs, CSP, module dependencies, service-worker restart, fetch interception and concurrent account isolation.

## Remaining limits

GPU aliases and JavaScript proxies remain observable. Engine behavior, native GPU execution, network/TLS characteristics, IP reputation and site history are unchanged. A zero CreepJS warning/lie count does not establish an untraceable browser or retailer acceptance. Headless heuristic scores are reported separately and can remain nonzero.

Audio retains its conservative metadata fallback: valid native values and explicit sample rates are preserved. It does not perturb rendered audio.

## Reproduce the measurements

```powershell
.\.venv\Scripts\python scripts/fingerprint_differential.py --backend javascript --all-configurations --repeats 3 --sites creepjs --require-zero-warnings --output artifacts/javascript-audit
.\.venv\Scripts\python scripts/summarize_fingerprint_differential.py artifacts/javascript-audit
.\.venv\Scripts\python -m pytest -q
```

The matrix covers all 32 combinations of the five settings, plus headed, headless and restoration baselines. Use `--account-id` for another deterministic profile, and omit `--sites creepjs` to capture BrowserLeaks and AmIUnique too. `--require-zero-warnings` requires zero lies, zero warning-bin entries, zero captured errors, matching main/dedicated/CreepJS worker renderers and no worker initialization errors. Missing results fail the check. The narrower `--require-zero-lies` remains available. Original detector exports and screenshots are saved without suppressing findings.
