"""Download curated extensions from Google's update service and verify CRX3 signatures."""
import base64
import hashlib
import io
import json
import struct
import uuid
import zipfile
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

IDS = {'rakuten': 'chhjbpecpncaggjpdakmflnfcopglcmi',
       'ublock-lite': 'ddkjiahejlhfcafbddmgiahcphecmpfh',
       'google-translate': 'aapbdbdomjkkjkaonfhkkikfgjllcleb',
       'google-docs-offline': 'ghbmnnjooekpmoecnnnilnnbdlolhkhi'}


def fields(data):
    offset = 0
    def varint():
        nonlocal offset
        value = shift = 0
        while offset < len(data) and shift < 64:
            byte = data[offset]; offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise ValueError('Invalid CRX metadata')
    while offset < len(data):
        tag = varint()
        if tag & 7 == 2:
            length = varint()
            if offset + length > len(data):
                raise ValueError('Invalid CRX metadata length')
            yield tag >> 3, data[offset:offset+length]
            offset += length
        elif tag & 7 == 0:
            varint()
        else:
            raise ValueError('Unsupported CRX metadata')


def verified_zip(package, extension_id):
    if len(package) < 12 or package[:4] != b'Cr24' or struct.unpack_from('<I', package, 4)[0] != 3:
        raise ValueError('The store did not return a signed CRX3 extension')
    size = struct.unpack_from('<I', package, 8)[0]
    if size > 1024*1024 or size+12 >= len(package):
        raise ValueError('Invalid CRX header size')
    header = list(fields(package[12:12+size])); archive = package[12+size:]
    signed = next((value for tag, value in header if tag == 10000), None)
    if signed is None:
        raise ValueError('Missing CRX signed metadata')
    message = b'CRX3 SignedData\x00' + struct.pack('<I', len(signed)) + signed + archive
    for tag, value in header:
        if tag not in (2, 3):
            continue
        proof = dict(fields(value)); public = proof.get(1, b'')
        identity = ''.join(chr(97+int(c,16)) for c in hashlib.sha256(public).hexdigest()[:32])
        if identity != extension_id:
            continue
        key = serialization.load_der_public_key(public)
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(proof[2], message, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(proof[2], message, ec.ECDSA(hashes.SHA256()))
        else:
            continue
        return archive, base64.b64encode(public).decode()
    raise ValueError('Extension signature does not match the curated store ID')


async def install(folder, catalog_id):
    if catalog_id not in IDS:
        raise ValueError('Unknown extension catalog item')
    extension_id = IDS[catalog_id]
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream('GET', 'https://clients2.google.com/service/update2/crx', params={
            'response': 'redirect', 'prodversion': '154.0.0.0', 'acceptformat': 'crx3',
            'x': f'id={extension_id}&installsource=ondemand&uc',
        }) as response:
            response.raise_for_status()
            parts = []; size = 0
            async for part in response.aiter_bytes():
                size += len(part)
                if size > 100*1024*1024:
                    raise ValueError('Extension exceeds the 100 MB download limit')
                parts.append(part)
    archive, key = verified_zip(b''.join(parts), extension_id)
    root = (Path(folder)/'browser-extensions').resolve()
    destination = root/(catalog_id+'-'+uuid.uuid4().hex)
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        if sum(item.file_size for item in package.infolist()) > 300*1024*1024:
            raise ValueError('Extension exceeds the 300 MB unpacked limit')
        for item in package.infolist():
            target = (destination/item.filename).resolve()
            if not target.is_relative_to(destination) or ':' in item.filename or '\\' in item.filename:
                raise ValueError('Unsafe extension archive path')
        package.extractall(destination)
    manifest_path = destination/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['key'] = key
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return str(destination)
