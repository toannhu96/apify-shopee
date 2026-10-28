"""Recovered, independently implemented vendor header components.

These are NOT a complete SAP signer. The cached-token branch cannot mint or
refresh a token; its result does not establish server acceptance. Provenance and
original-SDK comparisons are documented in docs/shopee-affiliate-api.md.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

CCD_KEY = 'shopee_webUnique_ccd'
MAX_CACHED_VALUE = 8192


def _invalid_constant(_value: str):
    raise ValueError('Invalid JSON constant')


def _cached_value(storage: Mapping[str, str]):
    if not isinstance(storage, Mapping):
        raise ValueError('Expected a storage mapping')
    raw = storage.get(CCD_KEY, '')
    if not isinstance(raw, str) or len(raw) > MAX_CACHED_VALUE:
        raise ValueError('Invalid cached storage value')
    try:
        return json.loads(raw, parse_int=float, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        return None


def _js_truthy(value) -> bool:
    # JSON cannot contain undefined. In JS empty arrays and objects are truthy.
    return not (value is None or value is False
                or isinstance(value, (int, float)) and value == 0
                or isinstance(value, str) and value == '')


def cached_degraded_token(session: Mapping[str, str], local: Mapping[str, str]) -> str:
    """Reproduce the tested cached fallback and final vendor ASCII gate.

    Session storage takes precedence using JavaScript truthiness. The SDK's
    fresh in-memory short-token path is outside this function. ASCII controls
    match the vendor gate but MUST be rejected separately before HTTP use.
    Both supplied mappings are validated, even when session wins. Inputs are
    never changed. Error messages never include stored values.
    """
    session_value = _cached_value(session)
    local_value = _cached_value(local)
    selected = session_value if _js_truthy(session_value) else local_value
    return selected if isinstance(selected, str) and selected and selected.isascii() else ''


def dat_from_samples(samples: Sequence[float]) -> str:
    """Exact vendor DAT transform: floor(256*x) for eight Math.random samples.

    Caller supplies samples so deterministic comparisons can preserve shared
    RNG ordering. This function does not reproduce JavaScript's RNG state.
    """
    if not isinstance(samples, Sequence) or len(samples) != 8:
        raise ValueError('Expected exactly eight random samples')
    if any(type(x) not in (int, float) or not 0 <= x < 1 or not math.isfinite(x)
           for x in samples):
        raise ValueError('Random samples must be finite numbers in [0, 1)')
    return bytes(math.floor(x * 256) for x in samples).hex()
