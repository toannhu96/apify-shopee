"""Recovered SAP RI and SEC framing, not the browser fingerprint generator."""
from __future__ import annotations
import base64
import struct
from dataclasses import dataclass

ALPHABET = "shopEeSHOPkrIJ45KL02/376BM+NQcdRntquvU1VW89XDFTACGYwxZabfgijlmyz"
STANDARD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
VERSION = 20190001
MASK = 0xFFFFFFFF
P1, P2, P3, P4, P5 = 0x9E3779B1, 0x85EBCA77, 0xC2B2AE3D, 0x27D4EB2F, 0x165667B1
MAX_PAYLOAD = 65535
MAX_SEC_CHARS = 4 * ((MAX_PAYLOAD + 10 + 2) // 3)

def _bytes(value) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError('expected bytes')
    return bytes(value)

def _rotl(value: int, bits: int) -> int:
    value &= MASK
    return ((value << bits) | (value >> (32 - bits))) & MASK

def xxh32(data: bytes, seed: int = 0) -> int:
    """Independent implementation of the bundled XXH32 primitive."""
    data = _bytes(data)
    if type(seed) is not int or not 0 <= seed <= MASK:
        raise ValueError('seed must fit uint32')
    seed &= MASK
    cursor, length = 0, len(data)
    if length >= 16:
        lanes = [(seed + P1 + P2) & MASK, (seed + P2) & MASK, seed, (seed - P1) & MASK]
        while cursor <= length - 16:
            for i, word in enumerate(struct.unpack_from('<4I', data, cursor)):
                lanes[i] = (_rotl(lanes[i] + word * P2, 13) * P1) & MASK
            cursor += 16
        value = sum(_rotl(lane, bits) for lane, bits in zip(lanes, (1, 7, 12, 18))) & MASK
    else:
        value = (seed + P5) & MASK
    value = (value + length) & MASK
    while cursor <= length - 4:
        value = (_rotl(value + struct.unpack_from('<I', data, cursor)[0] * P3, 17) * P4) & MASK
        cursor += 4
    for byte in data[cursor:]:
        value = (_rotl(value + byte * P5, 11) * P1) & MASK
    value ^= value >> 15
    value = (value * P2) & MASK
    value ^= value >> 13
    value = (value * P3) & MASK
    value ^= value >> 16
    return value & MASK

def encode_ri(timestamp_seconds: int, entropy: bytes) -> str:
    """Build observed 26-byte RI from an explicit timestamp and 22 random bytes."""
    if type(timestamp_seconds) is not int or not 0 <= timestamp_seconds <= MASK:
        raise ValueError('timestamp must fit uint32 seconds')
    entropy = _bytes(entropy)
    if len(entropy) != 22:
        raise ValueError('RI requires 22 entropy bytes')
    result = bytearray(struct.pack('<I', timestamp_seconds) + bytes(entropy))
    result[11] = (result[11] & 15) | (3 << 4)
    result[12], result[13] = 5, 1
    return result.hex()

@dataclass(frozen=True)
class RI:
    timestamp_seconds: int
    xor_key: int
    protocol_version: int
    marker: int
    mode: int
    nonce: bytes
    raw: bytes

def decode_ri(header: str) -> RI:
    if not isinstance(header, str) or len(header) != 52:
        raise ValueError('RI must contain 52 hexadecimal characters')
    try:
        raw = bytes.fromhex(header)
    except ValueError as error:
        raise ValueError('invalid RI hex') from error
    if len(raw) != 26:
        raise ValueError('invalid RI byte length')
    return RI(struct.unpack_from('<I', raw)[0], raw[4], raw[11] >> 4, raw[12], raw[13], raw[14:], raw)

@dataclass(frozen=True)
class SEC:
    version: int
    checksum: int
    xor_key: int
    payload: bytes

def encode_sec(payload: bytes, xor_key: int, version: int = VERSION) -> str:
    """Wrap explicit payload bytes; does not manufacture browser fingerprint data."""
    payload = _bytes(payload)
    if type(xor_key) is not int or not 0 <= xor_key <= 255:
        raise ValueError('xor key must fit one byte')
    if len(payload) > 65535:
        raise ValueError('payload length must fit uint16')
    if type(version) is not int or version != VERSION:
        raise ValueError('unsupported SEC version')
    content = struct.pack('<IH', version, len(payload)) + bytes(payload)
    envelope = struct.pack('<I', xxh32(content, 0)) + content
    encoded = base64.b64encode(bytes(byte ^ xor_key for byte in envelope)).decode('ascii')
    return encoded.translate(str.maketrans(STANDARD, ALPHABET))

def decode_sec(header: str, ri_header: str | None = None) -> SEC:
    if not isinstance(header, str) or len(header) > MAX_SEC_CHARS:
        raise ValueError('invalid SEC header type or size')
    try:
        translated = header.translate(str.maketrans(ALPHABET, STANDARD))
        encoded = base64.b64decode(translated, validate=True)
        if base64.b64encode(encoded).decode('ascii') != translated:
            raise ValueError('noncanonical base64')
    except (ValueError, TypeError) as error:
        raise ValueError('invalid SEC base64') from error
    if len(encoded) < 10:
        raise ValueError('SEC envelope is too short')
    key = decode_ri(ri_header).xor_key if ri_header is not None else encoded[4] ^ (VERSION & 255)
    raw = bytes(byte ^ key for byte in encoded)
    checksum, version, length = struct.unpack_from('<IIH', raw)
    if version != VERSION or len(raw) != 10 + length:
        raise ValueError('SEC version or length mismatch')
    if xxh32(raw[4:], 0) != checksum:
        raise ValueError('SEC checksum mismatch')
    return SEC(version, checksum, key, raw[10:])

def inspect_flatbuffer(payload: bytes) -> dict:
    """Structural root/vtable inspection only; field types are not inferred."""
    payload = _bytes(payload)
    if len(payload) < 8 or len(payload) > MAX_PAYLOAD:
        raise ValueError('truncated flatbuffer')
    root = struct.unpack_from('<I', payload)[0]
    if root < 4 or root % 4 or root + 4 > len(payload):
        raise ValueError('invalid root offset')
    vtable = root - struct.unpack_from('<i', payload, root)[0]
    if vtable < 0 or vtable % 2 or vtable + 4 > len(payload):
        raise ValueError('invalid vtable offset')
    size, object_size = struct.unpack_from('<HH', payload, vtable)
    if size < 4 or size % 2 or object_size < 4 or vtable + size > len(payload) or root + object_size > len(payload):
        raise ValueError('invalid vtable size')
    if max(root, vtable) < min(root + object_size, vtable + size):
        raise ValueError('overlapping vtable and object')
    offsets = list(struct.unpack_from('<' + 'H' * ((size - 4) // 2), payload, vtable + 4))
    if any(offset and not 4 <= offset < object_size for offset in offsets):
        raise ValueError('invalid field offset')
    return {'root_offset': root, 'vtable_offset': vtable, 'vtable_size': size, 'object_size': object_size, 'field_offsets': offsets}
