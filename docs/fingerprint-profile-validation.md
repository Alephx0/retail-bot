# Account fingerprint validation — 2026-10-08

The accepted OverpoweredJS result is **0.20 Low with all eight surface toggles
enabled and GPU variation active**, measured in both modes. The user accepted
0.20 and withdrew the earlier 0.10 target on October 8. Fingerprint-scan fell from
20/100 to 5/100 with genuine Google Docs Offline enabled, but its **Browser**
category still reads **Medium**. No detector response, score or displayed result
was replaced by the app.

## Follow-up: bundled Chromium versus installed Chrome

The user's 0.70 Elevated JavaScript result was reproduced on October 8. The app
was configured for Bundled Chromium 153, while the earlier 0.20 measurements used
installed Google Chrome 154. With the same account seed, all eight toggles,
incognito mode and an RTX 3060 Ti alias of the physical RTX 3070:

| JavaScript browser | Visits | Bot score | CreepJS lies / warnings / errors |
| --- | --- | --- | --- |
| Bundled Chromium 153 | 2 | 0.70 Elevated both times | 0 / 0 / 0 |
| Installed Chrome 154 | 2 | 0.20 Low both times | 0 / 0 / 0 |
| Installed Chrome 154 with the account's four selected extensions | 3 | 0.20 Low all three times | 0 / 0 / 0 |

The displayed automated-browser, anti-detect, user-agent and developer-tools
checks were clear in every run. This isolates the observed difference to the
browser-build selection in these tests, not to a known private scoring rule.
Version, brand and other build capabilities differ together. No claim is made
that changing only the reported brand would reproduce the result.

The local app's Browser application was changed to installed Chrome. Surface
toggles, generated presets and extension assignments were retained. Account and
global help now distinguish the browser application from profile implementation.
Evidence: `user-chromium/`, `user-chrome/` and `user-chrome-extensions/` under
`artifacts/overpowered-investigation/`. Diagnostic profiles contained no saved
account credentials or login sessions.

## GPU variation enabled: subsequent repair

The latest repair measures **0.20 Low with GPU aliasing and WebGL variation
active**, without selecting Preserve actual GPU. The latest user-supplied
comparison identifies **Google Chrome / Windows 11 / chromium engine /
incognito**, with a residential US connection. This supersedes the earlier
Firefox metadata and the description of normal mode. The user reports 0.10 in
that Chrome configuration. Browser version and GPU renderer were not included.
**The 0.20 result is accepted; further work toward 0.10 is no longer requested.**

| Configuration | Before | After | Repeats |
| --- | --- | --- | --- |
| JavaScript, all eight toggles, RTX 3070 Ti alias, standard variation | 0.99 High | 0.20 Low | Three fresh profiles, three visits each |
| Native, all eight toggles, RTX 3070 Ti alias, standard variation | 0.97 High | 0.20 Low | Three fresh profiles, three visits each |
| JavaScript, all eight toggles, default generated GPU | ? | 0.20 Low | One fresh profile |
| Native, all eight toggles, default generated GPU | ? | 0.20 Low | One fresh profile |

All 18 repeated visits scored 0.20. All six repeated-profile CreepJS captures had
zero lies, warnings and captured errors. The physical GPU is RTX 3070; the repeated
cases reported RTX 3070 Ti with its corresponding PCI device ID. CreepJS's actual
WebGL pixel and canvas-export fingerprints changed with native variation active.
Local WebGL1/WebGL2 tests also prove that JavaScript output changes, repeats for
the same seed, changes with a different seed, and agrees across readPixels,
canvas copies, cropped reads and WebGL2 pixel-buffer reads. This is actual drawing
variation, not an enabled checkbox with a no-op implementation.

Changes:

- GPU aliases now retain the renderer's PCI-ID field and use verified NVIDIA
  model/device pairs from [NVIDIA's published device table](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/README.md).
  Automatic selection chooses a different compatible family model when available.
  Explicit selection of the physical model retains its original device ID.
- JavaScript WebGL now changes interpolated fragment colors before raster output.
  Native pixel-read APIs observe the resulting framebuffer unchanged. Analytic
  shaders, texture copies, vertex shaders, integer outputs, multiple render
  targets and complex preprocessor programs are left alone. This limited policy
  avoids breaking exact drawing operations; it is not full GPU emulation.
- Native WebGL uses the engine's seeded color-output variation while retaining
  native analytic math, texture sampling and fragment coordinates. The standard
  variation switch remains nonzero; the native coefficient policy selects the
  color-output surface. Subtle and Off choices remain available.
- **Vary GPU** in Generated fingerprint restores the automatic GPU, standard
  WebGL variation and compatible WebGPU capabilities. Surface toggles stay as
  selected. Save and reopen the browser to apply. Existing accounts that selected
  Preserve actual GPU can switch back with this button.

The intermediate corrected-ID native case scored 0.84 with the original broad
shader-noise policy. JavaScript's first drawing-based repair also scored 0.84
when it altered analytic shaders. Restricting drawing variation to interpolated
colors brought both to 0.20. All displayed automated/anti-detect/UA/devtools checks
were clear after the final repair. The score is still an external server result;
matching model IDs and API consistency do not prove complete physical emulation.

The full regression suite passed **229 tests**. After the final shader compiler
change, all **41 affected regression tests** passed again. The compiler transform
leaves shaderSource, getShaderSource and pixel-read methods native; it compiles
valid caller source first, transforms supported drawing programs, restores the
caller-visible source, and falls back to the original compilation on failure.
Three additional JavaScript live-site runs remained 0.20 with zero CreepJS lies,
warnings and captured errors. Eight-core CPU and 1080p screen combinations also
remained 0.20 in the controlled comparisons. Evidence: `varied-regression.xml`, `varied-repeat/`,
`generated-defaults/`, `varied-correct-id/`, `varied-color-noise/`,
`js-interpolated-color/`, `varied-final/`, `varied-final.xml`, and
`desktop-combinations/` under `artifacts/overpowered-investigation/`.

```powershell
.\.venv\Scripts\python scripts/overpowered_audit.py --output artifacts/varied-gpu-audit --cases js-varied-gpu,native-varied-gpu --creepjs --repeats 3 --visits 3
```

A follow-up Chrome baseline used a fresh user-data directory with **no debugging
port or browser-control attachment**. Its genuine API response, captured with
[Chromium NetLog](https://www.chromium.org/for-testers/providing-network-details/),
was 0.36 Watch with an empty anomaly array. This did not reproduce the user's
0.10 and does not establish that CDP causes the remaining difference. Windows
accessibility/screenshot attempts did not expose the page content and are not
used as score evidence. Raw NetLog remains in ignored local artifacts.

Incognito follow-ups also scored 0.20 Low: two fresh profiles each for JavaScript
disabled, JavaScript with all surfaces and varied GPU, and Native with all
surfaces and varied GPU. All six CreepJS captures remained clear. Three visits
per configuration also stayed at 0.20 after the site's collector explicitly
reported incognito on subsequent visits. Standalone Chrome incognito without a
debugging connection returned 0.36 Watch. These results do not explain the user's
reported 0.10 baseline. Evidence: `incognito-comparison/`, `incognito-repeat/`,
`incognito-payload/` and `no-debugger-incognito/` under the investigation artifacts.

The earlier preservation preset and investigation below remain for comparison;
they are no longer the only configuration observed to reach Low.

## OverpoweredJS investigation and GPU-preservation preset

The user's latest metadata identifies the reported **0.10 Low** result as Google
Chrome on Windows 11 in incognito mode, superseding the earlier Firefox report.
We have not reproduced that exact score in an isolated Chromium profile. The new **Preserve actual
GPU** button in the account's Generated fingerprint tab measured **0.20 Low**
with all eight surface toggles selected, in three fresh profiles per backend:

| Backend | Bot score, all three runs | API anomalies | CreepJS lies / warnings / captured errors |
| --- | --- | --- | --- |
| JavaScript compatibility, Chrome 154 | 0.20 Low | Empty | 0 / 0 / 0 |
| Native Chromium 153 | 0.20 Low | Empty | 0 / 0 / 0 |

The API's **bot** component is 0.20; its separate **fraud** component is 0.10.
These are not interchangeable. The site's [score documentation](https://overpoweredjs.bot/docs/scores)
defines Low as below 0.30. Scores and anomaly flags are separate outputs; an empty
anomaly array does not establish a zero score.

This preset selects the actual GPU, WebGL variation **Off**, and WebGPU capability
policy **Native**. It leaves all surface controls and the other generated values
unchanged. Canvas drawing variation, CPU/memory, screen/scaling and font filtering
remain available. **This is not a successful all-perturbations-enabled result.**
Existing presets are not silently migrated, and selecting standard WebGL variation
again can restore the high scores. Save the profile and open a new browser to apply.

Controlled ablations before the repair found:

| Configuration | Bot score | Automated / anti-detect checks |
| --- | --- | --- |
| JavaScript WebGL, generated GPU and standard pixel variation | 0.99 High | Flagged / flagged |
| Same WebGL configuration, pixel variation off | 0.84 Elevated | Clear / clear |
| WebGL, actual GPU and pixel variation off | 0.20 Low | Clear / clear |
| JavaScript all surfaces, actual GPU but standard pixel variation | 0.97 High | Flagged / flagged |
| Native all surfaces, actual GPU but standard shader variation | 0.84 Elevated | Clear / clear |
| JavaScript all surfaces, actual GPU, pixel variation off, compatible WebGPU limits | 0.20 Low | Clear / clear |

The code explains a real consistency defect: JavaScript WebGL changes readPixels
results without changing the underlying framebuffer. Copying/exporting that
framebuffer can disagree with readback. A renderer alias also cannot reproduce the
claimed model's rasterization. The observed score changes support these as major
contributors; the site's public response does not expose private scoring weights.
Native shader changes avoid the readback-only inconsistency but still produced an
elevated score with the actual GPU identity. Restricting WebGPU limits alone did
not raise the score in the final controlled case above.

The repair preserves the PCI ID when a GPU choice is already the actual model,
skips WebGL hooks when the GPU and pixels are unchanged, and skips WebGPU hooks
when both identity and capabilities are native. Native mode omits unnecessary GPU
override switches. Headed JavaScript mode now keeps the installed browser's native
user agent and detailed client hints rather than supplying an unnecessary launch
user-agent override. These changes do not rewrite detector responses or scores.

A separate fresh Chrome profile, opened directly at the site before connecting a
browser-control client, returned **0.36 Watch**. Reloading after connecting returned
**0.20 Low**, with all displayed checks clear in both cases. This does not isolate
connection effects from reload/profile-state effects, but establishes that the
remaining difference is also present without the app's fingerprint scripts. Window
geometry changes did not improve 0.20. The reason for the remaining difference
from the user's reported result is unresolved; personal browser data was not read
or copied. The earlier **0.10** target was not reached and has since been withdrawn.
Fingerprint-scan was not rerun
in this investigation; its previously observed Medium classification remains open.

Reproduce the six completed runs:

```powershell
.\.venv\Scripts\python scripts/overpowered_audit.py --output artifacts/overpowered-investigation/reproduce --cases js-preserve-gpu,native-preserve-gpu --creepjs --repeats 3
```

Conditions: visible browser, fresh normal profiles, synthetic fixed account seed,
all eight surface toggles on, US timezone, direct connection with proxy matching
disabled, bundled runtime bridge, no additional extensions. Evidence lives under
`artifacts/overpowered-investigation/`: `ablation`, `controls`, `gpu-controls`,
`geometry`, `preconnect`, and `repaired`. The repaired API decisions contain empty
anomaly arrays, and worker renderer values agree with the actual main-thread GPU.
Raw network/device captures and downloaded public SDK research stay outside Git.

## Earlier public-site matrix (before GPU investigation)

Each cell reports **fingerprint-scan score / OverpoweredJS score and label**.
Sources are live captures from [fingerprint-scan](https://fingerprint-scan.com/),
[OverpoweredJS](https://overpoweredjs.bot/), and
[CreepJS](https://abrahamjuliot.github.io/creepjs/).

| Configuration | Old JS result | Repaired JS | Native fork (everything off) |
| --- | --- | --- | --- |
| Disabled | 20 / 0.20 Low | 5 / 0.20 Low | 5 / 0.76 Elevated |
| Canvas | 20 / 0.20 Low | 5 / 0.20 Low | Same disabled baseline |
| WebGL | 20 / 0.99 High | 5 / 0.99 High | Same disabled baseline |
| WebGPU | 20 / 0.84 Elevated | 5 / 0.84 Elevated | Same disabled baseline |
| Everything | 20 / 0.99 High | 5 / 0.99 High | Same disabled baseline |

Native with everything enabled measured **5 / 0.97 High** after the changes,
compared with **5 / 0.95 High** in the initial capture. JavaScript everything-on
still flagged automated-browser and anti-detect-browser checks. Native
everything-on flagged automated-browser, while anti-detect-browser was clear.
The disabled native baseline had both checks clear despite its elevated score.

CreepJS reported **0 lies, 0 warnings and 0 internally captured errors** for all
five JavaScript configurations and both native configurations in the completed
initial and repaired captures. This establishes no regression in those counters
on this machine; it does not establish universal compatibility or a Low bot score.

## Conditions and limits

- Windows desktop with an NVIDIA RTX 3070; installed Chrome 154 and the pinned
  native Chromium 153.0.8010.47-1. These are different browser builds.
- Fixed synthetic account and seed, visible browser, fresh browser storage per
  case, direct connection, proxy matching disabled. No retailer account, login,
  checkout, or production settings were used.
- Initial captures used incognito without selected extensions. Repaired captures
  used normal mode with the bundled runtime bridge and the genuine, signed Google
  Docs Offline extension. This compares usable configurations, not an isolated
  experiment attributing every difference to one code change.
- A separate repaired normal-mode run **without** Docs Offline still scored
  20/100 in JavaScript everything-on. With Docs Offline, the scanner's default
  Chrome-extension check changed to true and the score was 5/100. Hardware/OS and
  Network/proxy were Low; Browser remained Medium. The extension is optional,
  not silently selected for every existing account.
- One completed capture per configuration is reported here. Interrupted captures
  were rerun. Scores are external measurements and may change with browser, device,
  network, site implementation or run. Preserving valid combinations and native
  capability limits does not guarantee the requested detector classifications.

## Reproduce

Install the optional native browser first, and install/register Google Docs
Offline using Settings if reproducing that configuration. Supply its actual
unpacked folder, not the application's extension record ID:

```powershell
.\.venv\Scripts\python scripts/account_fingerprint_matrix.py --normal --extensions 'C:\path\to\google-docs-offline' --output artifacts/profile-audit
```

Omit `--normal` for incognito. Omit `--extensions` to test without selected third
party extensions. Use `--cases javascript-disabled,javascript-everything,native-disabled`
for a subset, or `--sites creepjs` for consistency counters only. Use separate
output folders for different settings. Rerunning a case replaces that case's
captures and merges its row into the existing aggregate.

Local evidence for this run is under `artifacts/fingerprint-expansion/`: initial
`baseline/`, repaired `repaired-normal/`, and `repaired-docs/`. Per-case JSON and
screenshots are the primary evidence; the last directory's earlier aggregate was
overwritten by a subset before merge support was added, so its two everything-on
captures remain in their per-case folders. Raw captures contain device/network
fingerprints and are intentionally excluded from Git.

## Functional verification

The October 8 GPU investigation passed **38 relevant regression tests** covering
the profile editor, GPU API preservation, native launch switches, fingerprint
surfaces and account browser behavior. A subsequent headed/headless identity check
passed both cases, including preservation of detailed client hints in headed mode
(**39 distinct tests** across the two runs). Evidence: `artifacts/overpowered-investigation/regression.xml`
and `client-hints.xml`. JavaScript syntax and Git whitespace checks passed.
The six public-site runs above are separate from these automated tests.

Automated tests cover global inheritance and explicit Off overrides; account and
starting-website editing; native settings/account toggle availability; compatible
preset validation; native CPU/memory agreement across pages and workers; native
font/screen/timezone behavior; real runtime APIs in normal and incognito sessions;
browser-close cleanup; extension assignment and removal; publisher signature and
tamper rejection; worker lifecycle; saved sessions; and generated-tab persistence.

The full regression rerun passed **224 tests**. A subsequently added normal-profile
session-import/persistence test also passed, bringing the verified coverage to
**225 distinct tests**. Normal/incognito runtime checks were repeated after the
persistence change and passed. JavaScript syntax and Git whitespace checks passed.
The suite includes browser interaction, settings, fixtures, account state and
checkout safety checks. Machine-readable results are in
`artifacts/fingerprint-expansion/regression-final.xml` and `persistence-final.xml`.
