import asyncio

import pytest

from retail.amazon import Amazon
from retail.fingerprint import build_scripts, build_worker_script
from retail.store import Store


TEST_PAGE = '<body><canvas id="c" width="200" height="100"></canvas></body>'


async def open_page(adapter, account, *, perturb_canvas=True):
    if perturb_canvas:
        # Canvas perturbation is now a Settings opt-in rather than a
        # build_scripts default. Enable it so the header path under test
        # actually installs the 2D hook.
        current = adapter.store.get('settings', 'settings') or {}
        adapter.store.put('settings', {**current, 'fingerprint_canvas': True}, 'settings')
        context = await adapter.context(account)
    else:
        await adapter.ready()
        context = await adapter.browser.new_context(**adapter.profiles.options(account))

    async def route(r):
        await r.fulfill(content_type='text/html', body=TEST_PAGE)

    await context.route('https://fingerprint.test/', route)
    page = await context.new_page()
    await page.goto('https://fingerprint.test/')
    return context, page


async def draw_gradient(page):
    """A diagonal gradient ensures plenty of blended (anti-aliased) pixels."""
    await page.evaluate('''() => {
        const c = document.getElementById('c');
        const ctx = c.getContext('2d');
        const grad = ctx.createLinearGradient(0, 0, c.width, c.height);
        grad.addColorStop(0, '#ffffff');
        grad.addColorStop(0.5, '#808080');
        grad.addColorStop(1, '#000000');
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, c.width, c.height);
    }''', isolated_context=False)


@pytest.mark.parametrize('perturb_canvas', [False, True])
def test_canvas_hash_is_stable_with_optional_account_perturbation(tmp_path, perturb_canvas):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)

        async def canvas_hash(account):
            context, page = await open_page(adapter, account, perturb_canvas=perturb_canvas)
            await draw_gradient(page)
            h1 = await page.evaluate(
                'document.getElementById("c").toDataURL()',
                isolated_context=False,
            )
            h2 = await page.evaluate(
                'document.getElementById("c").toDataURL()',
                isolated_context=False,
            )
            assert h1 == h2, 'canvas hash must be stable across reads'
            await context.close()
            return h1

        a = store.put('accounts', {'name': 'A', 'region': 'US'})
        b = store.put('accounts', {'name': 'B', 'region': 'US'})
        h_a = await canvas_hash(a)
        h_b = await canvas_hash(b)
        assert (h_a != h_b) == perturb_canvas

        await adapter.close()
        store.db.close()

    asyncio.run(scenario())


