"""Recovered module 6942 Salsa20/20 primitive; SAP key derivation is separate."""
import struct

MASK = (1 << 32) - 1
ROUNDS = ((0, 4, 8, 12), (5, 9, 13, 1), (10, 14, 2, 6), (15, 3, 7, 11),
          (0, 1, 2, 3), (5, 6, 7, 4), (10, 11, 8, 9), (15, 12, 13, 14))


def _rotate(word, count):
    word &= MASK
    return ((word << count) | (word >> (32 - count))) & MASK


def _replace(words, index, value):
    return words[:index] + (value,) + words[index + 1:]


def _quarter(words, a, b, c, d):
    words = _replace(words, b, words[b] ^ _rotate(words[a] + words[d], 7))
    words = _replace(words, c, words[c] ^ _rotate(words[b] + words[a], 9))
    words = _replace(words, d, words[d] ^ _rotate(words[c] + words[b], 13))
    return _replace(words, a, words[a] ^ _rotate(words[d] + words[c], 18))


def _block(key_words, nonce_words, counter):
    constants = (1634760805, 857760878, 2036477234, 1797285236)
    state = (constants[0], *key_words[:4], constants[1], *nonce_words,
             counter & MASK, counter >> 32, constants[2], *key_words[4:], constants[3])
    words = state
    for _ in range(10):
        for indices in ROUNDS:
            words = _quarter(words, *indices)
    return struct.pack('<16I', *((a + b) & MASK for a, b in zip(words, state)))


def salsa20(data: bytes, key: bytes, nonce: bytes, counter: int = 0) -> bytes:
    """Translate the captured SDK primitive for explicit data/key/nonce inputs."""
    if not all(isinstance(value, bytes) for value in (data, key, nonce)):
        raise ValueError('Data, key, and nonce must be bytes')
    if len(key) != 32 or len(nonce) != 8:
        raise ValueError('Salsa20 requires a 32-byte key and 8-byte nonce')
    if type(counter) is not int or not 0 <= counter < 1 << 64:
        raise ValueError('Counter must fit uint64')
    key_words = struct.unpack('<8I', key)
    nonce_words = struct.unpack('<2I', nonce)
    return b''.join(bytes(a ^ b for a, b in zip(data[start:start + 64],
        _block(key_words, nonce_words, (counter + start // 64) % (1 << 64))))
        for start in range(0, len(data), 64))
