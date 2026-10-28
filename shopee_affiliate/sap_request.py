"""Recovered request-field transforms, separate from device-state collection."""
from __future__ import annotations

import struct

from .sap_codec import MASK, decode_ri, xxh32


def _data(value, length: int | None = None) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError('Expected bytes')
    result = bytes(value)
    if length is not None and len(result) != length:
        raise ValueError('Invalid byte length')
    return result


def derive_request_key(timestamp_seconds: int, random_four: bytes) -> bytes:
    """Interleave each timestamp byte XOR random byte with that random byte."""
    if type(timestamp_seconds) is not int or not 0 <= timestamp_seconds <= MASK:
        raise ValueError('Timestamp must fit uint32 seconds')
    random_four = _data(random_four, 4)
    timestamp = struct.pack('<I', timestamp_seconds)
    return bytes(value for a, b in zip(timestamp, random_four) for value in (a ^ b, b))


def recover_key_timestamp(key: bytes) -> int:
    key = _data(key, 8)
    return int.from_bytes(bytes(key[i] ^ key[i + 1] for i in range(0, 8, 2)), 'little')


def xor_repeating(value: bytes, key: bytes) -> bytes:
    value, key = _data(value), _data(key)
    if not key:
        raise ValueError('XOR key cannot be empty')
    return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(value))


def request_url_digest(raw_url: str, salt_four: bytes) -> bytes:
    """Unpadded lowerhex ASCII of XXH32(UTF8(raw URL) + four salt bytes).

    The supplied URL is used exactly: no host/path/query normalization occurs.
    """
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 8192:
        raise ValueError('Invalid URL string')
    salt_four = _data(salt_four, 4)
    return format(xxh32(raw_url.encode('utf-8') + salt_four, 0), 'x').encode('ascii')


def encode_request_timestamp(timestamp_ms: int, key: bytes) -> bytes:
    if type(timestamp_ms) is not int or not 0 <= timestamp_ms < 1 << 64:
        raise ValueError('Timestamp must fit uint64 milliseconds')
    return xor_repeating(struct.pack('<Q', timestamp_ms), _data(key, 8))


def derive_slot_rotation(ri_header: str) -> int:
    """Observed rotation of the 57 schema fields for this SDK revision."""
    ri = decode_ri(ri_header)
    return (ri.timestamp_seconds + ri.raw[22] + ri.raw[23]) % 57
