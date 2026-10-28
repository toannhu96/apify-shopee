"""Original SDK module4078 detector block, reconstructed from synthetic traces.

Constructor VM27919..28170 sizes random padding from key[5] and selects its
start using key[7]. VtO VM31501..31600 inserts twenty ordered detector bytes.
The values are collected device facts, not invented defaults. This module
can preserve them from a caller-owned state export without evaluating JS.
"""
import secrets


def _layout(key: bytes) -> tuple[int, int]:
    if not isinstance(key, bytes) or len(key) != 8:
        raise ValueError('detector key must contain exactly eight bytes')
    length = 21 + key[5] % 20
    return length, key[7] % length + 2


def decode_detectors(block: bytes, key: bytes) -> bytes:
    """Recover the twenty ordered raw detector values from an encoded block."""
    length, start = _layout(key)
    if not isinstance(block, bytes) or len(block) != length:
        raise ValueError('detector block length does not match its key')
    return bytes(block[(start+i) % length] ^ key[((start+i) % length) % 8]
                 for i in range(20))


def encode_detectors(values: bytes, key: bytes, padding: bytes | None = None) -> bytes:
    """Encode collected detector values with fresh padding or oracle padding."""
    length, start = _layout(key)
    if not isinstance(values, bytes) or len(values) != 20:
        raise ValueError('exactly twenty raw detector bytes are required')
    if padding is None:
        padding = secrets.token_bytes(length)
    if not isinstance(padding, bytes) or len(padding) != length:
        raise ValueError('padding length does not match its key')
    positions = {(start+i) % length: values[i] for i in range(20)}
    return bytes(positions[p] ^ key[p % 8] if p in positions else padding[p]
                 for p in range(length))
