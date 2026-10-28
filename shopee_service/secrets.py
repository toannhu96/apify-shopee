"""Authenticated encryption for backend-only session records."""
from __future__ import annotations

import json
import math

from cryptography.fernet import Fernet, InvalidToken

MAX_PLAINTEXT_BYTES = 2 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = 3 * 1024 * 1024


class SecretError(ValueError):
    """Safe encryption/decryption failure without credential contents."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key')
        result[key] = value
    return result


def _reject(_):
    raise ValueError('Invalid constant')


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Nonfinite number')
    return result


class Cipher:
    def __init__(self, key: str | bytes):
        try:
            self._cipher = Fernet(key.encode('ascii') if isinstance(key, str) else key)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise SecretError('Encryption key is invalid') from None

    def encrypt(self, value: dict) -> str:
        try:
            if not isinstance(value, dict):
                raise TypeError()
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
            if len(raw) > MAX_PLAINTEXT_BYTES:
                raise ValueError()
            return self._cipher.encrypt(raw).decode('ascii')
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise SecretError('Session could not be encrypted') from None

    def decrypt(self, value: str) -> dict:
        try:
            if not isinstance(value, str) or len(value) > MAX_CIPHERTEXT_BYTES:
                raise ValueError()
            raw = self._cipher.decrypt(value.encode('ascii'))
            if len(raw) > MAX_PLAINTEXT_BYTES:
                raise ValueError()
            result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                parse_constant=_reject, parse_float=_float)
            if not isinstance(result, dict):
                raise TypeError()
            return result
        except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError):
            raise SecretError('Stored session could not be decrypted') from None
