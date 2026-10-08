"""Launch genuine installed browser identities and validated unpacked extensions."""
import json
import os
from pathlib import Path


def browser_options(settings):
    identity = settings.get('browser_identity', 'default')
    if settings.get('fingerprint_backend') == 'native':
        if identity not in ('default', 'chrome'):
            raise ValueError('Native Chromium uses its own engine. Choose JavaScript compatibility mode to launch genuine Edge, Brave or Opera.')
        return {}
    channel = settings.get('browser_channel', 'chromium') if identity == 'default' else identity
    if channel in ('brave', 'opera'):
        explicit = settings.get(channel+'_executable', '')
        relative = 'BraveSoftware/Brave-Browser/Application/brave.exe' if channel == 'brave' else 'Programs/Opera/opera.exe'
        candidates = [Path(explicit)] if explicit else [Path(os.environ[root])/relative for root in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)') if os.environ.get(root)]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is None:
            raise ValueError(f'{channel.title()} is not installed. Set its executable in global Browser settings.')
        return {'executable_path': str(executable.resolve())}
    # Full Chromium supports extensions in headless mode; the headless shell does not.
    return {'channel': channel}


def extension_paths(store, ids):
    paths = []
    for extension_id in dict.fromkeys(ids):
        record = store.get('browser_extensions', extension_id)
        if not record:
            raise ValueError('A selected extension no longer exists. Update the browser extension selection.')
        path = validate_extension_path(record['path'])
        paths.append(str(path))
    return paths


def validate_extension_path(value):
    path = Path(value).expanduser().resolve()
    manifest_path = path/'manifest.json'
    if not manifest_path.is_file() or manifest_path.stat().st_size > 1024*1024:
        raise ValueError('Choose an unpacked extension folder containing manifest.json')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise ValueError('The extension manifest is unreadable')
    if manifest.get('manifest_version') not in (2, 3) or not manifest.get('name') or not manifest.get('version'):
        raise ValueError('Invalid Chromium extension manifest')
    return path


CATALOG = [
    {'id': 'rakuten', 'name': 'Rakuten Cash Back', 'url': 'https://www.rakuten.com/button.htm'},
    {'id': 'ublock-lite', 'name': 'uBlock Origin Lite', 'url': 'https://chromewebstore.google.com/detail/ddkjiahejlhfcafbddmgiahcphecmpfh'},
    {'id': 'google-translate', 'name': 'Google Translate', 'url': 'https://chromewebstore.google.com/detail/aapbdbdomjkkjkaonfhkkikfgjllcleb'},
    {'id': 'google-docs-offline', 'name': 'Google Docs Offline', 'url': 'https://chromewebstore.google.com/detail/ghbmnnjooekpmoecnnnilnnbdlolhkhi'},
]
