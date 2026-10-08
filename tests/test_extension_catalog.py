import hashlib
import struct

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from retail.extension_catalog import verified_zip


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def test_crx_checks_publisher_identity_and_archive_signature():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashlib.sha256(public).digest()[:16]
    identity = ''.join(chr(97 + int(char, 16)) for char in digest.hex())
    signed = field(1, digest)
    archive = b'fixture archive bytes'
    message = b'CRX3 SignedData\x00' + struct.pack('<I', len(signed)) + signed + archive
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    header = field(2, field(1, public) + field(2, signature)) + field(10000, signed)
    package = b'Cr24' + struct.pack('<II', 3, len(header)) + header + archive
    assert verified_zip(package, identity)[0] == archive
    with pytest.raises(ValueError, match='curated store ID'):
        verified_zip(package, 'a' * 32)
    with pytest.raises(InvalidSignature):
        verified_zip(package[:-1] + b'X', identity)
    with pytest.raises(ValueError):
        verified_zip(package[:16], identity)
