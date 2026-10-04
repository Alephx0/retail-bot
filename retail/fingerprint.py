"""Seeded 2D canvas + WebGL + WebGPU + AudioContext perturbation and identity.

``build_scripts`` gates everything behind explicit opt-in. ``build_worker_script``
supplies the same implementation for worker entrypoints the caller controls.
Both must run before application code touches canvas, WebGL, WebGPU, or audio.

Design rules:

  - Every surface is an explicit ``bool`` opt-in. There is no implicit coupling
    between options and no derivation from browser launch mode. Callers select
    exactly what they want applied; a visible window does not change which
    hooks are installed.
  - Pixel perturbation is content-keyed, never position- or read-size-keyed.
    Only blended/edge pixels move. The byte, half-float, and float paths use
    the same eligibility predicate: neighbour-inequality on the full RGBA
    tuple, alpha range check, premultiplied colour range check, and
    horizontal-or-vertical boundary handling.
  - WebGL identity, capability limits, per-extension set, per-stage shader
    precision, and RGBA/RGBA_INTEGER readback are clamped to one coherent
    per-seed GPU profile.
  - WebGPU adapter info, adapter/device limits, and features are keyed on the
    same seed/profile as WebGL. Fabricated adapterInfo objects are Proxies
    over the native GPUAdapterInfo instance so WebIDL brand checks succeed.
    Subgroup sizes and isFallbackAdapter read through to the native object,
    keeping them consistent with the exposed feature set. requiredLimits and
    requiredFeatures are validated against the advertised profile before
    native dispatch. Unsupported limits reject with OperationError;
    unsupported features reject with TypeError.
  - Audio metadata fallbacks are keyed on the same seed as the GPU profile,
    but semantically valid native values are preserved. Positive sample
    rates, non-negative latency values, mono channel counts, and native
    OfflineAudioContext maxChannelCount values are left untouched. Rendered
    audio buffers and DynamicsCompressorNode.reduction remain fully native;
    this layer does not alter DSP output.
  - Native adapter/device methods are returned bound to the real target with
    name/length restored. Bound method identity relative to the prototype
    differs from a native object; this is a fundamental limitation of JS
    proxies around WebIDL interfaces.
  - WebGL extension lists are fabricated from a per-profile allow-list.
    Extensions whose objects carry only constants, and ASTC, get a stub that
    is internally consistent. Extensions with real state-mutating methods
    route to native; they are returned only if available.
  - Full-frame and (0,0) reads are perturbed. The padded region is clipped
    to the framebuffer; missing neighbours simply do not enter the hash.
  - WebGL2 readPixels dstOffset is supported via a subarray view.
  - OffscreenCanvas.transferToImageBitmap() routes the canvas through the
    same noisify core before handing back the bitmap.
  - 2D getImageData handles both rgba-unorm8 (Uint8ClampedArray) and
    rgba-float16 (Float16Array) results.
  - The readPixels pack-state guard reads native getParameter (captured
    before hooking) and fails closed.
  - Same-origin classic, module, and data: workers created through the
    wrapped constructors receive the bootstrap before their own entrypoint.
  - Runtime-hook concealment covers Function.prototype.toString, descriptor
    reads (Object/Reflect), and symbol enumeration of the idempotency marker.

Out of scope, by design and by platform limit: service-worker interception
(blob:/data: script URLs are rejected by the SW registrar), cross-origin
worker injection, native-level (non-JS) hook concealment, deep emulation of
native GPU rasterization/shader semantics, depth/stencil readback
perturbation, PBO-offset readback perturbation, non-tight pack-state
emulation, WebGPU device-level readback perturbation, WebIDL prototype
method identity, and timing side channels.
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

    const realSources = new WeakMap();
    const hiddenDescriptors = new WeakMap();
    function markHidden(owner, name, desc) {
      let m = hiddenDescriptors.get(owner);
      if (!m) { m = new Map(); hiddenDescriptors.set(owner, m); }
      m.set(name, desc);
    }

    const hookedOwners = new WeakMap();
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
      const proxy = new Proxy(orig, {
        apply: (t, self, args) => handler(t, self, args, orig)
      });
      realSources.set(proxy, orig);
      try {
        Object.defineProperty(owner, name, {
          value: proxy,
          writable:     !!desc.writable,
          enumerable:   !!desc.enumerable,
          configurable: !!desc.configurable
        });
      } catch (e) { return false; }
      markHidden(owner, name, desc);
      if (!seen) { seen = new Set(); hookedOwners.set(owner, seen); }
      seen.add(name);
      return true;
    }

    hook(Function.prototype, 'toString', (t, self, args) => {
      const real = realSources.get(self);
      if (real !== undefined) return Reflect.apply(t, real, args);
      return Reflect.apply(t, self, args);
    });

    /* ------------------------------------------------------------------ *
     * Shared GPU profile table.
     * ------------------------------------------------------------------ */

    const F = [127, 127, 23];
    const I = [31, 30, 0];
    const DESC_V = { 0x8DF0: F, 0x8DF1: F, 0x8DF2: F, 0x8DF3: I, 0x8DF4: I, 0x8DF5: I };
    const DESC_F = { 0x8DF0: F, 0x8DF1: F, 0x8DF2: F, 0x8DF3: I, 0x8DF4: I, 0x8DF5: I };
    const APPLE_V = DESC_V;
    const APPLE_F = Object.assign({}, DESC_F, { 0x8DF0: [15, 15, 10] });

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

    const PROFILES = [
      { vendorTag: 'NVIDIA',
        vendor: 'Google Inc. (NVIDIA)',
        renderer: 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)',
        gpu: { vendor: 'nvidia', architecture: 'turing', device: '', description: '' },
        precision: { vertex: DESC_V, fragment: DESC_F },
        limits: {
          0x0D33: 16384, 0x851C: 16384, 0x84E8: 16384,
          0x8869: 16, 0x8872: 16, 0x8B4C: 16, 0x8B4D: 32,
          0x8DFB: 4096, 0x8DFC: 31, 0x8DFD: 4096,
          0x8073: 2048, 0x88FF: 2048,
          0x8824: 8, 0x8CDF: 8, 0x8D57: 8,
          0x8A2F: 72, 0x8D6B: 4294967295,
          0x8A2D: 16, 0x8A2B: 16, 0x8A2E: 32, 0x8A30: 65536,
          0x8B4A: 16384, 0x8B49: 16384,
          0x9122: 256, 0x9125: 256,
          0x84FF: 16,
          0x8C80: 4, 0x8C8A: 128, 0x8C8B: 4, 0x8E70: 4,
          0x84FD: 15, 0x9111: 0,
          0x8A31: 16384, 0x8A33: 16384
        },
        viewport: [32767, 32767], pointSize: [1, 1024], lineWidth: [1, 1] },

      { vendorTag: 'Intel',
        vendor: 'Google Inc. (Intel)',
        renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)',
        gpu: { vendor: 'intel', architecture: 'gen-9', device: '', description: '' },
        precision: { vertex: DESC_V, fragment: DESC_F },
        limits: {
          0x0D33: 16384, 0x851C: 16384, 0x84E8: 16384,
          0x8869: 16, 0x8872: 16, 0x8B4C: 16, 0x8B4D: 32,
          0x8DFB: 4096, 0x8DFC: 30, 0x8DFD: 1024,
          0x8073: 2048, 0x88FF: 2048,
          0x8824: 8, 0x8CDF: 8, 0x8D57: 4,
          0x8A2F: 72, 0x8D6B: 4294967295,
          0x8A2D: 16, 0x8A2B: 16, 0x8A2E: 32, 0x8A30: 65536,
          0x8B4A: 16384, 0x8B49: 4096,
          0x9122: 128, 0x9125: 128,
          0x84FF: 16,
          0x8C80: 4, 0x8C8A: 128, 0x8C8B: 4, 0x8E70: 4,
          0x84FD: 15, 0x9111: 0,
          0x8A31: 16384, 0x8A33: 4096
        },
        viewport: [32767, 32767], pointSize: [1, 255], lineWidth: [1, 1] },

      { vendorTag: 'AMD',
        vendor: 'Google Inc. (AMD)',
        renderer: 'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)',
        gpu: { vendor: 'amd', architecture: 'gcn-4', device: '', description: '' },
        precision: { vertex: DESC_V, fragment: DESC_F },
        limits: {
          0x0D33: 16384, 0x851C: 16384, 0x84E8: 16384,
          0x8869: 16, 0x8872: 16, 0x8B4C: 16, 0x8B4D: 32,
          0x8DFB: 4096, 0x8DFC: 32, 0x8DFD: 4096,
          0x8073: 2048, 0x88FF: 2048,
          0x8824: 8, 0x8CDF: 8, 0x8D57: 8,
          0x8A2F: 72, 0x8D6B: 4294967295,
          0x8A2D: 16, 0x8A2B: 16, 0x8A2E: 32, 0x8A30: 65536,
          0x8B4A: 16384, 0x8B49: 16384,
          0x9122: 256, 0x9125: 256,
          0x84FF: 16,
          0x8C80: 4, 0x8C8A: 128, 0x8C8B: 4, 0x8E70: 4,
          0x84FD: 15, 0x9111: 0,
          0x8A31: 16384, 0x8A33: 16384
        },
        viewport: [32767, 32767], pointSize: [1, 2047], lineWidth: [1, 1] },

      { vendorTag: 'Apple',
        vendor: 'Google Inc. (Apple)',
        renderer: 'ANGLE (Apple, Apple M1, OpenGL 4.1)',
        gpu: { vendor: 'apple', architecture: 'apple-m1', device: '', description: '' },
        precision: { vertex: APPLE_V, fragment: APPLE_F },
        limits: {
          0x0D33: 16384, 0x851C: 16384, 0x84E8: 16384,
          0x8869: 16, 0x8872: 16, 0x8B4C: 16, 0x8B4D: 32,
          0x8DFB: 4096, 0x8DFC: 31, 0x8DFD: 1024,
          0x8073: 2048, 0x88FF: 2048,
          0x8824: 8, 0x8CDF: 8, 0x8D57: 4,
          0x8A2F: 72, 0x8D6B: 4294967295,
          0x8A2D: 16, 0x8A2B: 16, 0x8A2E: 32, 0x8A30: 65536,
          0x8B4A: 16384, 0x8B49: 4096,
          0x9122: 128, 0x9125: 128,
          0x84FF: 16,
          0x8C80: 4, 0x8C8A: 128, 0x8C8B: 4, 0x8E70: 4,
          0x84FD: 15, 0x9111: 0,
          0x8A31: 16384, 0x8A33: 4096
        },
        viewport: [32767, 32767], pointSize: [1, 511], lineWidth: [1, 1] }
    ];
    const P = PROFILES[(fin(SEED) >>> 0) % PROFILES.length];

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
      GPU_FEATURES_COMMON.concat(GPU_FEATURES_BY_VENDOR[P.vendorTag] || [])
    );

    /* ------------------------------------------------------------------ *
     * 2D canvas
     * ------------------------------------------------------------------ */

    function install2D() {
      const ctx2d  = win.CanvasRenderingContext2D;
      const offCtx = win.OffscreenCanvasRenderingContext2D;
      const OC     = win.OffscreenCanvas;
      const HCE    = win.HTMLCanvasElement;
      const canvases2D = new WeakSet();

      const rememberContext = (t, self, args) => {
        const context = Reflect.apply(t, self, args);
        if (context && (
            (ctx2d  && context instanceof ctx2d) ||
            (offCtx && context instanceof offCtx))) {
          canvases2D.add(self);
        }
        return context;
      };

      function makeImageDataHandler(getOrig) {
        return (t, self, args) => {
          const result = Reflect.apply(t, self, args);
          if (!result || !result.data) return result;
          if (args.length < 4) return result;
          for (let n = 0; n < 4; n++) {
            if (typeof args[n] !== 'number' || !Number.isFinite(args[n])) return result;
          }
          let x = args[0] | 0, y = args[1] | 0, w = args[2] | 0, h = args[3] | 0;
          if (!w || !h) return result;
          if (w < 0) { x += w; w = -w; }
          if (h < 0) { y += h; h = -h; }
          const cv = self.canvas;
          if (!cv) return result;
          const isU8  = result.data instanceof win.Uint8ClampedArray;
          const isF16 = (typeof win.Float16Array !== 'undefined') &&
                        (result.data instanceof win.Float16Array);
          if (!isU8 && !isF16) return result;
          const pxFmt = isF16 ? 'rgba-float16' : 'rgba-unorm8';
          try {
            const img = Reflect.apply(getOrig, self,
              [x - 1, y - 1, w + 2, h + 2,
               {colorSpace: result.colorSpace, pixelFormat: pxFmt}]);
            if (!img || !img.data) return result;
            if (isU8) {
              if (!(img.data instanceof win.Uint8ClampedArray)) return result;
              noisify(img.data, (w + 2) * 4, w + 2, h + 2,
                      x - 1, y - 1, 1, 1, w, h, cv.width, cv.height, NOISE_RATE);
            } else {
              if (!(img.data instanceof win.Float16Array)) return result;
              const u16 = new win.Uint16Array(img.data.buffer, img.data.byteOffset, img.data.length);
              noisifyF16(u16, (w + 2) * 4, w + 2, h + 2,
                         x - 1, y - 1, 1, 1, w, h, cv.width, cv.height, NOISE_RATE);
            }
            const src = img.data, sw = w + 2, out = result.data;
            for (let r = 0; r < h; r++) {
              const s = ((r + 1) * sw + 1) * 4;
              out.set(src.subarray(s, s + w * 4), r * w * 4);
            }
            return result;
          } catch (e) { return result; }
        };
      }

      if (ctx2d && HCE) {
        const orig2DGet = ctx2d.prototype.getImageData;
        const noisyCopy = (canvas) => {
          try {
            const w = canvas.width, h = canvas.height;
            if (!w || !h || !canvases2D.has(canvas)) return canvas;
            const c = win.document.createElement('canvas');
            c.width = w; c.height = h;
            const cx = c.getContext('2d');
            cx.drawImage(canvas, 0, 0);
            const img = orig2DGet.call(cx, 0, 0, w, h);
            noisify(img.data, w * 4, w, h, 0, 0, 0, 0, w, h, w, h, NOISE_RATE);
            cx.putImageData(img, 0, 0);
            return c;
          } catch (e) { return canvas; }
        };
        hook(HCE.prototype, 'getContext', rememberContext);
        hook(ctx2d.prototype, 'getImageData', makeImageDataHandler(orig2DGet));
        hook(HCE.prototype, 'toDataURL',
             (t, self, args) => Reflect.apply(t, noisyCopy(self), args));
        hook(HCE.prototype, 'toBlob',
             (t, self, args) => Reflect.apply(t, noisyCopy(self), args));
      }

      if (OC && offCtx) {
        const origOffGet = offCtx.prototype.getImageData;
        hook(OC.prototype, 'getContext', rememberContext);
        hook(offCtx.prototype, 'getImageData', makeImageDataHandler(origOffGet));
        hook(OC.prototype, 'convertToBlob', (t, self, args) => {
          try {
            if (!canvases2D.has(self)) return Reflect.apply(t, self, args);
            const w = self.width, h = self.height;
            const c  = new OC(w, h);
            const cx = c.getContext('2d');
            cx.drawImage(self, 0, 0);
            const img = origOffGet.call(cx, 0, 0, w, h);
            noisify(img.data, w * 4, w, h, 0, 0, 0, 0, w, h, w, h, NOISE_RATE);
            cx.putImageData(img, 0, 0);
            return Reflect.apply(t, c, args);
          } catch (e) { return Reflect.apply(t, self, args); }
        });
        hook(OC.prototype, 'transferToImageBitmap', (t, self, args) => {
          try {
            if (!canvases2D.has(self)) return Reflect.apply(t, self, args);
            const w = self.width, h = self.height;
            if (!w || !h) return Reflect.apply(t, self, args);
            const cx = self.getContext('2d');
            if (!cx) return Reflect.apply(t, self, args);
            const img = origOffGet.call(cx, 0, 0, w, h);
            if (!img || !img.data) return Reflect.apply(t, self, args);
            if (img.data instanceof win.Uint8ClampedArray) {
              noisify(img.data, w * 4, w, h, 0, 0, 0, 0, w, h, w, h, NOISE_RATE);
            } else if (typeof win.Float16Array !== 'undefined' &&
                       img.data instanceof win.Float16Array) {
              const u16 = new win.Uint16Array(img.data.buffer, img.data.byteOffset, img.data.length);
              noisifyF16(u16, w * 4, w, h, 0, 0, 0, 0, w, h, w, h, NOISE_RATE);
            } else {
              return Reflect.apply(t, self, args);
            }
            cx.putImageData(img, 0, 0);
            return Reflect.apply(t, self, args);
          } catch (e) { return Reflect.apply(t, self, args); }
        });
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
      const NONE = Symbol('native');

      const CONST_ONLY = {
        'WEBGL_compressed_texture_s3tc': {
          COMPRESSED_RGB_S3TC_DXT1_EXT: 0x83F0, COMPRESSED_RGBA_S3TC_DXT1_EXT: 0x83F1,
          COMPRESSED_RGBA_S3TC_DXT3_EXT: 0x83F2, COMPRESSED_RGBA_S3TC_DXT5_EXT: 0x83F3
        },
        'WEBGL_compressed_texture_s3tc_srgb': {
          COMPRESSED_SRGB_S3TC_DXT1_EXT: 0x8C4C, COMPRESSED_SRGB_ALPHA_S3TC_DXT1_EXT: 0x8C4D,
          COMPRESSED_SRGB_ALPHA_S3TC_DXT3_EXT: 0x8C4E, COMPRESSED_SRGB_ALPHA_S3TC_DXT5_EXT: 0x8C4F
        },
        'WEBGL_compressed_texture_etc': {
          COMPRESSED_R11_EAC: 0x9270, COMPRESSED_SIGNED_R11_EAC: 0x9271,
          COMPRESSED_RG11_EAC: 0x9272, COMPRESSED_SIGNED_RG11_EAC: 0x9273,
          COMPRESSED_RGB8_ETC2: 0x9274, COMPRESSED_SRGB8_ETC2: 0x9275,
          COMPRESSED_RGB8_PUNCHTHROUGH_ALPHA1_ETC2: 0x9276,
          COMPRESSED_SRGB8_PUNCHTHROUGH_ALPHA1_ETC2: 0x9277,
          COMPRESSED_RGBA8_ETC2_EAC: 0x9278, COMPRESSED_SRGB8_ALPHA8_ETC2_EAC: 0x9279
        },
        'WEBGL_compressed_texture_etc1': { COMPRESSED_RGB_ETC1_WEBGL: 0x8D64 },
        'EXT_texture_compression_rgtc': {
          COMPRESSED_RED_RGTC1_EXT: 0x8DBB, COMPRESSED_SIGNED_RED_RGTC1_EXT: 0x8DBC,
          COMPRESSED_RED_GREEN_RGTC2_EXT: 0x8DBD, COMPRESSED_SIGNED_RED_GREEN_RGTC2_EXT: 0x8DBE
        },
        'EXT_texture_compression_bptc': {
          COMPRESSED_RGBA_BPTC_UNORM_EXT: 0x8E8C, COMPRESSED_SRGB_ALPHA_BPTC_UNORM_EXT: 0x8E8D,
          COMPRESSED_RGB_BPTC_SIGNED_FLOAT_EXT: 0x8E8E, COMPRESSED_RGB_BPTC_UNSIGNED_FLOAT_EXT: 0x8E8F
        }
      };

      const CONST_PLUS_METHODS = {
        'WEBGL_compressed_texture_astc': {
          constants: {
            COMPRESSED_RGBA_ASTC_4x4_KHR: 0x93B0, COMPRESSED_RGBA_ASTC_5x4_KHR: 0x93B1,
            COMPRESSED_RGBA_ASTC_5x5_KHR: 0x93B2, COMPRESSED_RGBA_ASTC_6x5_KHR: 0x93B3,
            COMPRESSED_RGBA_ASTC_6x6_KHR: 0x93B4, COMPRESSED_RGBA_ASTC_8x5_KHR: 0x93B5,
            COMPRESSED_RGBA_ASTC_8x6_KHR: 0x93B6, COMPRESSED_RGBA_ASTC_8x8_KHR: 0x93B7,
            COMPRESSED_RGBA_ASTC_10x5_KHR: 0x93B8, COMPRESSED_RGBA_ASTC_10x6_KHR: 0x93B9,
            COMPRESSED_RGBA_ASTC_10x8_KHR: 0x93BA, COMPRESSED_RGBA_ASTC_10x10_KHR: 0x93BB,
            COMPRESSED_RGBA_ASTC_12x10_KHR: 0x93BC, COMPRESSED_RGBA_ASTC_12x12_KHR: 0x93BD,
            COMPRESSED_SRGB8_ALPHA8_ASTC_4x4_KHR: 0x93D0, COMPRESSED_SRGB8_ALPHA8_ASTC_5x4_KHR: 0x93D1,
            COMPRESSED_SRGB8_ALPHA8_ASTC_5x5_KHR: 0x93D2, COMPRESSED_SRGB8_ALPHA8_ASTC_6x5_KHR: 0x93D3,
            COMPRESSED_SRGB8_ALPHA8_ASTC_6x6_KHR: 0x93D4, COMPRESSED_SRGB8_ALPHA8_ASTC_8x5_KHR: 0x93D5,
            COMPRESSED_SRGB8_ALPHA8_ASTC_8x6_KHR: 0x93D6, COMPRESSED_SRGB8_ALPHA8_ASTC_8x8_KHR: 0x93D7,
            COMPRESSED_SRGB8_ALPHA8_ASTC_10x5_KHR: 0x93D8, COMPRESSED_SRGB8_ALPHA8_ASTC_10x6_KHR: 0x93D9,
            COMPRESSED_SRGB8_ALPHA8_ASTC_10x8_KHR: 0x93DA, COMPRESSED_SRGB8_ALPHA8_ASTC_10x10_KHR: 0x93DB,
            COMPRESSED_SRGB8_ALPHA8_ASTC_12x10_KHR: 0x93DC, COMPRESSED_SRGB8_ALPHA8_ASTC_12x12_KHR: 0x93DD
          },
          methods: { getSupportedProfiles: function () { return ['ldr']; } }
        }
      };

      const COMMON_FUNCTIONAL = [
        'ANGLE_instanced_arrays', 'EXT_blend_minmax', 'EXT_clip_control',
        'EXT_color_buffer_half_float', 'EXT_depth_clamp', 'EXT_float_blend',
        'EXT_frag_depth', 'EXT_polygon_offset_clamp', 'EXT_shader_texture_lod',
        'EXT_texture_filter_anisotropic', 'EXT_texture_mirror_clamp_to_edge',
        'EXT_sRGB', 'KHR_parallel_shader_compile', 'OES_element_index_uint',
        'OES_fbo_render_mipmap', 'OES_standard_derivatives', 'OES_texture_float',
        'OES_texture_float_linear', 'OES_texture_half_float',
        'OES_texture_half_float_linear', 'OES_vertex_array_object',
        'WEBGL_blend_func_extended', 'WEBGL_color_buffer_float',
        'WEBGL_debug_renderer_info', 'WEBGL_depth_texture', 'WEBGL_draw_buffers',
        'WEBGL_lose_context', 'WEBGL_multi_draw', 'WEBGL_polygon_mode',
        'WEBGL_provoking_vertex'
      ];

      const COMPRESSED_BY_VENDOR = {
        NVIDIA: ['WEBGL_compressed_texture_s3tc','WEBGL_compressed_texture_s3tc_srgb',
                 'EXT_texture_compression_rgtc','EXT_texture_compression_bptc',
                 'WEBGL_compressed_texture_astc','WEBGL_compressed_texture_etc'],
        AMD:    ['WEBGL_compressed_texture_s3tc','WEBGL_compressed_texture_s3tc_srgb',
                 'EXT_texture_compression_rgtc','EXT_texture_compression_bptc',
                 'WEBGL_compressed_texture_astc','WEBGL_compressed_texture_etc'],
        Intel:  ['WEBGL_compressed_texture_s3tc','WEBGL_compressed_texture_s3tc_srgb',
                 'EXT_texture_compression_rgtc','EXT_texture_compression_bptc',
                 'WEBGL_compressed_texture_astc','WEBGL_compressed_texture_etc',
                 'WEBGL_compressed_texture_etc1'],
        Apple:  ['WEBGL_compressed_texture_astc','WEBGL_compressed_texture_etc',
                 'WEBGL_compressed_texture_etc1','EXT_texture_compression_bptc']
      };

      const WEBGL2_ONLY = [
        'EXT_color_buffer_float','EXT_conservative_depth',
        'EXT_disjoint_timer_query_webgl2','EXT_render_snorm','EXT_texture_norm16',
        'NV_shader_noperspective_interpolation','OES_draw_buffers_indexed',
        'OES_sample_variables','OES_shader_multisample_interpolation',
        'OVR_multiview2','WEBGL_clip_cull_distance'
      ];

      const profileExtensionsCache = new WeakMap();
      function profileExtensionsFor(self) {
        let s = profileExtensionsCache.get(self);
        if (s) return s;
        const isGL2 = !!(gl2 && self instanceof gl2);
        s = new Set(COMMON_FUNCTIONAL);
        for (const e of (COMPRESSED_BY_VENDOR[P.vendorTag] || [])) s.add(e);
        if (isGL2) for (const e of WEBGL2_ONLY) s.add(e);
        profileExtensionsCache.set(self, s);
        return s;
      }

      function spoof(pname) {
        if (pname === 0x1F00) return 'WebKit';
        if (pname === 0x1F01) return 'WebKit WebGL';
        if (pname === 0x9245) return P.vendor;
        if (pname === 0x9246) return P.renderer;
        if (pname === 0x0D3A) return new win.Int32Array([P.viewport[0], P.viewport[1]]);
        if (pname === 0x846D) return new win.Float32Array([P.pointSize[0], P.pointSize[1]]);
        if (pname === 0x846E) return new win.Float32Array([P.lineWidth[0], P.lineWidth[1]]);
        if (Object.prototype.hasOwnProperty.call(P.limits, pname)) return P.limits[pname];
        return NONE;
      }

      const getParamHandler = (t, self, args, orig) => {
        const native = Reflect.apply(t, self, args);
        if (native === null || native === undefined) return native;
        if (args[0] === 0x84FF && !profileExtensionsFor(self).has('EXT_texture_filter_anisotropic')) return null;
        const s = spoof(args[0]);
        return s === NONE ? native : s;
      };

      const gifpHandler = (t, self, args, orig) => {
        const r = Reflect.apply(t, self, args);
        if (!r || !(r instanceof win.Int32Array)) return r;
        const cap = P.limits[0x8D57];
        for (let i = 0; i < r.length; i++) if (r[i] > cap) r[i] = cap;
        return r;
      };

      const stubCache = new WeakMap();
      const getExtensionHandler = (t, self, args, orig) => {
        const name = String(args[0]);
        if (!profileExtensionsFor(self).has(name)) return null;
        let m = stubCache.get(self);
        if (!m) { m = new Map(); stubCache.set(self, m); }
        if (Object.prototype.hasOwnProperty.call(CONST_ONLY, name)) {
          let stub = m.get(name);
          if (!stub) {
            stub = Object.create(null);
            const tbl = CONST_ONLY[name];
            for (const k in tbl) {
              Object.defineProperty(stub, k, { value: tbl[k], enumerable: true, configurable: false, writable: false });
            }
            m.set(name, stub);
          }
          return stub;
        }
        if (Object.prototype.hasOwnProperty.call(CONST_PLUS_METHODS, name)) {
          let stub = m.get(name);
          if (!stub) {
            stub = Object.create(null);
            const spec = CONST_PLUS_METHODS[name];
            for (const k in spec.constants) {
              Object.defineProperty(stub, k, { value: spec.constants[k], enumerable: true, configurable: false, writable: false });
            }
            for (const k in spec.methods) {
              Object.defineProperty(stub, k, { value: spec.methods[k], enumerable: true, configurable: false, writable: false });
            }
            m.set(name, stub);
          }
          return stub;
        }
        return Reflect.apply(t, self, args);
      };

      const getSupportedExtensionsHandler = (t, self, args, orig) => {
        const native = Reflect.apply(t, self, args);
        const nativeSet = new Set(Array.isArray(native) ? native : []);
        const allowed = profileExtensionsFor(self);
        const out = [];
        for (const e of allowed) {
          if (Object.prototype.hasOwnProperty.call(CONST_ONLY, e) ||
              Object.prototype.hasOwnProperty.call(CONST_PLUS_METHODS, e)) {
            out.push(e);
          } else if (nativeSet.has(e)) {
            out.push(e);
          }
        }
        return out;
      };

      const precisionHandler = (t, self, args, orig) => {
        const native = Reflect.apply(t, self, args);
        if (!native) return native;
        const shaderType = args[0];
        const precType   = args[1];
        let table = null;
        if (shaderType === 0x8B31) table = P.precision.vertex;
        else if (shaderType === 0x8B30) table = P.precision.fragment;
        if (!table) return native;
        const v = table[precType];
        if (!v) return native;
        try {
          const fake = Object.create(Object.getPrototypeOf(native));
          Object.defineProperties(fake, {
            rangeMin:  {value: v[0], enumerable: true, configurable: true},
            rangeMax:  {value: v[1], enumerable: true, configurable: true},
            precision: {value: v[2], enumerable: true, configurable: true}
          });
          return fake;
        } catch (e) { return native; }
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
        hook(gl1.prototype, 'getParameter', getParamHandler);
        hook(gl1.prototype, 'getExtension', getExtensionHandler);
        hook(gl1.prototype, 'getSupportedExtensions', getSupportedExtensionsHandler);
        hook(gl1.prototype, 'getShaderPrecisionFormat', precisionHandler);
        hook(gl1.prototype, 'readPixels', readPixelsHandler);
      }
      if (gl2) {
        hook(gl2.prototype, 'getParameter', getParamHandler);
        hook(gl2.prototype, 'getExtension', getExtensionHandler);
        hook(gl2.prototype, 'getSupportedExtensions', getSupportedExtensionsHandler);
        hook(gl2.prototype, 'getShaderPrecisionFormat', precisionHandler);
        hook(gl2.prototype, 'getInternalformatParameter', gifpHandler);
        hook(gl2.prototype, 'readPixels', readPixelsHandler);
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

      const boundMethodCache = new WeakMap();
      function bindToTarget(fn, target) {
        if (typeof fn !== 'function') return fn;
        let m = boundMethodCache.get(target);
        if (!m) { m = new WeakMap(); boundMethodCache.set(target, m); }
        let bound = m.get(fn);
        if (!bound) {
          bound = fn.bind(target);
          try { Object.defineProperty(bound, 'name', { value: fn.name, configurable: true }); } catch (e) {}
          try { Object.defineProperty(bound, 'length', { value: fn.length, configurable: true }); } catch (e) {}
          realSources.set(bound, fn);
          m.set(fn, bound);
        }
        return bound;
      }

      // GPUAdapterInfo view: wrap the native branded object. Only the four
      // identity fields are shadowed. subgroupMinSize / subgroupMaxSize /
      // isFallbackAdapter read through to the native object so they stay
      // consistent with the exposed feature set.
      const infoViewCache = new WeakMap();
      function makeAdapterInfo(nativeInfo) {
        if (!nativeInfo || typeof nativeInfo !== 'object') return nativeInfo;
        if (infoViewCache.has(nativeInfo)) return infoViewCache.get(nativeInfo);
        const view = new Proxy(nativeInfo, {
          get(target, prop, receiver) {
            if (prop === 'vendor')       return P.gpu.vendor;
            if (prop === 'architecture') return P.gpu.architecture;
            if (prop === 'device')       return P.gpu.device;
            if (prop === 'description')  return P.gpu.description;
            const value = Reflect.get(target, prop, target);
            if (typeof value === 'function') return bindToTarget(value, target);
            return value;
          }
        });
        infoViewCache.set(nativeInfo, view);
        return view;
      }

      const limitsViewCache = new WeakMap();
      function makeLimitsView(nativeLimits) {
        if (!nativeLimits || typeof nativeLimits !== 'object') return nativeLimits;
        if (limitsViewCache.has(nativeLimits)) return limitsViewCache.get(nativeLimits);
        const view = new Proxy(nativeLimits, {
          get(target, prop, receiver) {
            const native = Reflect.get(target, prop, target);
            if (typeof prop === 'string' && Object.prototype.hasOwnProperty.call(GPU_LIMITS, prop)) {
              return clampLimit(prop, native, GPU_LIMITS[prop]);
            }
            return native;
          }
        });
        limitsViewCache.set(nativeLimits, view);
        return view;
      }

      const featuresViewCache = new WeakMap();
      function makeFeaturesView(nativeFeatures) {
        if (!nativeFeatures || typeof nativeFeatures !== 'object') return nativeFeatures;
        if (featuresViewCache.has(nativeFeatures)) return featuresViewCache.get(nativeFeatures);
        const kept = new Set();
        try {
          for (const f of nativeFeatures) if (PROFILE_GPU_FEATURES.has(f)) kept.add(f);
        } catch (e) {
          try {
            const arr = Array.from(nativeFeatures);
            for (const f of arr) if (PROFILE_GPU_FEATURES.has(f)) kept.add(f);
          } catch (e2) {}
        }
        let view = null;
        const api = {
          get size() { return kept.size; },
          has(name) { return kept.has(name); },
          forEach(fn, thisArg) { for (const v of kept) fn.call(thisArg, v, v, view); },
          keys() { return kept.keys(); },
          values() { return kept.values(); },
          entries() { return kept.entries(); },
          [Symbol.iterator]() { return kept[Symbol.iterator](); }
        };
        view = new Proxy(nativeFeatures, {
          get(target, prop, receiver) {
            if (prop === 'size') return api.size;
            if (prop === 'has') return api.has;
            if (prop === 'forEach') return api.forEach;
            if (prop === 'keys') return api.keys;
            if (prop === 'values') return api.values;
            if (prop === 'entries') return api.entries;
            if (prop === Symbol.iterator) return api[Symbol.iterator];
            const value = Reflect.get(target, prop, target);
            if (typeof value === 'function') return bindToTarget(value, target);
            return value;
          }
        });
        featuresViewCache.set(nativeFeatures, view);
        return view;
      }

      function validateDeviceRequest(desc) {
        if (!desc || typeof desc !== 'object') return null;
        if (desc.requiredLimits && typeof desc.requiredLimits === 'object') {
          for (const key of Object.keys(desc.requiredLimits)) {
            const requested = desc.requiredLimits[key];
            if (typeof requested !== 'number') continue;
            const profileValue = GPU_LIMITS[key];
            if (typeof profileValue === 'number') {
              if (MIN_LIMITS.has(key)) {
                if (requested < profileValue) {
                  return makeOperationError('requiredLimits.' + key + ' (' + requested + ') is below the advertised minimum (' + profileValue + ')');
                }
              } else {
                if (requested > profileValue) {
                  return makeOperationError('requiredLimits.' + key + ' (' + requested + ') exceeds the advertised limit (' + profileValue + ')');
                }
              }
            }
          }
        }
        if (desc.requiredFeatures) {
          const feats = desc.requiredFeatures;
          const list = (typeof feats[Symbol.iterator] === 'function') ? Array.from(feats) : [];
          for (const f of list) {
            if (!PROFILE_GPU_FEATURES.has(f)) {
              return new TypeError('requiredFeatures contains unsupported feature: ' + f);
            }
          }
        }
        return null;
      }

      const adapterViewCache = new WeakMap();
      const deviceViewCache = new WeakMap();

      function wrapAdapter(adapter) {
        if (!adapter) return adapter;
        if (adapterViewCache.has(adapter)) return adapterViewCache.get(adapter);
        let requestDeviceWrap = null;
        const view = new Proxy(adapter, {
          get(target, prop, receiver) {
            if (prop === 'info') {
              let native = null;
              try { native = Reflect.get(target, prop, target); } catch (e) {}
              return makeAdapterInfo(native);
            }
            if (prop === 'limits') {
              let native;
              try { native = Reflect.get(target, prop, target); } catch (e) { return undefined; }
              return makeLimitsView(native);
            }
            if (prop === 'features') {
              let native;
              try { native = Reflect.get(target, prop, target); } catch (e) { return undefined; }
              return makeFeaturesView(native);
            }
            if (prop === 'requestDevice') {
              if (requestDeviceWrap) return requestDeviceWrap;
              const orig = Reflect.get(target, prop, target);
              if (typeof orig !== 'function') return orig;
              requestDeviceWrap = function (...args) {
                const error = validateDeviceRequest(args[0]);
                if (error) return Promise.reject(error);
                const p = Reflect.apply(orig, target, args);
                if (!p || typeof p.then !== 'function') return p;
                return p.then(device => wrapDevice(device));
              };
              try { Object.defineProperty(requestDeviceWrap, 'name', { value: orig.name, configurable: true }); } catch (e) {}
              try { Object.defineProperty(requestDeviceWrap, 'length', { value: orig.length, configurable: true }); } catch (e) {}
              realSources.set(requestDeviceWrap, orig);
              return requestDeviceWrap;
            }
            const value = Reflect.get(target, prop, target);
            if (typeof value === 'function') return bindToTarget(value, target);
            return value;
          }
        });
        adapterViewCache.set(adapter, view);
        return view;
      }

      function wrapDevice(device) {
        if (!device) return device;
        if (deviceViewCache.has(device)) return deviceViewCache.get(device);
        const view = new Proxy(device, {
          get(target, prop, receiver) {
            if (prop === 'adapterInfo') {
              let native = null;
              try { native = Reflect.get(target, prop, target); } catch (e) {}
              return makeAdapterInfo(native);
            }
            if (prop === 'limits') {
              let native;
              try { native = Reflect.get(target, prop, target); } catch (e) { return undefined; }
              return makeLimitsView(native);
            }
            if (prop === 'features') {
              let native;
              try { native = Reflect.get(target, prop, target); } catch (e) { return undefined; }
              return makeFeaturesView(native);
            }
            const value = Reflect.get(target, prop, target);
            if (typeof value === 'function') return bindToTarget(value, target);
            return value;
          }
        });
        deviceViewCache.set(device, view);
        return view;
      }

      hook(GPU.prototype, 'requestAdapter', (t, self, args) => {
        const p = Reflect.apply(t, self, args);
        if (!p || typeof p.then !== 'function') return p;
        return p.then(adapter => wrapAdapter(adapter));
      });

      if (GPUAdapter && GPUAdapter.prototype &&
          typeof GPUAdapter.prototype.requestAdapterInfo === 'function') {
        hook(GPUAdapter.prototype, 'requestAdapterInfo', (t, self, args) => {
          const p = Reflect.apply(t, self, args);
          if (!p || typeof p.then !== 'function') return p;
          return p.then(nativeInfo => makeAdapterInfo(nativeInfo));
        });
      }
    }

    /* ------------------------------------------------------------------ *
     * AudioContext
     * ------------------------------------------------------------------ */

    // Locate a getter on the prototype chain and wrap it. The wrapper
    // returns the native value when it is usable and the profile value
    // otherwise. The original getter is preserved via the concealment map
    // so descriptor reads continue to look native.
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
        wrapped = function () {
          let native;
          try { native = orig.call(this); } catch (e) { native = undefined; }
          return isValid(native) ? native : fallback;
        };
      } catch (e) { return false; }
      try {
        Object.defineProperty(owner, name, {
          get: wrapped,
          configurable: true,
          enumerable: desc.enumerable
        });
      } catch (e) { return false; }
      markHidden(owner, name, desc);
      realSources.set(wrapped, orig);
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
        const proxy = new Proxy(orig, {
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
        realSources.set(proxy, orig);
        return proxy;
      }

      if (W) { try { win.Worker = makeProxy(W, (args) => !!(args[1] && args[1].type === 'module')); } catch (e) {} }
      if (SW) { try { win.SharedWorker = makeProxy(SW, (args) => !!(args[1] && args[1].type === 'module')); } catch (e) {} }
    }

    /* ------------------------------------------------------------------ *
     * Concealment
     * ------------------------------------------------------------------ */

    function installConcealment() {
      const gopdHandler = (t, self, args) => {
        const owner = args[0], key = args[1];
        if (owner === win && key === MARKER) return undefined;
        const hid = hiddenDescriptors.get(owner);
        if (hid && hid.has(key)) return hid.get(key);
        return Reflect.apply(t, self, args);
      };
      hook(Object, 'getOwnPropertyDescriptor', gopdHandler);
      hook(Reflect, 'getOwnPropertyDescriptor', gopdHandler);
      hook(Object, 'getOwnPropertyDescriptors', (t, self, args) => {
        const owner = args[0];
        const r = Reflect.apply(t, self, args);
        if (owner === win) { try { delete r[MARKER]; } catch (e) {} }
        const hid = hiddenDescriptors.get(owner);
        if (hid) for (const kv of hid) r[kv[0]] = kv[1];
        return r;
      });
      hook(Object, 'getOwnPropertySymbols', (t, self, args) => {
        const r = Reflect.apply(t, self, args);
        if (args[0] === win) return r.filter(k => k !== MARKER);
        return r;
      });
      hook(Reflect, 'ownKeys', (t, self, args) => {
        const r = Reflect.apply(t, self, args);
        if (args[0] === win) return r.filter(k => k !== MARKER);
        return r;
      });
    }

    /* ------------------------------------------------------------------ */

    if (ENABLE_2D)      install2D();
    if (ENABLE_WEBGL)   installWebGL();
    if (ENABLE_WEBGPU)  installWebGPU();
    if (ENABLE_AUDIO)   installAudio();
    if (ENABLE_WORKERS) installWorkers();
    installConcealment();
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

    Every surface is an explicit opt-in. There is no implicit coupling
    between options and no derivation from browser launch mode; callers
    select exactly what they want applied. This keeps headed/headless
    execution mode and privacy/testing transformations independent, so a
    visible window does not silently change which hooks are installed.

    ``perturb_canvas``        2D pipeline (rgba-unorm8 and rgba-float16).
    ``spoof_webgl``           Identity, capability limits, per-extension
                              set, per-stage shader precision, RGBA and
                              RGBA_INTEGER readback.
    ``spoof_webgpu``          Adapter info, adapter/device limits, and
                              features, keyed on the same seed as WebGL.
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

    Register once with ``BrowserContext.add_init_script``. Cross-origin
    workers, service workers, and any worker started outside the wrapped
    constructors are not covered. Depth and stencil readbacks are never
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
