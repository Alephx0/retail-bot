"""Launch configuration for the pinned, separately distributed native backend.

Upstream source: https://github.com/pppi21/anti-fingerprint-browser (BSD-3-Clause).
The executable is an optional dependency, never downloaded during task browsing.
Native GPU identity is shared by WebGL, WebGPU and worker realms. Audio keeps
the existing valid-native-values policy instead of overriding requested rates.
"""
from pathlib import Path

VERSION = '153.0.8010.47-1'
ARCHIVE = f'chromium-{VERSION}.zip'
DOWNLOAD_URL = f'https://github.com/pppi21/anti-fingerprint-browser/releases/download/{VERSION}/{ARCHIVE}'
ARCHIVE_SHA256 = 'b2be4f085c196e76de39420f22c1dcbc3bbfe9d6dc49ec95f309fb8206549e3c'
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / 'browser-data' / f'native-chromium-{VERSION}'


def launch_options(settings, seed=None, profile_values=None):
    if settings.get('cdp_attach'):
        raise ValueError('Native fingerprint profiles require an app-managed browser; disable external CDP attachment.')
    executable = Path(settings.get('native_browser_executable') or DEFAULT_DIRECTORY / 'chrome.exe').expanduser().resolve()
    if not executable.is_file():
        raise ValueError('Native fingerprint browser is missing. Run scripts/install_native_browser.py or choose its executable in Settings.')
    args = ['--use-chromium-defaults']
    if seed is not None:
        # Upstream switch parsing uses int32, while account seeds use uint32.
        seed = seed & 0xffffffff
        if seed >= 2**31:
            seed -= 2**32
        switches = []
        if settings.get('fingerprint_canvas'):
            switches += ['canvas-curve-noise', 'canvas-blur-noise', 'canvas-gradient-noise']
        if settings.get('fingerprint_webgl') or settings.get('fingerprint_webgpu'):
            switches += ['fingerprint-gpu-vendor', 'fingerprint-gpu-renderer',
                         'fingerprint-webgpu-vendor', 'fingerprint-webgpu-architecture']
        if settings.get('fingerprint_webgl'):
            switches += ['webgl-shader-noise']
        args += [f'--{switch}=seed:{seed}' for switch in switches]
        if settings.get('fingerprint_webgl'):
            # Vary interpolated colors in the rendered framebuffer. Preserve
            # analytic math, fragment coordinates and texture sampling so
            # exact rendering operations keep their platform semantics.
            args += ['--webgl-shader-noise-k=kt=0,kt2=0,kp=0,ke=0,klg=0,kl=0,kn=0,kr=0,kx=0,kss=0,km=0,kf=0,kc=2']
        values = profile_values or {}
        if settings.get('fingerprint_navigator'):
            args += [f'--fingerprint-hardware-concurrency={values.get("cpu", "seed:"+str(seed))}',
                     f'--fingerprint-device-memory={values.get("memory", "seed:"+str(seed))}',
                     '--fingerprint-brand=Chrome']
        if values.get('gpu') and (settings.get('fingerprint_webgl') or settings.get('fingerprint_webgpu')):
            # Retain the physical vendor and WebGPU architecture. Only offer
            # renderer aliases from that device's hardware family.
            args = [arg for arg in args if not arg.startswith(('--fingerprint-gpu-', '--fingerprint-webgpu-'))]
            if values.get('gpu_choice') != 'native':
                args += ['--fingerprint-gpu-renderer='+values['gpu']]
        if settings.get('fingerprint_canvas') and values.get('canvas_noise') == .1:
            args = [arg for arg in args if not arg.startswith(('--canvas-curve-noise=', '--canvas-blur-noise=', '--canvas-gradient-noise='))]
            args += ['--canvas-curve-noise=0.003', '--canvas-blur-noise=0.005', '--canvas-gradient-noise=0.003']
        if settings.get('fingerprint_webgl') and values.get('webgl_noise') in (0, .05):
            args = [arg for arg in args if not arg.startswith('--webgl-shader-noise=')]
            if values['webgl_noise']:
                args += ['--webgl-shader-noise=0.001']
    if settings.get('fingerprint_timezone'):
        args += ['--fingerprint-timezone='+settings['fingerprint_timezone']]
    return {'executable_path': str(executable), 'args': args,
            'headless': not settings.get('show_browser_window', False)}


def needs_profile_browser(settings):
    return any(settings.get(f'fingerprint_{surface}') for surface in ('canvas', 'webgl', 'webgpu'))
