"""Seeded graphics, audio metadata, navigator and explicit font enumeration.

``build_scripts`` gates everything behind explicit opt-in. ``build_worker_script``
supplies the same implementation for worker entrypoints the caller controls.
Both must run before application code touches canvas, WebGL, WebGPU, or audio.

Design rules:

  - Transformations are explicit opt-ins, independent of browser launch mode.
    Either GPU flag enables coherent identity policy across both GPU APIs.
    WebGL drawing and WebGPU feature/limit policies retain separate flags.
  - Canvas perturbation changes gradient coordinates and Bezier control
    points during drawing. Readback, copying and export methods stay native
    and observe one bitmap. Exact pixel writes, analytic primitives, path
    endpoints, text metrics and non-primitive argument coercions stay native.
    Seeded drawing offsets replace the legacy canvas readback algorithm.
  - WebGL varies interpolated fragment colors during rendering, preserving
    native readPixels, PBO reads, texture copies and canvas exports. Analytic
    shaders, texture samples, vertex shaders, integer outputs, multiple targets
    and complex preprocessor programs are not rewritten. This is a limited
    drawing policy, not arbitrary GPU or shader emulation.
  - WebGL renderer aliases stay within the observed hardware family. Vendor,
    capabilities, extensions and shader precision remain native. Unknown and
    mobile families retain native identity. This does not emulate another GPU.
  - WebGPU vendor/architecture remain genuine; device/description are redacted
    when either GPU flag is enabled, unless both GPU identity and capability
    policy are native (in which case the API remains untouched).
    Adapters, devices, info, limits, and feature
    sets remain genuine platform objects; their prototype accessors apply
    the selected policy without replacing native receiver identities.
    Subgroup sizes and isFallbackAdapter read through to the native object,
    keeping them consistent with the exposed feature set. requiredLimits and
    requiredFeatures are validated against the advertised profile during
    native argument conversion. Unsupported limits reject with OperationError;
    unsupported features reject with TypeError.
  - Audio metadata fallbacks are keyed on the same seed as the GPU profile,
    but semantically valid native values are preserved. Positive sample
    rates, non-negative latency values, mono channel counts, and native
    OfflineAudioContext maxChannelCount values are left untouched. Rendered
    audio buffers and DynamicsCompressorNode.reduction remain fully native;
    this layer does not alter DSP output.
  - Unmodified adapter/device methods retain prototype identity and dynamic
    receivers. requestAdapter remains native. Modified prototype accessors
    remain JavaScript wrappers and do not establish native equivalence.
  - WebGL extensions enable the real implementation and are not filtered.
    WebGPU advertised features are restricted to natively supported features.
  - All pixel readback formats and pack states use the native implementation.
    Shader-source queries retain the source the caller submitted, while the
    engine compiles the drawing transform. Modified APIs still expose wrappers.
  - Same-origin classic, module, and data: workers created through the
    wrapped constructors receive the bootstrap before their own entrypoint.
    The application instead uses WorkerProfiles for graphics contexts, covering
    dedicated/shared/service/nested workers and service-worker restarts without
    rewriting worker URLs. Standalone init scripts do not provide that coverage.
  - Function.prototype.toString stays native. Callable wrappers reject direct
    prototype cycles instead of creating recursive prototype chains. Object
    and Reflect remain native; descriptors expose the actual installed hooks.
    The idempotency symbol is reflected honestly, without concealment.
    A local toString receiver bridge preserves native TypeError formatting
    for non-callable objects directly inheriting a wrapper. It does not
    rewrite stacks and does not make the proxy universally indistinguishable.

Not implemented by this script alone: service-worker interception and workers
outside wrapped constructors. Not implemented by either integration:
native-level (non-JS) hook concealment, deep emulation of
native GPU rasterization/shader semantics, WebGPU device-level readback perturbation, native callable
identity for modified methods/accessors, and timing side channels.
"""
from __future__ import annotations

import json
from .fingerprint_profiles import GPU_DEVICE_IDS


