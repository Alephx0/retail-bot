# Independent fingerprint-suite backend

Select **Apify fingerprint-suite (experimental)** in Settings > Browser > Fingerprint profiles to use Apify's generator and injector instead of the custom JavaScript hooks. JavaScript compatibility remains the default. The custom surface switches and US location settings are inactive in suite mode and retain their values for switching back. Suite mode applies a complete profile even when those switches are off.

Install the optional packages once from the project directory:

```powershell
npm ci --ignore-scripts
```

Node.js 20 or newer is required. Both `fingerprint-generator` and `fingerprint-injector` are pinned to **2.1.88**; transitive packages are locked by `package-lock.json`. Missing packages fail explicitly when suite mode is used. The other backends do not need Node. Package installation does not change production settings.

## Integration

`scripts/fingerprint_suite.cjs` runs the upstream generator and `newInjectedContext()` against an adapter that records its context options, filtered HTTP headers and initialization script. `retail/fingerprint_suite.py` applies those operations through Python/Patchright. The upstream injection payload is used without modification. Locale, native screen size and device scale are aligned with the generated profile. Account proxy and saved session options remain in Python and are never passed to the Node helper.

Generation is deterministic for a fixed account seed, locale and pinned package set. Only the short-lived Node helper's random-number source is seeded; browser `Math.random` is untouched. This also stabilizes the injector's generated history length. Package/data updates can change profiles. The helper has a timeout and is terminated on cancellation.

Generated profiles are constrained to desktop Chrome, the host operating system, and the account locale. Chrome version is supplied by the generator's dataset: the installed package could not generate an exact Chrome 154 profile during validation. Both actual browser version and generated version are recorded in audit artifacts. We do not rewrite the generated version to hide that discrepancy.

Suite mode requires an app-managed browser. It does not run custom canvas/GPU hooks, constructor wrappers, the native backend, or the managed worker bootstrap. The upstream document script references `window` and cannot simply be injected into a service-worker global. Consequently worker coverage must be measured as a separate result, not assumed. Profile health checks use the suite's viewport and a separate observation baseline so they do not reset it to the custom backend's settings.

## Differential comparison

```powershell
.\.venv\Scripts\python scripts/fingerprint_differential.py --backend fingerprint-suite --repeats 3 --sites creepjs --require-zero-warnings --output artifacts/suite-audit
.\.venv\Scripts\python scripts/fingerprint_differential.py --backend javascript --cases bot-off,everything,restored-off --repeats 3 --sites creepjs --require-zero-warnings --output artifacts/current-audit
```

Suite mode tests direct headed/headless baselines, disabled, `suite-full`, and restored sessions. `--all-configurations` is rejected for this backend because the custom five-switch matrix has no upstream equivalent. Use `--account-id` for additional fixed seeds. Omit `--sites creepjs` to include BrowserLeaks and AmIUnique.

Strict checks deliberately return exit code 1 for findings; a completed measurement is not the same as a passing fingerprint score. Exports retain all lies, warnings, captured errors, worker mismatches and headless ratings. Generated profile JSON and script digests are saved locally alongside screenshots. Results are specific to this Python/Patchright integration, browser, host and package version; they are not a universal ranking of the libraries.

Generate a side-by-side report, optionally including extra account seeds and a saved earlier audit:

```powershell
.\.venv\Scripts\python scripts/compare_fingerprint_backends.py --custom artifacts/current-audit --suite artifacts/suite-audit --previous artifacts/gpu-worker-repair-2026-10-05/final --output artifacts/suite-comparison
```

## Recorded comparison: 2026-10-06

On stock Chrome 154.0.8037.98, the fresh custom-backend audit matched the saved earlier probe values. Suite mode was tested with three account seeds, each repeated three times. The table covers enabled cases; disabled/restored cases remained clean.

| Measurement | Custom JavaScript | fingerprint-suite 2.1.88 |
|---|---:|---:|
| CreepJS lies per run | 0 | 5–6 |
| Warning-bin entries per run | 0 | 0–1 |
| Main/CreepJS worker GPU mismatch | None | Every enabled run |
| Like-headless rating | 38 | 25 |
| Headless rating | 67 | 33 |
| Stealth rating | 0 | 20 |

Suite findings included WebGL/WebGL2 callable recursion checks, navigator/worker disagreement and a worker platform-version inconsistency. One seed produced a suspicious-GPU warning. All three generated profiles advertised Chrome 147 while the runtime was Chrome 154. Repeated probe values and generated scripts were stable; disabling the backend restored baseline values. The functional repository suite passed 168 tests. These results support keeping the existing backend as default for this environment; the lower suite headless ratings do not establish lower overall detectability.

Upstream references: [fingerprint-suite](https://github.com/apify/fingerprint-suite), [injector implementation](https://github.com/apify/fingerprint-suite/blob/master/packages/fingerprint-injector/src/fingerprint-injector.ts), [generator implementation](https://github.com/apify/fingerprint-suite/blob/master/packages/fingerprint-generator/src/fingerprint-generator.ts).