def test_solid_shapes_read_back_exactly(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        account = store.put('accounts', {'name': 'A', 'region': 'US'})
        context, page = await open_page(adapter, account)

        # Solid rectangle: no gradients, no anti-aliasing.
        await page.evaluate('''() => {
            const c = document.getElementById('c');
            const ctx = c.getContext('2d');
            ctx.fillStyle = '#ff0000';
            ctx.fillRect(10, 10, 100, 50);
        }''', isolated_context=False)

        result = await page.evaluate('''() => {
            const ctx = document.getElementById('c').getContext('2d');
            const d = ctx.getImageData(20, 20, 50, 20).data;
            for (let i = 0; i < d.length; i += 4) {
                if (d[i] !== 255 || d[i+1] !== 0 || d[i+2] !== 0 || d[i+3] !== 255) {
                    return { ok: false, index: i, rgba: [d[i], d[i+1], d[i+2], d[i+3]] };
                }
            }
            return { ok: true };
        }''', isolated_context=False)

        assert result['ok'], f'solid pixels must be unchanged, got {result}'
        await context.close()
        await adapter.close()
        store.db.close()

    asyncio.run(scenario())


def test_crop_invariance(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        account = store.put('accounts', {'name': 'A', 'region': 'US'})
        context, page = await open_page(adapter, account)
        await draw_gradient(page)

        result = await page.evaluate('''() => {
            const c = document.getElementById('c');
            const ctx = c.getContext('2d');
            const full = ctx.getImageData(0, 0, c.width, c.height).data;
            const crop = ctx.getImageData(50, 20, 80, 40).data;
            const mismatches = [];
            for (let y = 0; y < 40; y++) {
                for (let x = 0; x < 80; x++) {
                    const ci = (y * 80 + x) * 4;
                    const fi = ((y + 20) * c.width + (x + 50)) * 4;
                    for (let k = 0; k < 4; k++) {
                        if (full[fi + k] !== crop[ci + k]) {
                            mismatches.push({x, y, k, full: full[fi + k], crop: crop[ci + k]});
                            break;
                        }
                    }
                    if (mismatches.length > 5) break;
                }
                if (mismatches.length > 5) break;
            }
            return { mismatches };
        }''', isolated_context=False)

        assert not result['mismatches'], f'crop invariance failed: {result["mismatches"]}'
        await context.close()
        await adapter.close()
        store.db.close()

    asyncio.run(scenario())


def test_patched_functions_look_native(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        account = store.put('accounts', {'name': 'A', 'region': 'US'})
        context, page = await open_page(adapter, account)

        for expr in (
            'HTMLCanvasElement.prototype.toDataURL.toString()',
            'HTMLCanvasElement.prototype.toBlob.toString()',
            'CanvasRenderingContext2D.prototype.getImageData.toString()',
        ):
            src = await page.evaluate(expr, isolated_context=False)
            assert '[native code]' in src, f'{expr} should look native, got {src!r}'

        await context.close()
        await adapter.close()
        store.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('perturb_canvas', [False, True])
def test_webgl_and_general_runtime_remain_native(tmp_path, perturb_canvas):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        try:
            await adapter.ready()
            context = await adapter.browser.new_context()
            page = await context.new_page()
            result = await page.evaluate('''async ({scripts, perturbCanvas}) => {
                const targets = [
                    [Function.prototype, 'toString'],
                    [EventTarget.prototype, 'addEventListener'],
                    [HTMLIFrameElement.prototype, 'contentWindow'],
                    [HTMLIFrameElement.prototype, 'contentDocument'],
                ];
                if (!perturbCanvas) {
                    for (const C of [HTMLCanvasElement, CanvasRenderingContext2D,
                                     OffscreenCanvas, OffscreenCanvasRenderingContext2D, Worker]) {
                        for (const key of Object.getOwnPropertyNames(C.prototype)) {
                            targets.push([C.prototype, key]);
                        }
                    }
                }
                for (const C of [WebGLRenderingContext, WebGL2RenderingContext]) {
                    for (const key of Object.getOwnPropertyNames(C.prototype)) {
                        targets.push([C.prototype, key]);
                    }
                }
                const before = targets.map(([p, k]) => Object.getOwnPropertyDescriptor(p, k));
                const ownKeys = Reflect.ownKeys(window);
                const canvas = document.createElement('canvas');
                const gl = canvas.getContext('webgl2', {preserveDrawingBuffer: true});
                if (!gl) throw new Error('WebGL2 is required for this regression test');
                gl.enable(gl.SCISSOR_TEST);
                for (let x = 0; x < canvas.width; x++) {
                    gl.scissor(x, 0, 1, canvas.height);
                    gl.clearColor(x / canvas.width, x / canvas.width, x / canvas.width, 1);
                    gl.clear(gl.COLOR_BUFFER_BIT);
                }
                const exportBefore = canvas.toDataURL();
                for (const script of scripts) (0, eval)(script);
                const unchanged = targets.every(([p, k], i) => {
                    const after = Object.getOwnPropertyDescriptor(p, k);
                    return Reflect.ownKeys(before[i]).every(key => before[i][key] === after[key]);
                });
                const addedKeys = Reflect.ownKeys(window).filter(k => !ownKeys.includes(k));
                const unused = document.createElement('canvas');
                unused.toDataURL();
                const exportDoesNotLockContext = !!unused.getContext('webgl2');
                function identity(gl) {
                    const ext = gl.getExtension('WEBGL_debug_renderer_info');
                    return {
                        vendor: ext && gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
                        renderer: ext && gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),
                        extensions: gl.getSupportedExtensions().sort(),
                        maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE),
                    };
                }
                const url = URL.createObjectURL(new Blob([
                    `const identity = ${identity.toString()};
                     onmessage = () => {
                        const gl = new OffscreenCanvas(1, 1).getContext('webgl2');
                        postMessage(gl ? identity(gl) : null);
                     };`
                ], {type: 'text/javascript'}));
                const worker = new Worker(url);
                let workerIdentity;
                try {
                    workerIdentity = await new Promise((resolve, reject) => {
                        const timeout = setTimeout(() => reject(new Error('Worker timed out')), 10000);
                        worker.onmessage = e => { clearTimeout(timeout); resolve(e.data); };
                        worker.onerror = e => { clearTimeout(timeout); reject(new Error(e.message)); };
                        worker.postMessage(null);
                    });
                } finally {
                    worker.terminate();
                    URL.revokeObjectURL(url);
                }
                return {unchanged, addedKeys, exportDoesNotLockContext,
                        exportUnchanged: exportBefore === canvas.toDataURL(),
                        main: identity(gl), worker: workerIdentity, error: gl.getError()};
            }''', {'scripts': build_scripts(123, perturb_canvas=perturb_canvas),
                   'perturbCanvas': perturb_canvas}, isolated_context=False)
            assert result['unchanged']
            assert result['addedKeys'] == []
            assert result['exportUnchanged']
            assert result['exportDoesNotLockContext']
            assert result['main'] == result['worker']
            assert result['error'] == 0
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


def test_child_frame_initialization_and_listener_removal(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        try:
            account = store.put('accounts', {'name': 'A', 'region': 'US'})
            context, page = await open_page(adapter, account)
            await draw_gradient(page)
            parent_hash = await page.evaluate(
                'document.getElementById("c").toDataURL()', isolated_context=False)
            result = await page.evaluate('''async html => {
                const frame = document.createElement('iframe');
                let removedCalls = 0;
                const removed = () => removedCalls++;
                frame.addEventListener('load', removed);
                frame.removeEventListener('load', removed);
                const loaded = new Promise(resolve => frame.addEventListener('load', resolve, {once: true}));
                frame.srcdoc = html;
                document.body.appendChild(frame);
                await loaded;
                return {removedCalls, marker: '__retailFingerprintInstalled' in frame.contentWindow};
            }''', TEST_PAGE, isolated_context=False)
            assert result == {'removedCalls': 0, 'marker': False}
            child = page.frames[1]
            await draw_gradient(child)
            child_hash = await child.evaluate(
                'document.getElementById("c").toDataURL()', isolated_context=False)
            assert child_hash == parent_hash
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('worker_type', ['classic', 'module'])
@pytest.mark.parametrize('perturb_canvas', [False, True])
def test_canvas_matches_explicitly_initialized_worker(tmp_path, worker_type, perturb_canvas):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        try:
            account = store.put('accounts', {'name': 'Worker fixture', 'region': 'US'})
            _, page = await open_page(adapter, account, perturb_canvas=perturb_canvas)
            seed = int(adapter.profiles.get(account)['seed'], 16)
            result = await page.evaluate('''async ({workerType, workerScript}) => {
                async function render() {
                    const results = [];
                    for (const background of [null, '#ffffff', 'rgba(100, 50, 20, 0.4)']) {
                        const canvas = new OffscreenCanvas(48, 24);
                        const ctx = canvas.getContext('2d');
                        if (background) {
                            ctx.fillStyle = background;
                            ctx.fillRect(0, 0, 48, 24);
                        }
                        const gradient = ctx.createLinearGradient(0, 0, 48, 24);
                        gradient.addColorStop(0, 'rgba(255, 40, 80, 0.3)');
                        gradient.addColorStop(1, 'rgba(20, 160, 220, 1)');
                        ctx.fillStyle = gradient;
                        ctx.beginPath();
                        ctx.arc(24.25, 12.5, 9.4, 0, Math.PI * 2);
                        ctx.fill();
                        ctx.font = '10px sans-serif';
                        ctx.fillText('Canvas', 2.5, 14.25);
                        const pixels = Array.from(ctx.getImageData(0, 0, 48, 24).data);
                        const repeat = Array.from(ctx.getImageData(0, 0, 48, 24).data);
                        const blob = await canvas.convertToBlob();
                        const exported = Array.from(new Uint8Array(await blob.arrayBuffer()));
                        results.push({pixels, repeat, exported});
                    }
                    return results;
                }
                const source = `${workerScript}; const render = ${render.toString()};
                    self.onmessage = async () => {
                        try { self.postMessage({results: await render()}); }
                        catch (e) { self.postMessage({error: String(e)}); }
                    };`;
                const url = URL.createObjectURL(new Blob([source], {type: 'text/javascript'}));
                const worker = new Worker(url, {type: workerType});
                try {
                    const workerResult = new Promise((resolve, reject) => {
                        const timer = setTimeout(() => reject(new Error('Worker timeout')), 10000);
                        worker.onmessage = e => {
                            clearTimeout(timer);
                            if (e.data.error) reject(new Error(e.data.error));
                            else resolve(e.data.results);
                        };
                        worker.onerror = e => { clearTimeout(timer); reject(new Error(e.message)); };
                        worker.postMessage(null);
                    });
                    const [main, remote] = await Promise.all([render(), workerResult]);
                    const equal = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
                    return {
                        pixelsAgree: main.every((r, i) => equal(r.pixels, remote[i].pixels)),
                        exportsAgree: main.every((r, i) => equal(r.exported, remote[i].exported)),
                        stable: [...main, ...remote].every(r => equal(r.pixels, r.repeat)),
                        hasPartialAlpha: main[0].pixels.some((v, i) => i % 4 === 3 && v > 0 && v < 255),
                        hasTransparentPixels: main[0].pixels.some((v, i) => i % 4 === 3 && v === 0),
                    };
                } finally {
                    worker.terminate();
                    URL.revokeObjectURL(url);
                }
            }''', {'workerType': worker_type,
                   'workerScript': build_worker_script(seed, perturb_canvas=True) if perturb_canvas else ''},
                isolated_context=False)
            assert result == {
                'pixelsAgree': True, 'exportsAgree': True, 'stable': True,
                'hasPartialAlpha': True, 'hasTransparentPixels': True,
            }
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


def test_transparent_perturbation_and_native_readback_semantics(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        try:
            await adapter.ready()
            context = await adapter.browser.new_context()
            page = await context.new_page()
            result = await page.evaluate('''script => {
                const nativeRead = CanvasRenderingContext2D.prototype.getImageData;
                (0, eval)(script);
                const canvas = document.createElement('canvas');
                canvas.width = 128; canvas.height = 64;
                const ctx = canvas.getContext('2d');
                const gradient = ctx.createLinearGradient(0, 0, 128, 64);
                gradient.addColorStop(0, 'rgba(230, 180, 140, 0.3)');
                gradient.addColorStop(1, 'rgba(30, 50, 90, 0.8)');
                ctx.fillStyle = gradient;
                ctx.fillRect(8, 8, 112, 48);
                const native = nativeRead.call(ctx, 0, 0, 128, 64).data;
                const patched = ctx.getImageData(0, 0, 128, 64).data;
                let changes = 0, alphaChanges = 0, transparentChanges = 0;
                for (let i = 0; i < patched.length; i++) {
                    if (patched[i] !== native[i]) {
                        changes++;
                        if (i % 4 === 3) alphaChanges++;
                        if (native[(i & ~3) + 3] === 0) transparentChanges++;
                    }
                }
                const crop = ctx.getImageData(16, 12, 40, 24).data;
                const cropAgrees = crop.every((v, i) => {
                    const row = Math.floor(i / 160), colByte = i % 160;
                    return v === patched[((row + 12) * 128 + 16) * 4 + colByte];
                });
                let conversions = 0, optionReads = 0;
                ctx.getImageData({valueOf() { conversions++; return 0; }}, 0, 1, 1);
                ctx.getImageData(0, 0, 1, 1, {get colorSpace() { optionReads++; return 'srgb'; }});
                const outcome = fn => {
                    try { const image = fn(); return {
                        type: image.data.constructor.name, pixelFormat: image.pixelFormat,
                        bytes: Array.from(new Uint8Array(image.data.buffer)),
                    }; } catch (e) { return {error: e.name}; }
                };
                const floatArgs = [0, 0, 8, 8, {pixelFormat: 'rgba-float16'}];
                const floatNative = outcome(() => nativeRead.apply(ctx, floatArgs));
                const floatPatched = outcome(() => ctx.getImageData(...floatArgs));
                const zeroNative = outcome(() => nativeRead.call(ctx, 0, 0, 0, 1));
                const zeroPatched = outcome(() => ctx.getImageData(0, 0, 0, 1));
                return {changes, alphaChanges, transparentChanges, cropAgrees, conversions, optionReads,
                        floatNative, floatPatched, zeroNative, zeroPatched};
            }''', build_scripts(123, perturb_canvas=True)[0], isolated_context=False)
            assert result['changes'] > 0
            assert result['alphaChanges'] == result['transparentChanges'] == 0
            assert result['cropAgrees']
            assert result['conversions'] == result['optionReads'] == 1
            assert result['floatNative'] == result['floatPatched']
            assert result['zeroNative'] == result['zeroPatched'] == {'error': 'IndexSizeError'}
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('spoof_audio', [False, True])
def test_audio_context_values_are_plausible_and_stable(tmp_path, spoof_audio):
    """AudioContext must report values that agree with themselves.

    With spoof_audio=False the runtime's own values are returned. With
    spoof_audio=True the module still prefers every semantically valid
    native value and only substitutes a fallback for an invalid read.
    In both cases the values must be internally consistent and stable
    across repeated contexts.
    """
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        await adapter.ready()
        context = await adapter.browser.new_context()
        for script in build_scripts(123, spoof_audio=spoof_audio):
            await context.add_init_script(script)
        page = await context.new_page()
        await page.goto('about:blank')
        result = await page.evaluate('''() => {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return { unavailable: true };
            const a = new AC();
            const first = {
                sampleRate: a.sampleRate,
                outputLatency: a.outputLatency,
                baseLatency: a.baseLatency,
                maxChannelCount: a.destination.maxChannelCount,
            };
            a.close();
            const b = new AC();
            const second = {
                sampleRate: b.sampleRate,
                outputLatency: b.outputLatency,
                baseLatency: b.baseLatency,
                maxChannelCount: b.destination.maxChannelCount,
            };
            b.close();
            return { first, second };
        }''', isolated_context=False)
        await context.close()
        await adapter.close()
        store.db.close()
        if result.get('unavailable'):
            pytest.skip('AudioContext unavailable in this runtime')
        first, second = result['first'], result['second']
        # Values must be present and stable.
        assert first == second
        # Values must be plausible: positive sample rate and non-negative
        # latencies/channel capacity. Do not require stereo: mono is valid,
        # and channel-count behavior legitimately varies by context/device.
        assert isinstance(first['sampleRate'], (int, float)) and first['sampleRate'] > 0
        assert isinstance(first['outputLatency'], (int, float)) and first['outputLatency'] >= 0
        assert isinstance(first['baseLatency'], (int, float)) and first['baseLatency'] >= 0
        assert isinstance(first['maxChannelCount'], (int, float)) and first['maxChannelCount'] >= 0

    asyncio.run(scenario())


def test_audio_spoof_preserves_usable_native_values(tmp_path):
    """When the runtime already reports usable audio values, spoofing must
    not replace them. Substituting a fabricated device over a genuine one
    only creates incoherence with the rest of the runtime.
    """
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        await adapter.ready()
        native_ctx = await adapter.browser.new_context()
        native_page = await native_ctx.new_page()
        await native_page.goto('about:blank')
        native = await native_page.evaluate('''() => {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return null;
            const a = new AC();
            const out = {
                sampleRate: a.sampleRate,
                outputLatency: a.outputLatency,
                baseLatency: a.baseLatency,
                maxChannelCount: a.destination.maxChannelCount,
            };
            a.close();
            return out;
        }''', isolated_context=False)
        await native_ctx.close()

        spoofed_ctx = await adapter.browser.new_context()
        for script in build_scripts(123, spoof_audio=True):
            await spoofed_ctx.add_init_script(script)
        spoofed_page = await spoofed_ctx.new_page()
        await spoofed_page.goto('about:blank')
        spoofed = await spoofed_page.evaluate('''() => {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return null;
            const a = new AC();
            const out = {
                sampleRate: a.sampleRate,
                outputLatency: a.outputLatency,
                baseLatency: a.baseLatency,
                maxChannelCount: a.destination.maxChannelCount,
            };
            a.close();
            return out;
        }''', isolated_context=False)
        await spoofed_ctx.close()
        await adapter.close()
        store.db.close()
        if native is None or spoofed is None:
            pytest.skip('AudioContext unavailable in this runtime')
        # A semantically valid native value must survive the fallback.
        # Zero is valid for latency values and channel capacity; only
        # sampleRate is required to be strictly positive.
        for key in ('sampleRate', 'outputLatency', 'baseLatency', 'maxChannelCount'):
            native_val, spoofed_val = native[key], spoofed[key]
            native_usable = (
                isinstance(native_val, (int, float))
                and not (isinstance(native_val, float) and native_val != native_val)
                and (native_val > 0 if key == 'sampleRate' else native_val >= 0)
            )
            if native_usable:
                assert spoofed_val == native_val, f'{key} was replaced despite a usable native value'

    asyncio.run(scenario())

@pytest.mark.parametrize(('channels', 'sample_rate'), [(1, 44100), (2, 48000)])
def test_audio_spoof_preserves_offline_audio_semantics(tmp_path, channels, sample_rate):
    """Offline audio metadata and rendered output must match the native runtime."""
    async def read_offline_audio(adapter, *, patched):
        context = await adapter.browser.new_context()
        try:
            if patched:
                for script in build_scripts(123, spoof_audio=True):
                    await context.add_init_script(script)
            page = await context.new_page()
            await page.goto('about:blank')
            return await page.evaluate("""async ([channels, sampleRate]) => {
                const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
                if (!OAC) return null;
                const ctx = new OAC(channels, 128, sampleRate);
                const source = ctx.createConstantSource();
                source.offset.value = 0.25;
                source.connect(ctx.destination);
                source.start();
                const buffer = await ctx.startRendering();
                return {
                    sampleRate: ctx.sampleRate,
                    maxChannelCount: ctx.destination.maxChannelCount,
                    channelCount: ctx.destination.channelCount,
                    renderedSampleRate: buffer.sampleRate,
                    samples: Array.from({length: buffer.numberOfChannels},
                        (_, i) => Array.from(buffer.getChannelData(i))),
                };
            }""", [channels, sample_rate], isolated_context=False)
        finally:
            await context.close()

    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        try:
            await adapter.ready()
            native = await read_offline_audio(adapter, patched=False)
            patched = await read_offline_audio(adapter, patched=True)
            if native is None or patched is None:
                pytest.skip('OfflineAudioContext unavailable in this runtime')
            # Browser versions can report different offline channel capacities.
            # Preserve the native value instead of assuming it is always zero.
            assert patched == native
            assert patched['sampleRate'] == sample_rate
            assert patched['renderedSampleRate'] == sample_rate
            assert patched['channelCount'] == channels
            assert patched['samples'] == [[0.25] * 128 for _ in range(channels)]
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())


def test_audio_spoof_leaves_compressor_reduction_native(tmp_path):
    """DynamicsCompressorNode.reduction is graph state, not profile metadata."""
    async def read_reduction(adapter, *, patched):
        context = await adapter.browser.new_context()
        if patched:
            for script in build_scripts(123, spoof_audio=True):
                await context.add_init_script(script)
        page = await context.new_page()
        await page.goto('about:blank')
        value = await page.evaluate("""() => {
            const AC = window.AudioContext || window.webkitAudioContext;
            if (!AC) return null;
            const ctx = new AC();
            const compressor = ctx.createDynamicsCompressor();
            const reduction = compressor.reduction;
            ctx.close();
            return reduction;
        }""", isolated_context=False)
        await context.close()
        return value

    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        await adapter.ready()
        try:
            native = await read_reduction(adapter, patched=False)
            patched = await read_reduction(adapter, patched=True)
            if native is None or patched is None:
                pytest.skip('AudioContext unavailable in this runtime')
            assert patched == native
        finally:
            await adapter.close()
            store.db.close()

    asyncio.run(scenario())
