import asyncio

import pytest

from retail.amazon import Amazon
from retail.fingerprint import build_scripts
from retail.store import Store


@pytest.mark.parametrize('seed', [1, 123, 285165479])
def test_webgpu_retains_native_objects_and_request_semantics(tmp_path, seed):
    async def scenario():
        store = Store(tmp_path)
        # The bundled Chromium on this host exposes no WebGPU adapter.
        # Use the same installed Chrome channel as the live differential audit.
        store.put('settings', {'browser_channel': 'chrome'}, 'settings')
        adapter = Amazon(store)
        try:
            await adapter.ready()
            context = await adapter.browser.new_context()
            await context.route('https://gpu.test/', lambda r: r.fulfill(body='<body>GPU test</body>'))
            page = await context.new_page()
            await page.goto('https://gpu.test/')
            result = await page.evaluate('''async scripts => {
                if (!navigator.gpu) throw new Error('WebGPU unavailable');
                const requestAdapter = GPU.prototype.requestAdapter;
                const infoGetter = Object.getOwnPropertyDescriptor(GPUAdapter.prototype, 'info').get;
                const vendorGetter = Object.getOwnPropertyDescriptor(GPUAdapterInfo.prototype, 'vendor').get;
                const architectureGetter = Object.getOwnPropertyDescriptor(GPUAdapterInfo.prototype, 'architecture').get;
                const nativeSize = Object.getOwnPropertyDescriptor(GPUSupportedFeatures.prototype, 'size').get;
                const nativeHas = GPUSupportedFeatures.prototype.has;
                const nativeRequestDevice = GPUAdapter.prototype.requestDevice;
                for (const script of scripts) (0, eval)(script);
                const gpu = await navigator.gpu.requestAdapter();
                if (!gpu) throw new Error('A WebGPU adapter is required for this regression');
                const failures=[];
                const check=(name, fn) => { try { if (!fn()) failures.push(name); }
                                          catch(e) { failures.push(name+': '+e.message); } };
                check('native requestAdapter', () => requestAdapter === GPU.prototype.requestAdapter);
                check('adapter brand', () => infoGetter.call(gpu) === gpu.info);
                check('info brand', () => typeof vendorGetter.call(gpu.info) === 'string');
                check('native vendor', () => vendorGetter.call(gpu.info) === gpu.info.vendor);
                check('native architecture', () => architectureGetter.call(gpu.info) === gpu.info.architecture);
                check('feature brand', () => nativeSize.call(gpu.features) >= gpu.features.size);
                check('native feature method', () => typeof nativeHas.call(gpu.features, 'timestamp-query') === 'boolean');
                const getter=Object.getOwnPropertyDescriptor(GPUAdapterInfo.prototype, 'vendor').get;
                check('info descriptor consistency', () => getter.call(gpu.info) === gpu.info.vendor);
                check('stable info', () => gpu.info === gpu.info);
                const features=[...gpu.features];
                check('features iteration', () => features.length === gpu.features.size && features.every(f => gpu.features.has(f)));
                check('features borrowed method', () => GPUSupportedFeatures.prototype.has.call(gpu.features, features[0]) === gpu.features.has(features[0]));
                const visited=[];
                gpu.features.forEach((v,k,set) => { if (v!==k || set!==gpu.features) failures.push('forEach receiver'); visited.push(v); });
                check('features forEach', () => JSON.stringify(visited)===JSON.stringify(features));
                let invalid;
                try { getter.call({}); } catch(e) { invalid=e.name; }
                check('invalid info receiver', () => invalid==='TypeError');
                let reads=0, iterations=0;
                const requested=features.filter(f=>f==='timestamp-query');
                const device=await gpu.requestDevice({get requiredFeatures(){
                    reads++;
                    return (function*(){ iterations++; yield* requested; })();
                }});
                try {
                    check('descriptor evaluated once', () => reads===1 && iterations===1);
                    check('requested features enabled', () => requested.every(f=>device.features.has(f)));
                    check('device method identity', () => device.createBuffer===GPUDevice.prototype.createBuffer);
                    check('device brand', () => {
                        const b=GPUDevice.prototype.createBuffer.call(device,{size:4,usage:GPUBufferUsage.COPY_DST});
                        b.destroy(); return true;
                    });
                    check('device info brand', () => typeof vendorGetter.call(device.adapterInfo)==='string');
                } finally { device.destroy(); }
                let invalidRequest;
                try { await GPUAdapter.prototype.requestDevice.call({}, {requiredFeatures:['unknown-feature']}); }
                catch(e) { invalidRequest=e.name; }
                check('invalid request receiver', () => invalidRequest==='TypeError');
                let limitError;
                try { const fresh=await navigator.gpu.requestAdapter(); const d=await fresh.requestDevice({requiredLimits:{maxTextureDimension2D:fresh.limits.maxTextureDimension2D+1}}); d.destroy(); }
                catch(e) { limitError=e.name; }
                check('advertised limit enforced', () => limitError==='OperationError');
                async function traceRequest(request) {
                    const trace=[];
                    const desc=Object.freeze({
                        get label(){trace.push('label'); return {toString(){trace.push('label value');return 'test';}};},
                        get defaultQueue(){trace.push('queue');return {get label(){trace.push('queue label');return '';}};},
                        get requiredFeatures(){trace.push('features');return (function*(){trace.push('iterate');})();},
                        get requiredLimits(){trace.push('limits');return Object.freeze({
                            get maxTextureDimension2D(){trace.push('limit');return {valueOf(){trace.push('number');return 64;}};}
                        });}
                    });
                    const fresh=await navigator.gpu.requestAdapter();
                    const d=await request.call(fresh,desc); d.destroy(); return trace;
                }
                const nativeTrace=await traceRequest(nativeRequestDevice);
                const patchedTrace=await traceRequest(gpu.requestDevice);
                check('native dictionary order and conversion', () => JSON.stringify(nativeTrace)===JSON.stringify(patchedTrace));
                let sequenceError;
                try { const fresh=await navigator.gpu.requestAdapter(); const d=await fresh.requestDevice({requiredFeatures:{length:0}}); d.destroy(); }
                catch(e) {sequenceError=e.name;}
                check('noniterable sequence rejected', () => sequenceError==='TypeError');
                return {failures, vendor:gpu.info.vendor};
            }''', build_scripts(seed, spoof_webgpu=True), isolated_context=False)
            assert result['failures'] == [], result
            assert result['vendor'] in {'nvidia', 'amd', 'intel', 'apple'}
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('seed', [1, 123, 987654321])
def test_webgl_advertises_only_native_extensions_and_supported_limits(tmp_path, seed):
    async def scenario():
        store = Store(tmp_path)
        adapter = Amazon(store)
        try:
            await adapter.ready()
            context = await adapter.browser.new_context()
            await context.add_init_script('''
                globalThis.nativePrecisionGetters = ['rangeMin','rangeMax','precision'].map(k =>
                    Object.getOwnPropertyDescriptor(WebGLShaderPrecisionFormat.prototype,k).get);
                globalThis.nativeGL = [WebGLRenderingContext, WebGL2RenderingContext].map(C => ({
                    getParameter:C.prototype.getParameter,
                    getExtension:C.prototype.getExtension,
                    getSupportedExtensions:C.prototype.getSupportedExtensions,
                    getShaderPrecisionFormat:C.prototype.getShaderPrecisionFormat
                }));
            ''' + '\n'.join(build_scripts(seed, spoof_webgl=True)))
            page = await context.new_page()
            await page.goto('about:blank')
            results = await page.evaluate('''() => ['webgl','webgl2'].map((kind,index) => {
                const gl = document.createElement('canvas').getContext(kind);
                if (!gl) return {kind,unavailable:true};
                const native=nativeGL[index];
                const aggregateErrors=[];
                if(index) {
                    for(const [total,blocks,components] of [
                        ['MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS','MAX_VERTEX_UNIFORM_BLOCKS','MAX_VERTEX_UNIFORM_COMPONENTS'],
                        ['MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS','MAX_FRAGMENT_UNIFORM_BLOCKS','MAX_FRAGMENT_UNIFORM_COMPONENTS']]) {
                        const combined=gl.getParameter(gl[total]);
                        const storage=gl.getParameter(gl[blocks])*gl.getParameter(gl.MAX_UNIFORM_BLOCK_SIZE)/4
                            + gl.getParameter(gl[components]);
                        if(combined<storage || combined>native.getParameter.call(gl,gl[total]))
                            aggregateErrors.push({total,combined,storage});
                    }
                }
                const supported=native.getSupportedExtensions.call(gl);
                const advertised=gl.getSupportedExtensions();
                if(JSON.stringify(advertised)!==JSON.stringify(supported))
                    throw new Error('Native extension set changed');
                const unsupported=advertised.filter(e=>!supported.includes(e));
                const unavailable=advertised.filter(e=>!gl.getExtension(e));
                const excess=[];
                const precisionErrors=[];
                for (const key of ['MAX_TEXTURE_SIZE','MAX_CUBE_MAP_TEXTURE_SIZE',
                    'MAX_RENDERBUFFER_SIZE','MAX_VERTEX_ATTRIBS','MAX_TEXTURE_IMAGE_UNITS',
                    'MAX_VERTEX_TEXTURE_IMAGE_UNITS','MAX_COMBINED_TEXTURE_IMAGE_UNITS',
                    'MAX_VERTEX_UNIFORM_VECTORS','MAX_VARYING_VECTORS','MAX_FRAGMENT_UNIFORM_VECTORS',
                    'MAX_3D_TEXTURE_SIZE','MAX_ARRAY_TEXTURE_LAYERS','MAX_DRAW_BUFFERS',
                    'MAX_COLOR_ATTACHMENTS','MAX_SAMPLES','MAX_UNIFORM_BUFFER_BINDINGS',
                    'MAX_VERTEX_UNIFORM_COMPONENTS','MAX_FRAGMENT_UNIFORM_COMPONENTS',
                    'MAX_VERTEX_OUTPUT_COMPONENTS','MAX_FRAGMENT_INPUT_COMPONENTS']) {
                    if (!(key in gl)) continue;
                    const actual=native.getParameter.call(gl,gl[key]);
                    const reported=gl.getParameter(gl[key]);
                    if (reported!==actual) excess.push({key,actual,reported});
                }
                for (const key of ['MAX_VIEWPORT_DIMS','ALIASED_POINT_SIZE_RANGE','ALIASED_LINE_WIDTH_RANGE']) {
                    const actual=native.getParameter.call(gl,gl[key]);
                    const reported=gl.getParameter(gl[key]);
                    const lower=key==='MAX_VIEWPORT_DIMS'?reported[0]<=actual[0]:reported[0]>=actual[0];
                    const ordered=key==='MAX_VIEWPORT_DIMS' || reported[0]<=reported[1];
                    if (!lower || !ordered || JSON.stringify([...reported])!==JSON.stringify([...actual])) excess.push({key,actual:[...actual],reported:[...reported]});
                }
                for (const shader of [gl.VERTEX_SHADER,gl.FRAGMENT_SHADER]) {
                    for (const type of [gl.LOW_FLOAT,gl.MEDIUM_FLOAT,gl.HIGH_FLOAT,gl.LOW_INT,gl.MEDIUM_INT,gl.HIGH_INT]) {
                        const actual=native.getShaderPrecisionFormat.call(gl,shader,type);
                        const reported=gl.getShaderPrecisionFormat(shader,type);
                        if (Reflect.ownKeys(reported).length) precisionErrors.push('own properties');
                        nativePrecisionGetters.forEach(getter => {
                            try { getter.call(reported); }
                            catch(e) { precisionErrors.push(e.name); }
                        });
                        for(const key of ['rangeMin','rangeMax','precision'])
                            if(reported[key]!==actual[key]) excess.push({shader,type,key,actual:actual[key],reported:reported[key]});
                    }
                }
                // A supported compression extension must be enabled in the real context.
                const formats=[...native.getParameter.call(gl,gl.COMPRESSED_TEXTURE_FORMATS)];
                const missingFormats=[];
                for(const name of advertised.filter(e=>/compressed_texture|texture_compression/.test(e))) {
                    const ext=gl.getExtension(name);
                    for(const key in ext) if(/^COMPRESSED_/.test(key) && typeof ext[key]==='number' && !formats.includes(ext[key])) missingFormats.push({name,key});
                }
                let receiverError;
                try { gl.getExtension.call({},'unsupported-extension'); } catch(e) {receiverError=e.name;}
                const enumMismatches=[];
                const debug=gl.getExtension('WEBGL_debug_renderer_info');
                if(debug) {
                    const key=debug.UNMASKED_RENDERER_WEBGL;
                    const expected=gl.getParameter(key);
                    for(const arg of [String(key),key+.75,key+2**32])
                        if(gl.getParameter(arg)!==expected) enumMismatches.push(String(arg));
                    let conversions=0;
                    const actual=gl.getParameter({valueOf(){conversions++;return key;}});
                    if(actual!==expected || conversions!==1) enumMismatches.push({actual,expected,conversions});
                }
                const frame=document.createElement('iframe');document.body.append(frame);
                const other=frame.contentDocument.createElement('canvas').getContext(kind);
                const C=index?WebGL2RenderingContext:WebGLRenderingContext;
                let borrowedError;
                try { C.prototype.getExtension.call(other,'WEBGL_debug_renderer_info'); }
                catch(e) {borrowedError=e.name;}
                frame.remove();
                return {kind,unsupported,unavailable,excess,aggregateErrors,precisionErrors,missingFormats,receiverError,enumMismatches,borrowedError};
            })''', isolated_context=False)
            for result in results:
                if result.get('unavailable') is True:
                    continue
                assert not result['unsupported'], result
                assert not result['unavailable'], result
                assert not result['excess'], result
                assert not result['aggregateErrors'], result
                assert not result['precisionErrors'], result
                assert not result['missingFormats'], result
                assert result['receiverError'] == 'TypeError', result
                assert not result['enumMismatches'], result
                assert not result.get('borrowedError'), result
        finally:
            await adapter.close()
            store.db.close()
    asyncio.run(scenario())