CANVAS_JS_TEMPLATE = r"""
(function () {
  'use strict';

  function AFP_BOOTSTRAP() {
    'use strict';

    const SEED            = __SEED__ >>> 0;
    const NOISE_RATE      = __NOISE_RATE__;
    const GL_NOISE_RATE   = __GL_NOISE_RATE__;
    const ENABLE_2D       = __ENABLE_2D__;
    const ENABLE_WEBGL    = __ENABLE_WEBGL__;
    const ENABLE_WEBGPU   = __ENABLE_WEBGPU__;
    const ENABLE_AUDIO    = __ENABLE_AUDIO__;
    const ENABLE_WORKERS  = __ENABLE_WORKERS__;
    const ENABLE_NAVIGATOR = __ENABLE_NAVIGATOR__;
    const ENABLE_FONTS = __ENABLE_FONTS__;
    const PROFILE_VALUES = __PROFILE_VALUES__;
    const GPU_DEVICE_IDS = __GPU_DEVICE_IDS__;

    const win = globalThis;
    const _gopd = Object.getOwnPropertyDescriptor;

    const MARKER = Symbol.for('w' + (((SEED ^ 0xCAFEBABE) >>> 0).toString(36)));
    if (win[MARKER]) return;
    try {
      Object.defineProperty(win, MARKER, {
        value: true, enumerable: false, configurable: false, writable: false
      });
    } catch (e) { win[MARKER] = true; }

    /* ------------------------------------------------------------------ *
     * Hashing core
     * ------------------------------------------------------------------ */

    const fin = (h) => {
      h = Math.imul(h ^ (h >>> 16), 0x7feb352d);
      h = Math.imul(h ^ (h >>> 15), 0x846ca68b);
      return (h ^ (h >>> 16)) | 0;
    };

    /* ------------------------------------------------------------------ *
     * Hook infrastructure
     * ------------------------------------------------------------------ */

    const hookedOwners = new WeakMap();
    const nativeFunctionToString = Function.prototype.toString;
    function callableProxy(orig, traps) {
      let inheritedToString;
      const proxy = new Proxy(orig, {
        ...traps,
        get(target, key, receiver) {
          const value = Reflect.get(target, key, receiver);
          if (key !== 'toString' || value !== nativeFunctionToString ||
              !receiver || (typeof receiver !== 'object' && typeof receiver !== 'function') ||
              receiver === proxy || Reflect.getPrototypeOf(receiver) !== proxy) return value;
          // Objects inheriting a callable are not callable themselves. Bridge
          // that invalid receiver to an equivalent native-function prototype
          // for TypeError construction, without rewriting Error.stack or the
          // shared Function.prototype.toString intrinsic. Keep repeated
          // property reads stable and honor explicitly supplied call receivers.
          if (!inheritedToString) inheritedToString = new Proxy(value, {
            apply(method, self, args) {
              if (self && typeof self === 'object' && Reflect.getPrototypeOf(self) === proxy) {
                self = Object.create(target, Object.getOwnPropertyDescriptors(self));
              }
              return Reflect.apply(method, self, args);
            }
          });
          return inheritedToString;
        },
        setPrototypeOf(target, next) {
          // Proxy forwarding otherwise tests for cycles against the target,
          // not the public callable, and can introduce a recursive chain.
          const seen = new Set();
          for (let cursor = next; cursor !== null; cursor = Reflect.getPrototypeOf(cursor)) {
            if (cursor === proxy || cursor === target || seen.has(cursor)) return false;
            seen.add(cursor);
          }
          return Reflect.setPrototypeOf(target, next);
        }
      });
      return proxy;
    }
    function hook(proto, name, handler) {
      if (!proto) return false;
      let owner = proto, desc = null;
      while (owner) {
        desc = _gopd.call(Object, owner, name);
        if (desc) break;
        owner = Object.getPrototypeOf(owner);
      }
      if (!desc || typeof desc.value !== 'function') return false;
      let seen = hookedOwners.get(owner);
      if (seen && seen.has(name)) return true;
      const orig  = desc.value;
      const proxy = callableProxy(orig, {
        apply: (t, self, args) => handler(t, self, args, orig)
      });
      try {
        Object.defineProperty(owner, name, {
          value: proxy,
          writable:     !!desc.writable,
          enumerable:   !!desc.enumerable,
          configurable: !!desc.configurable
        });
      } catch (e) { return false; }
      if (!seen) { seen = new Set(); hookedOwners.set(owner, seen); }
      seen.add(name);
      return true;
    }

    function hookGetter(proto, name, transform) {
      if (!proto) return;
      const desc = _gopd(proto, name);
      if (!desc || typeof desc.get !== 'function' || !desc.configurable) return;
      Object.defineProperty(proto, name, {...desc, get: callableProxy(desc.get, {
        apply(target, self, args) {
          return transform(Reflect.apply(target, self, args), self);
        }
      })});
    }

    // Keep the engine's Function.prototype.toString intact. Callable proxies
    // already have a native representation; overriding this shared intrinsic
    // changes error behavior for every function in the realm.

    /* ------------------------------------------------------------------ *
     * Shared GPU profile table.
     * ------------------------------------------------------------------ */

    const GPU_LIMITS = {
      maxTextureDimension1D: 8192, maxTextureDimension2D: 8192,
      maxTextureDimension3D: 2048, maxTextureArrayLayers: 256,
      maxBindGroups: 4, maxBindGroupsPlusVertexBuffers: 24,
      maxBindingsPerBindGroup: 1000,
      maxDynamicUniformBuffersPerPipelineLayout: 8,
      maxDynamicStorageBuffersPerPipelineLayout: 4,
      maxSampledTexturesPerShaderStage: 16, maxSamplersPerShaderStage: 16,
      maxStorageBuffersPerShaderStage: 8,
      maxStorageBuffersInVertexStage: 8, maxStorageBuffersInFragmentStage: 8,
      maxStorageTexturesPerShaderStage: 4,
      maxStorageTexturesInVertexStage: 4, maxStorageTexturesInFragmentStage: 4,
      maxUniformBuffersPerShaderStage: 12,
      maxUniformBufferBindingSize: 65536,
      maxStorageBufferBindingSize: 134217728,
      minUniformBufferOffsetAlignment: 256, minStorageBufferOffsetAlignment: 256,
      maxVertexBuffers: 8, maxBufferSize: 268435456,
      maxVertexAttributes: 16, maxVertexBufferArrayStride: 2048,
      maxInterStageShaderVariables: 16, maxColorAttachments: 8,
      maxColorAttachmentBytesPerSample: 32,
      maxComputeWorkgroupStorageSize: 16384,
      maxComputeInvocationsPerWorkgroup: 256,
      maxComputeWorkgroupSizeX: 256, maxComputeWorkgroupSizeY: 256,
      maxComputeWorkgroupSizeZ: 64, maxComputeWorkgroupsPerDimension: 65535,
      maxImmediateSize: 64
    };

    const GPU_FEATURES_COMMON = [
      'core-features-and-limits',
      'subgroups',
      'depth-clip-control',
      'depth32float-stencil8',
      'timestamp-query',
      'indirect-first-instance',
      'shader-f16',
      'rg11b10ufloat-renderable',
      'bgra8unorm-storage',
      'float32-filterable',
      'dual-source-blending',
      'clip-distances'
    ];
    const GPU_FEATURES_BY_VENDOR = {
      NVIDIA: ['texture-compression-bc', 'texture-compression-etc2', 'texture-compression-astc'],
      AMD:    ['texture-compression-bc', 'texture-compression-etc2', 'texture-compression-astc'],
      Intel:  ['texture-compression-bc', 'texture-compression-etc2', 'texture-compression-astc'],
      Apple:  ['texture-compression-astc', 'texture-compression-etc2']
    };

    // Alias a model within its observed hardware family. Keep the actual
    // vendor, graphics backend, and capability tuple; a different vendor's
    // name cannot turn this device into that vendor's implementation.
    const RENDERER_FAMILIES = [
      [/NVIDIA GeForce RTX 30\d{2}(?: Ti)?/i, ['NVIDIA GeForce RTX 3060 Ti', 'NVIDIA GeForce RTX 3070', 'NVIDIA GeForce RTX 3070 Ti', 'NVIDIA GeForce RTX 3080', 'NVIDIA GeForce RTX 3080 Ti']],
      [/NVIDIA GeForce RTX 40\d{2}(?: Ti| SUPER)?/i, ['NVIDIA GeForce RTX 4060', 'NVIDIA GeForce RTX 4060 Ti', 'NVIDIA GeForce RTX 4070', 'NVIDIA GeForce RTX 4080']],
      [/NVIDIA GeForce RTX 20\d{2}(?: Ti| SUPER)?/i, ['NVIDIA GeForce RTX 2060', 'NVIDIA GeForce RTX 2060 SUPER', 'NVIDIA GeForce RTX 2070', 'NVIDIA GeForce RTX 2080']],
      [/NVIDIA GeForce GTX 16\d{2}(?: Ti| SUPER)?/i, ['NVIDIA GeForce GTX 1650', 'NVIDIA GeForce GTX 1660', 'NVIDIA GeForce GTX 1660 Ti', 'NVIDIA GeForce GTX 1660 SUPER']],
      [/AMD Radeon RX 6\d{3}(?: XT)?/i, ['AMD Radeon RX 6600', 'AMD Radeon RX 6600 XT', 'AMD Radeon RX 6700 XT', 'AMD Radeon RX 6800']],
      [/AMD Radeon RX 7\d{3}(?: XT| XTX)?/i, ['AMD Radeon RX 7600', 'AMD Radeon RX 7700 XT', 'AMD Radeon RX 7800 XT', 'AMD Radeon RX 7900 XT']],
      [/AMD Radeon RX 5\d{2}/i, ['AMD Radeon RX 550', 'AMD Radeon RX 560', 'AMD Radeon RX 570', 'AMD Radeon RX 580']],
      [/Intel\(R\) UHD Graphics 6[23]0/i, ['Intel(R) UHD Graphics 620', 'Intel(R) UHD Graphics 630']],
      [/Apple M1(?: Pro| Max)?/i, ['Apple M1', 'Apple M1 Pro', 'Apple M1 Max']],
    ];
    function rendererAlias(native) {
      if (typeof native !== 'string' || /laptop|mobile|max-q/i.test(native)) return native;
      if (PROFILE_VALUES.gpu) return PROFILE_VALUES.gpu;
      for (const [pattern, models] of RENDERER_FAMILIES) {
        const match = native.match(pattern);
        if (!match) continue;
        const choices = models.filter(model => model.toLowerCase() !== match[0].toLowerCase());
        const model = choices[(fin(SEED) >>> 0) % choices.length];
        // Preserve the renderer format with a verified model/device pair
        // when available, never the physical device ID of a different model.
        const alias = native.replace(pattern, model);
        return GPU_DEVICE_IDS[model]
          ? alias.replace(/\(0x[0-9a-f]+\)/ig, '(0x0000' + GPU_DEVICE_IDS[model] + ')')
          : alias.replace(/\s*\(0x[0-9a-f]+\)/ig, '');
      }
      // Unknown families retain genuine identity rather than an invented
      // platform/vendor combination. Other enabled transforms still apply.
      return native;
    }


    /* ------------------------------------------------------------------ *
     * Audio profile table. Selected from the same seed hash as the GPU
     * profile so a real device and its reported audio path remain
     * internally consistent. These are plausible desktop values, not
     * universal truths — the values are only substituted when the native
     * getter returns a degenerate answer (missing, NaN, or zero).
     * ------------------------------------------------------------------ */

    const AUDIO_PROFILES = [
      { sampleRate: 44100, outputLatency: 0.010, baseLatency: 0.005,
        maxChannelCount: 2 },
      { sampleRate: 48000, outputLatency: 0.012, baseLatency: 0.006,
        maxChannelCount: 2 },
      { sampleRate: 44100, outputLatency: 0.015, baseLatency: 0.008,
        maxChannelCount: 2 },
      { sampleRate: 48000, outputLatency: 0.008, baseLatency: 0.004,
        maxChannelCount: 2 }
    ];
    const A = AUDIO_PROFILES[(fin(SEED ^ 0x5A17) >>> 0) % AUDIO_PROFILES.length];

    const PROFILE_GPU_FEATURES = new Set(
      GPU_FEATURES_COMMON.concat(...Object.values(GPU_FEATURES_BY_VENDOR))
    );

    /* ------------------------------------------------------------------ *
     * 2D canvas
     * ------------------------------------------------------------------ */

    function install2D() {
      // Change the drawing inputs, not the bytes returned by reads. This
      // leaves exact pixel writes, crops, copies, exports and float reads
      // observing the same rendered bitmap, including transparent pixels.
      const offset = (salt) => (((fin(SEED ^ salt) >>> 0) / 4294967296) - 0.5) * NOISE_RATE / 2;
      const dx = offset(0x6A09E667), dy = offset(0xBB67AE85);
      const numeric = (args, count) => args.length >= count &&
        args.slice(0, count).every(v => typeof v === 'number' && Number.isFinite(v));
      function coordinates(proto, name, count, indices, valid = () => true) {
        hook(proto, name, (t, self, args) => {
          // Delegate non-primitive inputs untouched: native WebIDL must own
          // coercion, exception order, and one-time consumption of getters.
          if (!numeric(args, count) || !valid(args)) return Reflect.apply(t, self, args);
          const changed = args.slice();
          for (const [index, delta] of indices) changed[index] += delta;
          return Reflect.apply(t, self, changed);
        });
      }
      for (const ctor of [win.CanvasRenderingContext2D, win.OffscreenCanvasRenderingContext2D]) {
        const proto = ctor && ctor.prototype;
        if (!proto) continue;
        coordinates(proto, 'createLinearGradient', 4, [[0, dx], [1, dy], [2, dx], [3, dy]],
          a => a[0] !== a[2] || a[1] !== a[3]);
        coordinates(proto, 'createRadialGradient', 6, [[0, dx], [1, dy], [3, dx], [4, dy]],
          a => a[2] >= 0 && a[5] >= 0 && (a[0] !== a[3] || a[1] !== a[4] || a[2] !== a[5]));
        coordinates(proto, 'createConicGradient', 3, [[1, dx], [2, dy]]);
      }
      for (const ctor of [win.CanvasRenderingContext2D, win.OffscreenCanvasRenderingContext2D, win.Path2D]) {
        const proto = ctor && ctor.prototype;
        if (!proto) continue;
        coordinates(proto, 'bezierCurveTo', 6, [[0, dx], [1, dy], [2, dx], [3, dy]]);
        coordinates(proto, 'quadraticCurveTo', 4, [[0, dx], [1, dy]]);
        // Preserve analytic primitives (arc/ellipse/rect), endpoints, exact
        // pixel writes, and text metrics. Only gradient coordinates and
        // Bezier control points receive the seeded drawing offset.
      }
    }


    /* ------------------------------------------------------------------ *
     * WebGL
     * ------------------------------------------------------------------ */

    function installWebGL() {
      const gl1 = win.WebGLRenderingContext;
      const gl2 = win.WebGL2RenderingContext;
      if (!gl1 && !gl2) return;

      const nativeExtensions1 = gl1 ? gl1.prototype.getSupportedExtensions : null;
      const nativeExtensions2 = gl2 ? gl2.prototype.getSupportedExtensions : null;

      const getParamHandler = (query) => (t, self, args) => {
        if (!args.length) return Reflect.apply(t, self, args);
        Reflect.apply(query, self, []);
        const pname = (+args[0]) >>> 0;
        const native = Reflect.apply(t, self, [pname]);
        return pname === 0x9246 ? rendererAlias(native) : native;
      };

      // Perturb the rendered color, not readback bytes. Every read path (PBO,
      // float, crop, copy and canvas export) then observes the same framebuffer.
      // Restrict rewriting to simple fragment shaders; complex preprocessors,
      // multiple render targets, integer outputs and vertex shaders pass through.
      function rewriteFragment(source) {
        const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\r\n]*/g, match => match.replace(/[^\r\n]/g, ' '));
        if (/^\s*#\s*(?!version\b|extension\b|line\b)\w+/m.test(code)) return null;
        if (!/\bvarying\b/.test(code) && !/\bin\s+(?:(?:lowp|mediump|highp)\s+)?vec[234]\b/.test(code)) return null;
        // Texture copies and shaders using only analytic expressions have no
        // interpolated-color surface to vary. Preserve their exact results.
        if (/\btexture\w*\s*\(/.test(code)) return null;
        let output = 'gl_FragColor';
        if (/^\s*#\s*version\s+300\b/m.test(code)) {
          const outputs = [...code.matchAll(/\bout\s+(?:(?:lowp|mediump|highp)\s+)?vec4\s+(\w+)\s*;/g)];
          if (outputs.length !== 1 || (code.match(/\bout\b/g) || []).length !== 1) return null;
          output = outputs[0][1];
        } else if (/\bgl_FragData\b/.test(code) || !/\bgl_FragColor\b/.test(code)) return null;
        const mains = [...code.matchAll(/\bvoid\s+(main)\s*\(\s*(?:void\s*)?\)\s*\{/g)];
        if (mains.length !== 1) return null;
        const name = 'retail_profile_main_' + (SEED >>> 0);
        if (code.includes(name)) return null;
        const start = mains[0].index + mains[0][0].indexOf('main');
        const delta = [0x18273,0x82731,0x73182].map(salt =>
          ((((fin(SEED ^ salt) >>> 0) / 4294967296) - .5) * GL_NOISE_RATE / 16).toFixed(10));
        return source.slice(0,start) + name + source.slice(start+4) +
          '\nvoid main(){' + name + '();' +
          'if(all(greaterThan(' + output + '.rgb,vec3(0.0)))&&all(lessThan(' + output + '.rgb,vec3(1.0)))){' +
          output + '.rgb+=vec3(' + delta.join(',') + ')*' + output + '.rgb*(vec3(1.0)-' + output + '.rgb);}}\n';
      }
      function installShaderVariation(proto) {
        const getShaderParameter = proto.getShaderParameter;
        const getShaderSource = proto.getShaderSource;
        const shaderSource = proto.shaderSource;
        hook(proto, 'compileShader', (t,self,args) => {
          // Compile the caller's source first: native validation, errors and
          // original shader failures must retain their normal behavior.
          const result = Reflect.apply(t,self,args);
          const [shader] = args;
          if (!shader || typeof shader !== 'object') return result;
          if (Reflect.apply(getShaderParameter,self,[shader,0x8B4F]) !== 0x8B30 ||
              !Reflect.apply(getShaderParameter,self,[shader,0x8B81])) return result;
          const source = Reflect.apply(getShaderSource,self,[shader]);
          if (typeof source !== 'string') return result;
          const changed = rewriteFragment(source);
          if (changed === null) return result;
          Reflect.apply(shaderSource,self,[shader,changed]);
          let compiled = false;
          try {
            Reflect.apply(t,self,args);
            compiled = !!Reflect.apply(getShaderParameter,self,[shader,0x8B81]);
          } finally {
            // glShaderSource stores text without replacing the compiled shader.
            // Keep source queries and later caller edits entirely native.
            Reflect.apply(shaderSource,self,[shader,source]);
          }
          if (!compiled) Reflect.apply(t,self,args);
          return result;
        });
      }

      if (gl1) {
        if (PROFILE_VALUES.gpu_choice !== 'native') hook(gl1.prototype, 'getParameter', getParamHandler(nativeExtensions1));
        if (ENABLE_WEBGL && GL_NOISE_RATE > 0) installShaderVariation(gl1.prototype);
      }
      if (gl2) {
        if (PROFILE_VALUES.gpu_choice !== 'native') hook(gl2.prototype, 'getParameter', getParamHandler(nativeExtensions2));
        if (ENABLE_WEBGL && GL_NOISE_RATE > 0) installShaderVariation(gl2.prototype);
      }
    }

    /* ------------------------------------------------------------------ *
     * WebGPU
     * ------------------------------------------------------------------ */

    function installWebGPU() {
      if (PROFILE_VALUES.webgpu_limits === 'native' && PROFILE_VALUES.gpu_choice === 'native') return;
      const GPU = win.GPU;
      const GPUAdapter = win.GPUAdapter;
      if (!GPU || !GPU.prototype) return;
      if (typeof GPU.prototype.requestAdapter !== 'function') return;

      const MIN_LIMITS = new Set([
        'minUniformBufferOffsetAlignment', 'minStorageBufferOffsetAlignment'
      ]);

      function clampLimit(name, native, cap) {
        if (typeof native !== 'number') return cap;
        if (typeof cap !== 'number') return native;
        return MIN_LIMITS.has(name) ? Math.max(native, cap) : Math.min(native, cap);
      }

      function makeOperationError(msg) {
        try { if (typeof win.OperationError === 'function') return new win.OperationError(msg); } catch (e) {}
        try { return new win.DOMException(msg, 'OperationError'); }
        catch (e) {
          const err = new Error(msg);
          err.name = 'OperationError';
          return err;
        }
      }

      // Retain genuine platform objects. Replacing adapters, devices, or
      // their metadata with Proxies loses the receiver's WebIDL brand.
      const infoProto = win.GPUAdapterInfo && win.GPUAdapterInfo.prototype;
      for (const key of ['device', 'description']) {
        hookGetter(infoProto, key, () => '');
      }
      if (!ENABLE_WEBGPU || PROFILE_VALUES.webgpu_limits === 'native') return;
      const limitsProto = win.GPUSupportedLimits && win.GPUSupportedLimits.prototype;
      for (const key of Object.keys(GPU_LIMITS)) {
        hookGetter(limitsProto, key, native => clampLimit(key, native, GPU_LIMITS[key]));
      }

      const featureProto = win.GPUSupportedFeatures && win.GPUSupportedFeatures.prototype;
      if (featureProto) {
        const nativeValues = featureProto.values;
        const nativeSize = _gopd(featureProto, 'size').get;
        const keptCache = new WeakMap();
        function kept(self) {
          // Validate every receiver even if a cached view exists. The native
          // set is immutable; cached membership is safe for its lifetime.
          Reflect.apply(nativeSize, self, []);
          if (!keptCache.has(self)) {
            keptCache.set(self, new Set(Array.from(Reflect.apply(nativeValues, self, []))
              .filter(value => PROFILE_GPU_FEATURES.has(value))));
          }
          return keptCache.get(self);
        }
        hookGetter(featureProto, 'size', (native, self) => kept(self).size);
        hook(featureProto, 'has', (t, self, args) => {
          const features = kept(self);
          if (!args.length) return Reflect.apply(t, self, args);
          // DOMString conversion once, including Symbol rejection.
          const key = `${args[0]}`;
          return Reflect.apply(t, self, [key]) && features.has(key);
        });
        for (const key of ['keys', 'values', 'entries', Symbol.iterator]) {
          hook(featureProto, key, (t, self, args) => {
            const features = kept(self);
            return key === 'entries' ? features.entries() : features.values();
          });
        }
        hook(featureProto, 'forEach', (t, self, args) => {
          kept(self);
          const callback = args[0], thisArg = args[1];
          if (typeof callback !== 'function') return Reflect.apply(t, self, args);
          return Reflect.apply(t, self, [function(value, key) {
            if (PROFILE_GPU_FEATURES.has(value)) Reflect.apply(callback, thisArg, [value, key, self]);
          }]);
        });
      }

      if (GPUAdapter && GPUAdapter.prototype) {
        hook(GPUAdapter.prototype, 'requestDevice', (t, self, args) => {
          const input = args[0];
          if (input == null || (typeof input !== 'object' && typeof input !== 'function')) {
            return Reflect.apply(t, self, args);
          }
          // Let native WebIDL conversion determine dictionary access order,
          // receiver errors, defaults, enum conversion, and iterator closing.
          // These temporary argument views never replace returned platform objects.
          const desc = new Proxy(Object.create(null), {
            get(_, key) {
              const value = Reflect.get(input, key, input);
              if (key === 'requiredFeatures' && value != null &&
                  (typeof value === 'object' || typeof value === 'function')) {
                return {
                  *[Symbol.iterator]() {
                    for (const feature of value) {
                      const name = `${feature}`;
                      if (!self.features.has(name)) throw new TypeError('Unsupported required feature: ' + name);
                      yield name;
                    }
                  }
                };
              }
              if (key === 'requiredLimits' && value != null &&
                  (typeof value === 'object' || typeof value === 'function')) {
                return new Proxy(Object.create(null), {
                  ownKeys() { return Reflect.ownKeys(value); },
                  getOwnPropertyDescriptor(_, name) {
                    const d = Reflect.getOwnPropertyDescriptor(value, name);
                    return d ? {configurable: true, enumerable: d.enumerable} : undefined;
                  },
                  get(_, name) {
                    const raw = Reflect.get(value, name, value);
                    if (raw === undefined) return undefined;
                    const number = +raw;
                    // Invalid GPUSize64 values are rejected by native conversion.
                    const requested = Math.trunc(number);
                    if (Number.isFinite(number) && requested >= 0 && requested < 2 ** 64 &&
                        Object.prototype.hasOwnProperty.call(GPU_LIMITS, name)) {
                      const limit = self.limits[name];
                      if (MIN_LIMITS.has(name) ? requested < limit : requested > limit) {
                        throw makeOperationError('requiredLimits.' + name + ' exceeds the advertised limit');
                      }
                    }
                    return number;
                  }
                });
              }
              return value;
            }
          });
          return Reflect.apply(t, self, [desc]);
        });
      }

      // requestAdapter and all adapter/device object getters stay native.
      // Prototype policy also applies to legacy requestAdapterInfo results.
    }

    /* ------------------------------------------------------------------ *
     * AudioContext
     * ------------------------------------------------------------------ */

    // Locate a getter on the prototype chain and wrap it. The wrapper
    // returns the native value when it is usable and the profile value
    // otherwise. Reflection exposes the actual installed getter.
    function replaceIfDegenerate(proto, name, fallback, isValid) {
      let owner = proto, desc = null;
      while (owner) {
        desc = _gopd.call(Object, owner, name);
        if (desc) break;
        owner = Object.getPrototypeOf(owner);
      }
      if (!desc || typeof desc.get !== 'function') return false;
      const orig = desc.get;
      let wrapped;
      try {
        wrapped = callableProxy(orig, {
          apply(target, self, args) {
            // Invalid receivers must retain the native WebIDL TypeError.
            // Fallbacks apply only to successfully read degenerate values.
            const native = Reflect.apply(target, self, args);
            return isValid(native) ? native : fallback;
          }
        });
      } catch (e) { return false; }
      try {
        Object.defineProperty(owner, name, {
          get: wrapped,
          configurable: desc.configurable,
          enumerable: desc.enumerable
        });
      } catch (e) { return false; }
      return true;
    }

    function installAudio() {
      const AC  = win.AudioContext        || win.webkitAudioContext;
      const OAC = win.OfflineAudioContext || win.webkitOfflineAudioContext;
      const BAC = win.BaseAudioContext;
      if (!AC && !OAC) return;

      const isNumber = (v) => typeof v === 'number' && Number.isFinite(v);
      const isPositive = (v) => isNumber(v) && v > 0;
      const isNonNegative = (v) => isNumber(v) && v >= 0;

      // sampleRate is inherited from BaseAudioContext by both real-time and
      // offline contexts. Wrap it once at the shared base prototype when
      // available to avoid stacking wrappers through both inheritance paths.
      if (BAC && BAC.prototype) {
        replaceIfDegenerate(BAC.prototype, 'sampleRate', A.sampleRate, isPositive);
      } else {
        if (AC && AC.prototype) {
          replaceIfDegenerate(AC.prototype, 'sampleRate', A.sampleRate, isPositive);
        }
        if (OAC && OAC.prototype) {
          replaceIfDegenerate(OAC.prototype, 'sampleRate', A.sampleRate, isPositive);
        }
      }

      // baseLatency and outputLatency belong to AudioContext, not
      // BaseAudioContext. A zero latency is a valid native result and must
      // therefore be preserved.
      if (AC && AC.prototype) {
        replaceIfDegenerate(AC.prototype, 'outputLatency', A.outputLatency, isNonNegative);
        replaceIfDegenerate(AC.prototype, 'baseLatency',   A.baseLatency,   isNonNegative);
      }

      // maxChannelCount is an unsigned value. Preserve every finite
      // non-negative native result, including mono devices and offline
      // contexts whose channel capacity depends on the runtime.
      const ADN = win.AudioDestinationNode;
      if (ADN && ADN.prototype) {
        replaceIfDegenerate(ADN.prototype, 'maxChannelCount',
                            A.maxChannelCount, isNonNegative);
      }

      // DynamicsCompressorNode.reduction is graph state, not device
      // metadata. Leave it completely native.
    }

    /* ------------------------------------------------------------------ *
     * Worker / SharedWorker constructor interception
     * ------------------------------------------------------------------ */

    function installWorkers() {
      const W = win.Worker;
      const SW = win.SharedWorker;
      if (!W && !SW) return;
      if (!win.URL || !win.Blob) return;
      const created = new Set();
      let bootUrl = null;
      try {
        bootUrl = win.URL.createObjectURL(new win.Blob(['(' + AFP_BOOTSTRAP.toString() + ')();'], { type: 'text/javascript' }));
        created.add(bootUrl);
      } catch (e) { return; }
      if (!bootUrl) return;

      function inlineDataUrl(raw) {
        const s = String(raw);
        const comma = s.indexOf(',');
        if (comma < 0) return null;
        const meta = s.slice(5, comma);
        const payload = s.slice(comma + 1);
        let src;
        try { if (/;base64/i.test(meta)) src = win.atob(payload); else src = decodeURIComponent(payload); } catch (e) { return null; }
        const merged = '(' + AFP_BOOTSTRAP.toString() + ')();\n' + src;
        return 'data:text/javascript;charset=utf-8,' + encodeURIComponent(merged);
      }

      function wrapUrl(raw, isModule) {
        if (typeof raw !== 'string') return null;
        if (raw.slice(0, 5) === 'data:') return inlineDataUrl(raw);
        let u;
        try { u = new win.URL(raw, win.location && win.location.href); } catch (e) { return null; }
        if (created.has(u.href)) return null;
        const proto = u.protocol;
        if (proto !== 'http:' && proto !== 'https:' && proto !== 'blob:') return null;
        if (win.location && u.origin !== win.location.origin) return null;
        let src;
        if (isModule) {
          src = 'import ' + JSON.stringify(bootUrl) + ';\n' + 'await import(' + JSON.stringify(u.href) + ');\n';
        } else {
          src = 'importScripts(' + JSON.stringify(bootUrl) + ');\n' + 'importScripts(' + JSON.stringify(u.href) + ');\n';
        }
        try {
          const b = win.URL.createObjectURL(new win.Blob([src], { type: 'text/javascript' }));
          created.add(b);
          return b;
        } catch (e) { return null; }
      }

      function makeProxy(orig, getModuleFlag) {
        const proxy = callableProxy(orig, {
          construct(t, args, newTarget) {
            try {
              const isModule = getModuleFlag(args);
              const wrapped = wrapUrl(args[0], isModule);
              if (wrapped) {
                const a = args.slice();
                a[0] = wrapped;
                return Reflect.construct(t, a, newTarget);
              }
            } catch (e) {}
            return Reflect.construct(t, args, newTarget);
          }
        });
        return proxy;
      }

      if (W) { try { win.Worker = makeProxy(W, (args) => !!(args[1] && args[1].type === 'module')); } catch (e) {} }
      if (SW) { try { win.SharedWorker = makeProxy(SW, (args) => !!(args[1] && args[1].type === 'module')); } catch (e) {} }
    }

    /* ------------------------------------------------------------------ *
     * Install selected surfaces
     * ------------------------------------------------------------------ */

    /* ------------------------------------------------------------------ */

    function installNavigator() {
      for (const proto of [win.Navigator?.prototype, win.WorkerNavigator?.prototype]) {
        hookGetter(proto, 'hardwareConcurrency', native => {
          if (!Number.isInteger(native) || native < 2) return native;
          if (PROFILE_VALUES.cpu) return Math.min(native, PROFILE_VALUES.cpu);
          const choices = [2, 4, 8, 12, 16].filter(value => value <= native);
          return choices[(fin(SEED ^ 0x37A15) >>> 0) % choices.length];
        });
        hookGetter(proto, 'deviceMemory', native => {
          // A 64-bit browser heap can exceed 4 GB. Do not claim a desktop
          // memory bucket smaller than that engine capacity, or exceed native.
          if (typeof native !== 'number' || native < 8) return native;
          if (PROFILE_VALUES.memory) return Math.min(native, PROFILE_VALUES.memory);
          const choices = [8, 16, 32].filter(value => value <= native);
          return choices[(fin(SEED ^ 0x96B31) >>> 0) % choices.length];
        });
      }
    }

    function installFontEnumeration() {
      // Filter only native FontData objects after the real permission check.
      // Keep each family together; never invent a font or change its bytes.
      hook(win, 'queryLocalFonts', (target, self, args) =>
        Reflect.apply(target, self, args).then(fonts => fonts.filter(font => {
          const family = String(font.family).toLowerCase();
          if (PROFILE_VALUES.font_mode === 'native') return true;
          if (PROFILE_VALUES.fonts) return PROFILE_VALUES.fonts.some(name => name.toLowerCase() === family);
          if (['arial','times new roman','courier new','segoe ui','segoe ui emoji'].includes(family)) return true;
          let hash = SEED ^ 0x491FC;
          for (let i=0; i<family.length; i++) hash = Math.imul(hash ^ family.charCodeAt(i), 16777619);
          return (fin(hash) >>> 0) % 3 !== 0;
        })));
    }

    if (ENABLE_NAVIGATOR) installNavigator();
    if (ENABLE_FONTS) installFontEnumeration();
    if (ENABLE_2D)      install2D();
    if (ENABLE_WEBGL || ENABLE_WEBGPU) installWebGL();
    if (ENABLE_WEBGL || ENABLE_WEBGPU) installWebGPU();
    if (ENABLE_AUDIO)   installAudio();
    if (ENABLE_WORKERS) installWorkers();
  }

  AFP_BOOTSTRAP();
})();
"""


