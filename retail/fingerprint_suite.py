"""Independent upstream fingerprint-suite adapter; no custom hooks are composed."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = '2.1.88'


async def build_profile(seed, locale, browser_version):
    node = shutil.which('node')
    if not node or not (ROOT / 'node_modules/fingerprint-injector/package.json').is_file():
        raise ValueError('Fingerprint-suite requires Node.js and its optional packages. Run npm ci --ignore-scripts in the project directory.')
    operating_system = {'win32': 'windows', 'darwin': 'macos', 'linux': 'linux'}.get(sys.platform)
    if not operating_system:
        raise ValueError('Fingerprint-suite supports desktop Windows, macOS and Linux profiles')
    request = {'seed': int(seed) & 0xffffffff, 'locale': locale,
               'browserMajor': int(browser_version.split('.')[0]), 'operatingSystem': operating_system}
    process = await asyncio.create_subprocess_exec(
        node, str(ROOT / 'scripts/fingerprint_suite.cjs'), cwd=ROOT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), 30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError('Fingerprint-suite generation failed: ' + stderr.decode(errors='replace')[:2000])
    profile = json.loads(stdout)
    if profile.get('schema') != 1 or profile.get('versions') != {'generator': VERSION, 'injector': VERSION}:
        raise ValueError('Fingerprint-suite package/schema mismatch; reinstall with npm ci --ignore-scripts')
    if not profile.get('scripts') or not profile.get('options', {}).get('user_agent'):
        raise ValueError('Fingerprint-suite returned an incomplete profile')
    profile['digest'] = hashlib.sha256(stdout).hexdigest()
    return profile
