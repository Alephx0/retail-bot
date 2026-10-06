"""Seeded 2D canvas + WebGL + WebGPU + AudioContext perturbation and identity.

``build_scripts`` gates everything behind explicit opt-in. ``build_worker_script``
supplies the same implementation for worker entrypoints the caller controls.
Both must run before application code touches canvas, WebGL, WebGPU, or audio.

Design rules:

  - Transformations are explicit opt-ins, independent of browser launch mode.
    Either GPU flag enables coherent identity policy across both GPU APIs.
    WebGL readback and WebGPU feature/limit policies retain separate flags.
  - Canvas perturbation changes gradient coordinates and Bezier control
    points during drawing. Readback, copying and export methods stay native
    and observe one bitmap. Exact pixel writes, analytic primitives, path
    endpoints, text metrics and non-primitive argument coercions stay native.
    Seeded drawing offsets replace the legacy canvas readback algorithm.
  - WebGL pixel perturbation is content-keyed, never read-size-keyed.
    A neighbour-based heuristic selects candidate blended/edge pixels; it
    cannot distinguish every explicitly painted pixel from rasterized output.
    The byte, half-float, and float paths use
    the same eligibility predicate: neighbour-inequality on the full RGBA
    tuple, alpha range check, premultiplied colour range check, and
    horizontal-or-vertical boundary handling.
  - WebGL renderer aliases stay within the observed hardware family. Vendor,
    capabilities, extensions and shader precision remain native. Unknown and
    mobile families retain native identity. This does not emulate another GPU.
  - WebGPU vendor/architecture remain genuine; device/description are redacted
    when either GPU flag is enabled. Adapters, devices, info, limits, and feature
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
  - Full-frame and (0,0) reads are perturbed. The padded region is clipped
    to the framebuffer; missing neighbours simply do not enter the hash.
  - WebGL2 readPixels dstOffset is supported via a subarray view.
  - OffscreenCanvas transfers and both byte/float 2D readbacks use the
    unmodified platform implementation on the transformed bitmap.
  - The readPixels pack-state guard reads native getParameter (captured
    before hooking) and fails closed.
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
native GPU rasterization/shader semantics, depth/stencil readback
perturbation, PBO-offset readback perturbation, non-tight pack-state
emulation, WebGPU device-level readback perturbation, native callable
identity for modified methods/accessors, and timing side channels.
"""
from __future__ import annotations


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
    const ENABLE_FLOATGL  = __ENABLE_FLOATGL__;

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

    const pack = (d, j) =>
      (d[j] & 254) | ((d[j + 1] & 254) << 8) |
      ((d[j + 2] & 254) << 16) | ((d[j + 3] & 254) << 24);

    // U8 eligibility: neighbour pack differs from centre, alpha in range,
    // premultiplied colour in range. Pack includes all four channels.
    function blendedU8(d, i, a, b, me) {
      if (pack(d, a) === me || pack(d, b) === me) return false;
      const alpha = d[i + 3], pa = d[a + 3], qa = d[b + 3];
      if (alpha < Math.min(pa, qa) || alpha > Math.max(pa, qa)) return false;
      for (let c = 0; c < 3; c++) {
        const v = (d[i + c] & 254) * alpha;
        const p = (d[a + c] & 254) * pa, q = (d[b + c] & 254) * qa;
        if (v < (p < q ? p : q) || v > (p > q ? p : q)) return false;
      }
      return true;
    }

    function noisify(d, stride, w, h, bx, by, tx, ty, tw, th, cw, ch, rate, flip) {
      const thr = Math.max(0, Math.min(256, Math.floor(rate * 256)));
      for (let y = ty; y < ty + th; y++) {
        const Y = by + y;
        if (Y < 0 || Y >= ch) continue;
        for (let x = tx; x < tx + tw; x++) {
          const X = bx + x;
          if (X < 0 || X >= cw) continue;
          const i = y * stride + x * 4;
          if (d[i + 3] === 0) continue;
          const me = pack(d, i);
          const hasL = X > 0 && x > 0, hasR = X + 1 < cw && x + 1 < w;
          const hasU = Y > 0 && y > 0, hasD = Y + 1 < ch && y + 1 < h;
          if (!((hasL && hasR && blendedU8(d, i, i - 4, i + 4, me)) ||
                (hasU && hasD && blendedU8(d, i, i - stride, i + stride, me)))) continue;
          const hs = ((hasL ? pack(d, i - 4) : 0) + (hasR ? pack(d, i + 4) : 0)) | 0;
          const vs = ((hasU ? pack(d, i - stride) : 0) + (hasD ? pack(d, i + stride) : 0)) | 0;
          const k  = fin(fin(fin(SEED ^ me) ^ hs) ^ vs) >>> 0;
          if ((k & 255) >= thr) continue;
          const c   = ((k >>> 8) & 0xFFFF) % 3;
          const bit = (k >>> 24) & 1;
          d[i + c] = (d[i + c] & 254) | bit;
          if (flip) flip(i + c, bit);
        }
      }
    }

    const F32B = new Float32Array(1);
    const U32B = new Uint32Array(F32B.buffer);
    const f2b = (v) => { F32B[0] = v; return U32B[0]; };
    const b2f = (b) => { U32B[0] = b >>> 0; return F32B[0]; };
    const f2q = (v) => { F32B[0] = v; return U32B[0] >>> 8; };
    const maskF32Lsb = (v) => b2f(f2b(v) & 0xFFFFFFFE);

    // F32 eligibility mirrors blendedU8. The neighbour-equals-centre test
    // hashes RGBA (not just RGB), matching pack()'s four-channel inclusion.
    function blendedF32(d, i, a, b, me) {
      const qA = (f2q(d[a]) ^ Math.imul(f2q(d[a + 1]), 0x9E3779B1) ^
                  Math.imul(f2q(d[a + 2]), 0x85EBCA6B) ^
                  Math.imul(f2q(d[a + 3]), 0x27D4EB2F)) | 0;
      const qB = (f2q(d[b]) ^ Math.imul(f2q(d[b + 1]), 0x9E3779B1) ^
                  Math.imul(f2q(d[b + 2]), 0x85EBCA6B) ^
                  Math.imul(f2q(d[b + 3]), 0x27D4EB2F)) | 0;
      if (qA === me || qB === me) return false;
      const alpha = d[i + 3], pa = d[a + 3], qa = d[b + 3];
      if (alpha < Math.min(pa, qa) || alpha > Math.max(pa, qa)) return false;
      for (let c = 0; c < 3; c++) {
        const v = maskF32Lsb(d[i + c]) * alpha;
        const p = maskF32Lsb(d[a + c]) * pa, q = maskF32Lsb(d[b + c]) * qa;
        if (v < (p < q ? p : q) || v > (p > q ? p : q)) return false;
      }
      return true;
    }

    function noisifyF32(d, stride, w, h, bx, by, tx, ty, tw, th, cw, ch, rate, flip) {
      const thr = Math.max(0, Math.min(256, Math.floor(rate * 256)));
      for (let y = ty; y < ty + th; y++) {
        const Y = by + y;
        if (Y < 0 || Y >= ch) continue;
        for (let x = tx; x < tx + tw; x++) {
          const X = bx + x;
          if (X < 0 || X >= cw) continue;
          const i = y * stride + x * 4;
          if (!(d[i + 3] > 0)) continue;
          const hasL = X > 0 && x > 0, hasR = X + 1 < cw && x + 1 < w;
          const hasU = Y > 0 && y > 0, hasD = Y + 1 < ch && y + 1 < h;
          if (!((hasL && hasR) || (hasU && hasD))) continue;
          const qR = f2q(d[i]), qG = f2q(d[i + 1]), qB = f2q(d[i + 2]), qA = f2q(d[i + 3]);
          const me = (qR ^ Math.imul(qG, 0x9E3779B1) ^ Math.imul(qB, 0x85EBCA6B) ^
                      Math.imul(qA, 0x27D4EB2F)) | 0;
          let eligible = false;
          if (hasL && hasR && blendedF32(d, i, i - 4, i + 4, me)) eligible = true;
          if (!eligible && hasU && hasD && blendedF32(d, i, i - stride, i + stride, me)) eligible = true;
          if (!eligible) continue;
          const lR = hasL ? f2q(d[i - 4]) : 0;
          const rR = hasR ? f2q(d[i + 4]) : 0;
          const uR = hasU ? f2q(d[i - stride]) : 0;
          const dR = hasD ? f2q(d[i + stride]) : 0;
          const hs = (((lR + rR) | 0) ^ Math.imul((uR + dR) | 0, 0x27D4EB2F)) | 0;
          const k  = fin(fin(fin(SEED ^ me) ^ hs) ^ 0) >>> 0;
          if ((k & 255) >= thr) continue;
          const c   = ((k >>> 8) & 0xFFFF) % 3;
          const bit = (k >>> 24) & 1;
          const j = i + c;
          F32B[0] = d[j];
          U32B[0] = U32B[0] ^ bit;
          d[j] = F32B[0];
          if (flip) flip(j, bit);
        }
      }
    }

    // Half-float unpack for the eligibility check.
    function h2f(h) {
      const s = (h & 0x8000) ? -1 : 1;
      const e = (h & 0x7C00) >> 10;
      const m = h & 0x03FF;
      if (e === 0) return s * m * Math.pow(2, -24);
      if (e === 31) return m ? NaN : s * Infinity;
      return s * Math.pow(2, e - 15) * (1 + m / 1024);
    }
    const maskF16Lsb = (h) => h & 0xFFFE;

    // F16 eligibility mirrors blendedU8. The neighbour-equals-centre test
    // hashes RGBA, matching pack()'s four-channel inclusion.
    function blendedF16(d, i, a, b, me) {
      const aq0 = d[a] & 0xFFFE, aq1 = d[a + 1] & 0xFFFE,
            aq2 = d[a + 2] & 0xFFFE, aq3 = d[a + 3] & 0xFFFE;
      const bq0 = d[b] & 0xFFFE, bq1 = d[b + 1] & 0xFFFE,
            bq2 = d[b + 2] & 0xFFFE, bq3 = d[b + 3] & 0xFFFE;
      const aHash = (aq0 ^ Math.imul(aq1, 0x9E37) ^ Math.imul(aq2, 0x85EB) ^
                     Math.imul(aq3, 0x27D4)) | 0;
      const bHash = (bq0 ^ Math.imul(bq1, 0x9E37) ^ Math.imul(bq2, 0x85EB) ^
                     Math.imul(bq3, 0x27D4)) | 0;
      if (aHash === me || bHash === me) return false;
      const alpha = h2f(d[i + 3]), pa = h2f(d[a + 3]), qa = h2f(d[b + 3]);
      if (alpha < Math.min(pa, qa) || alpha > Math.max(pa, qa)) return false;
      for (let c = 0; c < 3; c++) {
        const v = h2f(maskF16Lsb(d[i + c])) * alpha;
        const p = h2f(maskF16Lsb(d[a + c])) * pa;
        const q = h2f(maskF16Lsb(d[b + c])) * qa;
        if (v < (p < q ? p : q) || v > (p > q ? p : q)) return false;
      }
      return true;
    }

    function noisifyF16(d, stride, w, h, bx, by, tx, ty, tw, th, cw, ch, rate, flip) {
      const thr = Math.max(0, Math.min(256, Math.floor(rate * 256)));
      for (let y = ty; y < ty + th; y++) {
        const Y = by + y;
        if (Y < 0 || Y >= ch) continue;
        for (let x = tx; x < tx + tw; x++) {
          const X = bx + x;
          if (X < 0 || X >= cw) continue;
          const i = y * stride + x * 4;
          if ((d[i + 3] & 0x7FFF) === 0) continue;
          const hasL = X > 0 && x > 0, hasR = X + 1 < cw && x + 1 < w;
          const hasU = Y > 0 && y > 0, hasD = Y + 1 < ch && y + 1 < h;
          if (!((hasL && hasR) || (hasU && hasD))) continue;
          const qR = d[i] & 0xFFFE, qG = d[i + 1] & 0xFFFE,
                qB = d[i + 2] & 0xFFFE, qA = d[i + 3] & 0xFFFE;
          const me = (qR ^ Math.imul(qG, 0x9E37) ^ Math.imul(qB, 0x85EB) ^
                      Math.imul(qA, 0x27D4)) | 0;
          let eligible = false;
          if (hasL && hasR && blendedF16(d, i, i - 4, i + 4, me)) eligible = true;
          if (!eligible && hasU && hasD && blendedF16(d, i, i - stride, i + stride, me)) eligible = true;
          if (!eligible) continue;
          const lR = hasL ? (d[i - 4] & 0xFFFE) : 0;
          const rR = hasR ? (d[i + 4] & 0xFFFE) : 0;
          const uR = hasU ? (d[i - stride] & 0xFFFE) : 0;
          const dR = hasD ? (d[i + stride] & 0xFFFE) : 0;
          const hs = (((lR + rR) | 0) ^ Math.imul((uR + dR) | 0, 0x27D4EB2F)) | 0;
          const k  = fin(fin(fin(SEED ^ me) ^ hs) ^ 0) >>> 0;
          if ((k & 255) >= thr) continue;
          const c   = ((k >>> 8) & 0xFFFF) % 3;
          const bit = (k >>> 24) & 1;
          d[i + c] = (d[i + c] & 0xFFFE) | bit;
          if (flip) flip(i + c, bit);
        }
      }
    }

    function noisifyInt(d, stride, w, h, bx, by, tx, ty, tw, th, cw, ch, rate, flip, mask) {
      const thr = Math.max(0, Math.min(256, Math.floor(rate * 256)));
      for (let y = ty; y < ty + th; y++) {
        const Y = by + y;
        if (Y < 0 || Y >= ch) continue;
        for (let x = tx; x < tx + tw; x++) {
          const X = bx + x;
          if (X < 0 || X >= cw) continue;
          const i = y * stride + x * 4;
          if (d[i + 3] === 0) continue;
          const hasL = X > 0 && x > 0, hasR = X + 1 < cw && x + 1 < w;
          const hasU = Y > 0 && y > 0, hasD = Y + 1 < ch && y + 1 < h;
          if (!((hasL && hasR) || (hasU && hasD))) continue;
          let blend = false;
          for (let c = 0; c < 4 && !blend; c++) {
            if (hasL && hasR) {
              const v = d[i + c] & mask;
              const p = d[i - 4 + c] & mask, q = d[i + 4 + c] & mask;
              if (v !== p && v !== q && ((p < v && v < q) || (q < v && v < p))) blend = true;
            }
            if (!blend && hasU && hasD) {
              const v = d[i + c] & mask;
              const p = d[i - stride + c] & mask, q = d[i + stride + c] & mask;
              if (v !== p && v !== q && ((p < v && v < q) || (q < v && v < p))) blend = true;
            }
          }
          if (!blend) continue;
          const q0 = d[i] & mask, q1 = d[i + 1] & mask, q2 = d[i + 2] & mask;
          const me = (q0 ^ Math.imul(q1, 0x9E3779B1) ^ Math.imul(q2, 0x85EBCA6B)) | 0;
          const hs = (((hasL ? (d[i - 4] & mask) : 0) + (hasR ? (d[i + 4] & mask) : 0)) | 0) >>> 0;
          const vs = (((hasU ? (d[i - stride] & mask) : 0) + (hasD ? (d[i + stride] & mask) : 0)) | 0) >>> 0;
          const k = fin(fin(fin(SEED ^ me) ^ hs) ^ vs) >>> 0;
          if ((k & 255) >= thr) continue;
          const c   = ((k >>> 8) & 0xFFFF) % 3;
          const bit = (k >>> 24) & 1;
          d[i + c] = (d[i + c] & mask) | bit;
          if (flip) flip(i + c, bit);
        }
      }
    }

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
      for (const [pattern, models] of RENDERER_FAMILIES) {
        const match = native.match(pattern);
        if (!match) continue;
        const choices = models.filter(model => model.toLowerCase() !== match[0].toLowerCase());
        const model = choices[(fin(SEED) >>> 0) % choices.length];
        // A PCI device ID belongs to the original model, so do not pair it
        // with the alias. Preserve ANGLE/backend and shader-model suffixes.
        return native.replace(pattern, model).replace(/\s*\(0x[0-9a-f]+\)/ig, '');
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

      const nativeGetParam1 = gl1 ? gl1.prototype.getParameter : null;
      const nativeGetParam2 = gl2 ? gl2.prototype.getParameter : null;
      const nativeExtensions1 = gl1 ? gl1.prototype.getSupportedExtensions : null;
      const nativeExtensions2 = gl2 ? gl2.prototype.getSupportedExtensions : null;

      const getParamHandler = (query) => (t, self, args) => {
        if (!args.length) return Reflect.apply(t, self, args);
        Reflect.apply(query, self, []);
        const pname = (+args[0]) >>> 0;
        const native = Reflect.apply(t, self, [pname]);
        return pname === 0x9246 ? rendererAlias(native) : native;
      };

      const readPixelsHandler = (t, self, args) => {
        const x = args[0] | 0, y = args[1] | 0, w = args[2] | 0, h = args[3] | 0;
        const format = args[4], type = args[5], pixels = args[6];
        if (w <= 0 || h <= 0) return Reflect.apply(t, self, args);
        if (!pixels || !ArrayBuffer.isView(pixels)) return Reflect.apply(t, self, args);
        const dstOffset = (args.length >= 8 && Number.isFinite(args[7])) ? (args[7] | 0) : 0;
        if (dstOffset < 0) return Reflect.apply(t, self, args);
        if (pixels.length < dstOffset + w * h * 4) return Reflect.apply(t, self, args);
        const destView = dstOffset ? pixels.subarray(dstOffset) : pixels;

        const RGBA = self.RGBA, RGBA_INT = 0x8D99;
        const U8 = self.UNSIGNED_BYTE, I8 = self.BYTE;
        const U16 = self.UNSIGNED_SHORT, I16 = self.SHORT;
        const U32 = self.UNSIGNED_INT, I32 = self.INT;
        const F32 = self.FLOAT, F16 = self.HALF_FLOAT;

        let strat = null;
        if (format === RGBA) {
          if (type === U8 && destView instanceof win.Uint8Array) strat = { kind: 'u8', Ctor: win.Uint8Array, mask: 254 };
          else if (ENABLE_FLOATGL && type === F32 && destView instanceof win.Float32Array) strat = { kind: 'f32', Ctor: win.Float32Array };
          else if (ENABLE_FLOATGL && type === F16 && destView instanceof win.Uint16Array) strat = { kind: 'f16', Ctor: win.Uint16Array, mask: 0xFFFE };
        } else if (format === RGBA_INT) {
          if (type === U8 && destView instanceof win.Uint8Array) strat = { kind: 'int', Ctor: win.Uint8Array, mask: 254 };
          else if (type === I8 && destView instanceof win.Int8Array) strat = { kind: 'int', Ctor: win.Int8Array, mask: 254 };
          else if (type === U16 && destView instanceof win.Uint16Array) strat = { kind: 'int', Ctor: win.Uint16Array, mask: 0xFFFE };
          else if (type === I16 && destView instanceof win.Int16Array) strat = { kind: 'int', Ctor: win.Int16Array, mask: 0xFFFE };
          else if (type === U32 && destView instanceof win.Uint32Array) strat = { kind: 'int', Ctor: win.Uint32Array, mask: 0xFFFFFFFE };
          else if (type === I32 && destView instanceof win.Int32Array) strat = { kind: 'int', Ctor: win.Int32Array, mask: 0xFFFFFFFE };
        }
        if (!strat) return Reflect.apply(t, self, args);

        const isGL2 = !!(gl2 && self instanceof gl2);
        const nativeGetParam = isGL2 ? nativeGetParam2 : nativeGetParam1;
        if (!nativeGetParam) return Reflect.apply(t, self, args);
        try {
          const align = Reflect.apply(nativeGetParam, self, [0x0D05]);
          if (typeof align !== 'number' || (align !== 4 && align !== 1)) return Reflect.apply(t, self, args);
          if (isGL2) {
            const rowLen = Reflect.apply(nativeGetParam, self, [0x0D02]);
            const skipRows = Reflect.apply(nativeGetParam, self, [0x0D03]);
            const skipPx = Reflect.apply(nativeGetParam, self, [0x0D04]);
            if (typeof rowLen !== 'number' || typeof skipRows !== 'number' || typeof skipPx !== 'number') return Reflect.apply(t, self, args);
            if (rowLen !== 0 || skipRows !== 0 || skipPx !== 0) return Reflect.apply(t, self, args);
          }
        } catch (e) { return Reflect.apply(t, self, args); }

        const ret = Reflect.apply(t, self, args);
        const bw = self.drawingBufferWidth | 0, bh = self.drawingBufferHeight | 0;
        const px0 = Math.max(0, x - 1), py0 = Math.max(0, y - 1);
        const px1 = Math.min(bw, x + w + 1), py1 = Math.min(bh, y + h + 1);
        const pw = px1 - px0, ph = py1 - py0;
        if (pw <= 0 || ph <= 0) return ret;
        const tx = x - px0, ty = y - py0;

        const dispatch = (buf, flip) => {
          if (strat.kind === 'u8') noisify(buf, pw * 4, pw, ph, px0, py0, tx, ty, w, h, bw, bh, GL_NOISE_RATE, flip);
          else if (strat.kind === 'int') noisifyInt(buf, pw * 4, pw, ph, px0, py0, tx, ty, w, h, bw, bh, GL_NOISE_RATE, flip, strat.mask);
          else if (strat.kind === 'f32') noisifyF32(buf, pw * 4, pw, ph, px0, py0, tx, ty, w, h, bw, bh, GL_NOISE_RATE, flip);
          else if (strat.kind === 'f16') noisifyF16(buf, pw * 4, pw, ph, px0, py0, tx, ty, w, h, bw, bh, GL_NOISE_RATE, flip);
        };

        try {
          if (pw === w && ph === h && dstOffset === 0) { dispatch(pixels, null); return ret; }
          const pad = new strat.Ctor(pw * ph * 4);
          Reflect.apply(t, self, [px0, py0, pw, ph, format, type, pad]);
          dispatch(pad, (idx, bit) => {
            const localY = (idx / (pw * 4)) | 0;
            const localX = ((idx % (pw * 4)) / 4) | 0;
            const ix = localX - tx, iy = localY - ty;
            if (ix < 0 || iy < 0 || ix >= w || iy >= h) return;
            const j = (iy * w + ix) * 4 + (idx & 3);
            if (strat.kind === 'f32') {
              F32B[0] = destView[j];
              U32B[0] = U32B[0] ^ bit;
              destView[j] = F32B[0];
            } else {
              destView[j] = (destView[j] & strat.mask) | bit;
            }
          });
        } catch (e) {}
        return ret;
      };

      if (gl1) {
        hook(gl1.prototype, 'getParameter', getParamHandler(nativeExtensions1));
        if (ENABLE_WEBGL) hook(gl1.prototype, 'readPixels', readPixelsHandler);
      }
      if (gl2) {
        hook(gl2.prototype, 'getParameter', getParamHandler(nativeExtensions2));
        if (ENABLE_WEBGL) hook(gl2.prototype, 'readPixels', readPixelsHandler);
      }
    }

    /* ------------------------------------------------------------------ *
     * WebGPU
     * ------------------------------------------------------------------ */

    function installWebGPU() {
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
      if (!ENABLE_WEBGPU) return;
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
    perturb_float_readback: bool = False,
) -> list[str]:
    """Return graphics/audio init scripts for one account seed.

    Transformations are opt-in and independent of browser launch mode.
    Either GPU flag enables shared GPU identity policy; rendering/readback
    and WebGPU capability policies remain separately selectable.

    ``perturb_canvas``        Gradient/Bezier drawing offsets; native bitmap
                              reads and exports, including float16 data.
    ``spoof_webgl``           Hardware-family renderer alias, WebGPU model
                              redaction, RGBA/RGBA_INTEGER readback.
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
    ``perturb_float_readback`` Perturb RGBA FLOAT and HALF_FLOAT
                              readPixels destinations in WebGL.

    Register once with ``BrowserContext.add_init_script``. By itself, cross-origin
    workers, service workers, and any worker started outside the wrapped
    constructors are not covered. The application supplies WorkerProfiles
    separately for complete graphics-worker startup. Depth and stencil readbacks are never
    perturbed. Integer non-edge pixels are bit-identical to native.
    PBO-offset and non-default pack-state reads fall through to native.
    WebGPU device-level readback is not perturbed.
    """
    if not (perturb_canvas or spoof_webgl or spoof_webgpu or spoof_audio
            or intercept_workers or perturb_float_readback):
        return []

    seed = int(seed_int) & 0xFFFFFFFF
    js = CANVAS_JS_TEMPLATE
    js = js.replace("__SEED__",             str(seed))
    js = js.replace("__NOISE_RATE__",       "0.5")
    js = js.replace("__GL_NOISE_RATE__",    "0.25")
    js = js.replace("__ENABLE_2D__",        "true" if perturb_canvas         else "false")
    js = js.replace("__ENABLE_WEBGL__",     "true" if spoof_webgl            else "false")
    js = js.replace("__ENABLE_WEBGPU__",    "true" if spoof_webgpu           else "false")
    js = js.replace("__ENABLE_AUDIO__",     "true" if spoof_audio            else "false")
    js = js.replace("__ENABLE_WORKERS__",   "true" if intercept_workers      else "false")
    js = js.replace("__ENABLE_FLOATGL__",   "true" if perturb_float_readback else "false")
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
