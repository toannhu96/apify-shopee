"""Recovered SAP collection ciphers and source-derived key assembly.

The fixed constant is public protocol data from the captured SDK, not an account
credential. This module does not acquire or synthesize device collection inputs.
"""
from .sap_codec import xxh32
from .sap_request import xor_repeating
from .sap_salsa20 import salsa20

BASE_KEY = bytes.fromhex('17e88cde461c0050af4fb3d230d667ccdb31ff62d513a6deac996ab4d2223966')
INITIAL_STATE = (
    41, 82, 74, 163, 214, 66, 204, 104, 1, 87, 212, 142, 77, 75, 15, 37,
    80, 210, 235, 160, 247, 40, 232, 3, 198, 124, 219, 157, 20, 166, 192, 11,
    229, 94, 236, 78, 88, 114, 253, 122, 61, 125, 138, 209, 105, 251, 139, 147,
    165, 123, 244, 38, 26, 65, 100, 115, 151, 71, 218, 137, 140, 191, 85, 46,
    190, 42, 81, 179, 22, 211, 56, 51, 92, 167, 93, 245, 184, 112, 116, 32,
    144, 145, 111, 95, 33, 132, 84, 103, 199, 6, 195, 24, 18, 68, 158, 196,
    19, 222, 48, 129, 16, 155, 53, 59, 186, 220, 153, 148, 187, 161, 130, 213,
    72, 248, 73, 227, 45, 216, 205, 29, 101, 230, 5, 79, 23, 76, 91, 21,
    156, 208, 223, 149, 243, 28, 201, 181, 9, 60, 13, 136, 135, 169, 90, 62,
    241, 108, 159, 34, 168, 164, 98, 174, 133, 215, 221, 154, 49, 7, 143, 55,
    128, 225, 240, 183, 30, 12, 31, 126, 234, 200, 110, 182, 162, 70, 239, 206,
    43, 170, 89, 120, 86, 58, 180, 25, 172, 173, 252, 121, 10, 83, 47, 217,
    141, 231, 178, 197, 39, 113, 57, 226, 233, 238, 175, 188, 14, 69, 224, 67,
    117, 246, 36, 99, 102, 27, 189, 44, 50, 17, 203, 2, 54, 35, 63, 177,
    194, 134, 176, 249, 150, 118, 0, 107, 146, 171, 228, 96, 202, 64, 131, 4,
    207, 254, 185, 242, 255, 250, 193, 106, 237, 8, 152, 52, 127, 97, 119, 109,
)


def derive_keys(request_mask: bytes, request_random: bytes) -> dict[str, bytes]:
    if any(not isinstance(v, bytes) or len(v) != 8 for v in (request_mask, request_random)):
        raise ValueError('Request mask and random input must each contain eight bytes')
    return {'custom': BASE_KEY[16:] + request_mask + request_random,
            'salsa': BASE_KEY[:16] + request_random + request_mask,
            'nonce': BASE_KEY[16:24]}


def _swap(state, i, j):
    if i == j:
        return state
    low, high = sorted((i, j))
    return state[:low] + (state[high],) + state[low + 1:high] + (state[low],) + state[high + 1:]


def custom_stream(data: bytes, key: bytes) -> bytes:
    """Exact fixed-permutation module 6955 transform, resetting state per call."""
    if not isinstance(data, bytes) or not isinstance(key, bytes) or not key:
        raise ValueError('Data and nonempty key must be bytes')

    def stream():
        state = INITIAL_STATE
        i = j = 0
        for byte in data:
            i = (i + 1) & 255
            j = (j + (state[i] ^ key[i % len(key)])) & 255
            state = _swap(state, i, j)
            index = ((state[j] ^ key[i % len(key)]) + (state[i] ^ key[j % len(key)])) & 255
            yield byte ^ state[index] ^ key[index % len(key)]
    return bytes(stream())


def _transform(data, kind, mask, random):
    keys = derive_keys(mask, random)
    if kind == 'custom':
        return custom_stream(data, keys['custom'])
    if kind == 'salsa':
        return salsa20(data, keys['salsa'], keys['nonce'])
    raise ValueError('Unsupported collection kind')


def encode_collection(data: bytes, kind: str, mask: bytes, random: bytes) -> tuple[bytes, bytes]:
    ciphertext = _transform(data, kind, mask, random)
    checksum = xxh32(ciphertext).to_bytes(4, 'little')
    return xor_repeating(ciphertext, mask), xor_repeating(checksum, mask)


def decode_collection(data: bytes, checksum: bytes, kind: str, mask: bytes, random: bytes) -> bytes:
    derive_keys(mask, random)
    if not isinstance(checksum, bytes) or len(checksum) != 4:
        raise ValueError('Invalid collection checksum')
    ciphertext = xor_repeating(data, mask)
    expected = xxh32(ciphertext).to_bytes(4, 'little')
    if xor_repeating(checksum, mask) != expected:
        raise ValueError('Collection checksum mismatch')
    return _transform(ciphertext, kind, mask, random)
