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


def launch_options(settings, seed=None):
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
    return {'executable_path': str(executable), 'args': args,
            'headless': not settings.get('show_browser_window', False)}


def needs_profile_browser(settings):
    return any(settings.get(f'fingerprint_{surface}') for surface in ('canvas', 'webgl', 'webgpu'))