def build_scripts(
    seed_int: int,
    *,
    perturb_canvas: bool = False,
    spoof_webgl: bool = False,
    spoof_webgpu: bool = False,
    spoof_audio: bool = False,
    intercept_workers: bool = False,
    spoof_navigator: bool = False,
    restrict_fonts: bool = False,
    perturb_float_readback: bool = False,
    profile_values: dict | None = None,
) -> list[str]:
    """Return selected compatibility init scripts for one account seed.

    Transformations are opt-in and independent of browser launch mode.
    Either GPU flag enables shared GPU identity policy; rendering
    and WebGPU capability policies remain separately selectable.

    ``perturb_canvas``        Gradient/Bezier drawing offsets; native bitmap
                              reads and exports, including float16 data.
    ``spoof_webgl``           Hardware-family renderer alias, WebGPU model
                              redaction, interpolated fragment-color variation.
    ``spoof_webgpu``          Same identity policy plus adapter/device
                              limit and feature restrictions.
    ``spoof_audio``           Conservative AudioContext metadata fallback.
                              Positive sample rates and all finite
                              non-negative latency/channel-count values are
                              preserved, including zero latency, mono output,
                              and native OfflineAudioContext channel capacity.
                              Rendered audio and compressor state stay native.
    ``intercept_workers``     Wrap Worker and SharedWorker constructors,
                              including data: sources.
    ``spoof_navigator``       Seeded CPU/memory buckets bounded by native values.
    ``restrict_fonts``        Permission-gated native local-font subset; font
                              rendering, FontFace and glyph metrics unchanged.
    ``perturb_float_readback`` Legacy compatibility argument; readback stays
                              native for every format. Use spoof_webgl to
                              enable the framebuffer drawing variation.

    Register once with ``BrowserContext.add_init_script``. By itself, cross-origin
    workers, service workers, and any worker started outside the wrapped
    constructors are not covered. The application supplies WorkerProfiles
    separately for complete graphics-worker startup. All readbacks observe the
    rendered framebuffer, including floating-point, PBO and packed reads.
    WebGPU device-level readback is not perturbed.
    """
    if not (perturb_canvas or spoof_webgl or spoof_webgpu or spoof_audio
            or intercept_workers or perturb_float_readback or spoof_navigator or restrict_fonts):
        return []

    seed = int(seed_int) & 0xFFFFFFFF
    js = CANVAS_JS_TEMPLATE
    js = js.replace("__SEED__",             str(seed))
    js = js.replace("__NOISE_RATE__",       str((profile_values or {}).get("canvas_noise", 0.5)))
    js = js.replace("__GL_NOISE_RATE__",    str((profile_values or {}).get("webgl_noise", 0.25)))
    js = js.replace("__ENABLE_2D__",        "true" if perturb_canvas         else "false")
    js = js.replace("__ENABLE_WEBGL__",     "true" if spoof_webgl            else "false")
    js = js.replace("__ENABLE_WEBGPU__",    "true" if spoof_webgpu           else "false")
    js = js.replace("__ENABLE_AUDIO__",     "true" if spoof_audio            else "false")
    js = js.replace("__ENABLE_WORKERS__",   "true" if intercept_workers      else "false")
    js = js.replace("__ENABLE_NAVIGATOR__", "true" if spoof_navigator        else "false")
    js = js.replace("__ENABLE_FONTS__",     "true" if restrict_fonts         else "false")
    js = js.replace("__PROFILE_VALUES__", json.dumps(profile_values or {}))
    js = js.replace("__GPU_DEVICE_IDS__", json.dumps(GPU_DEVICE_IDS))
    return [js]


def build_worker_script(seed_int: int, **kwargs) -> str:
    """Return a bootstrap for an owned classic/module worker entrypoint.

    Execute once before creating contexts, using the document's account
    seed. Workers created through the wrapped constructor receive this
    automatically; this function is for workers the operator starts
    outside that path. Re-running in the same realm is a no-op because
    the marker symbol derives from the same seed.

    Keyword arguments mirror ``build_scripts``. Pass the same flags the
    page uses so worker rendering agrees with the parent document.
    Worker interception is forced off because this code already runs
    inside a worker; nested Worker construction is not covered.
    """
    kwargs['intercept_workers'] = False
    scripts = build_scripts(seed_int, **kwargs)
    return scripts[0] if scripts else ''
