"""Install the pinned optional Windows browser; does not change app settings."""
import argparse
import hashlib
from pathlib import Path
import sys
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retail.native_fingerprint import ARCHIVE, ARCHIVE_SHA256, DEFAULT_DIRECTORY, DOWNLOAD_URL


def install(archive=None):
    if sys.platform != 'win32':
        raise RuntimeError('This pinned native browser build supports Windows only')
    if DEFAULT_DIRECTORY.exists():
        raise FileExistsError(f'Install directory already exists: {DEFAULT_DIRECTORY}')
    DEFAULT_DIRECTORY.parent.mkdir(parents=True, exist_ok=True)
    if archive is None:
        archive = DEFAULT_DIRECTORY.parent / ARCHIVE
        if not archive.exists():
            print('Downloading the pinned portable browser (about 1.6 GB)...', flush=True)
            partial = archive.with_suffix('.zip.part')
            with urllib.request.urlopen(DOWNLOAD_URL, timeout=60) as response, partial.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            partial.replace(archive)
    archive = Path(archive)
    with archive.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != ARCHIVE_SHA256:
        raise ValueError('Browser archive SHA-256 does not match the pinned release')
    destination = DEFAULT_DIRECTORY.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination):
                raise ValueError(f'Unsafe archive member: {member.filename}')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Browser archive must not contain symbolic links')
        destination.mkdir()
        bundle.extractall(destination)
    if not (destination / 'chrome.exe').is_file():
        raise ValueError('Installed archive is missing chrome.exe')
    print(f'Installed verified archive at {destination}')
    print('This third-party Chromium build disables Safe Browsing. App settings were not changed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Verify and extract an already downloaded release ZIP')
    install(parser.parse_args().archive)
