"""Explicit, immutable SAP request history for a single managed account session.

The returned dictionaries contain sensitive headers and must be encrypted at rest.
Persist each reservation before sending it; a timeout must not roll it back. These
functions do not acquire locks, send requests or renew cookies/device tokens.
"""
from __future__ import annotations

import secrets
import struct
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from .http_transport import product_url
from .sap_codec import decode_ri, decode_sec, encode_ri
from .sap_collections import (
    collection_request_state,
    parse_collection,
    refresh_collection,
)
from .sap_crypto import decode_collection
from .sap_refresh import refresh_root
from .sap_request import derive_request_key, xor_repeating
from .sap_signer import _seed
from .sap_state import decode_payload

UINT32_MAX = 2**32 - 1
WINDOW_MS = 120_000
STATE_KEYS = frozenset({'version', 'headers', 'clock_anchor_ms', 'last_ms', 'path_count',
                       'window_started_ms', 'window_count'})


def _clock(value: int | None) -> int:
    result = time.time_ns() // 1_000_000 if value is None else value
    if type(result) is not int or not 0 <= result < 2**32 * 1000:
        raise ValueError('Invalid signing clock')
    return result


def _metadata(headers):
    raw = decode_payload(decode_sec(headers['x-sap-sec'], headers['x-sap-ri']).payload,
                         headers['x-sap-ri'])
    vectors = tuple(bytes.fromhex(value['hex']) for value in raw['vectors'])
    mask, random = vectors[1], vectors[14]
    timestamp = int.from_bytes(xor_repeating(vectors[4], mask), 'little')
    if timestamp // 1000 != decode_ri(headers['x-sap-ri']).timestamp_seconds:
        raise ValueError('Signing seed timestamps disagree')
    custom = decode_collection(vectors[12], vectors[13], 'custom', mask, random)
    request = collection_request_state(custom, mask)
    salsa = decode_collection(vectors[15], vectors[11], 'salsa', mask, random)
    salsa_values = {v['pc']: v['value'] for v in parse_collection('salsa', salsa)}
    mask32 = int.from_bytes(mask[:4], 'little')
    counters = [request['path_count']] + [salsa_values[key] ^ mask32 ^ constant
        for key, constant in ((36744, 3882804305), (35838, 604268431), (37224, 1471809388))]
    return timestamp, request, raw['canonical_fields'][5]['value'], counters


def initialize_session(seed_headers: Mapping[str, str], now_ms: int | None = None) -> dict:
    """Import one accepted seed, recovering its clock and first-request window.

Recent non-first seeds have an unknowable window anchor and are rejected. After
two minutes their next request can reset that window. Elapsed time in the seed
has only whole seconds, preserving the existing client's subsecond uncertainty.
"""
    now = _clock(now_ms)
    try:
        headers = _seed(seed_headers)
        last, request, window_rate, _ = _metadata(headers)
        anchor = last - request['elapsed_seconds'] * 1000
        if now < last or anchor < 0 or not 1 <= request['path_count'] <= UINT32_MAX:
            raise ValueError('Invalid seed clock or counters')
        if request['path_count'] == 1 and window_rate == 1:
            window_start, window_count = last, 1
        elif now - last > WINDOW_MS:
            window_start, window_count = now, 0
        else:
            raise ValueError('Export the first product request after a page reload')
        return {'version': 1, 'headers': headers, 'clock_anchor_ms': anchor,
                'last_ms': last, 'path_count': request['path_count'],
                'window_started_ms': window_start, 'window_count': window_count}
    except (KeyError, TypeError, OverflowError, IndexError):
        raise ValueError('Signing seed is invalid') from None


def _validated_state(state: Mapping, now: int) -> dict:
    if not isinstance(state, Mapping) or set(state) != STATE_KEYS:
        raise ValueError('Invalid signing state shape')
    numeric = STATE_KEYS - {'headers'}
    if any(type(state[k]) is not int for k in numeric) or state['version'] != 1:
        raise ValueError('Invalid signing state version or counters')
    headers = _seed(state['headers'])
    last, request, window_rate, counters = _metadata(headers)
    if (last != state['last_ms'] or not state['clock_anchor_ms'] <= last <= now
            or state['clock_anchor_ms'] < 0
            or (last - state['clock_anchor_ms']) // 1000 != request['elapsed_seconds']
            or request['path_count'] != state['path_count']
            or not 1 <= state['path_count'] < UINT32_MAX
            or any(count >= UINT32_MAX for count in counters)):
        raise ValueError('Signing state clock or counters disagree')
    count, start = state['window_count'], state['window_started_ms']
    if not 0 <= count <= state['path_count'] or not 0 <= start <= now:
        raise ValueError('Invalid signing window')
    if count == 0:
        if start - last <= WINDOW_MS:
            raise ValueError('A reset window requires an aged seed')
    else:
        if not start <= last or last - start > WINDOW_MS:
            raise ValueError('Invalid signing window anchor')
        expected = 60 * count / max(60, (last - start) // 1000)
        if struct.pack('<f', expected) != struct.pack('<f', window_rate):
            raise ValueError('Signing window rate disagrees with headers')
    return {**state, 'headers': headers}


def reserve_request(item_id: str | int, state: Mapping,
                    now_ms: int | None = None) -> tuple[dict[str, str], dict]:
    """Return fresh headers and the next state without mutating the supplied state.

The caller must hold an exclusive, fenced per-account lease and durably store
the new state before sending the request. Reusing an old reservation is unsafe.
"""
    url, now = urlsplit(product_url(item_id)), _clock(now_ms)
    try:
        current = _validated_state(state, now)
        elapsed = (now - current['clock_anchor_ms']) // 1000
        if elapsed > UINT32_MAX:
            raise ValueError('Signing elapsed counter overflow')
        start, count = current['window_started_ms'], current['window_count']
        start, count = (now, 1) if count == 0 or now - start > WINDOW_MS else (start, count + 1)
        headers, total = current['headers'], current['path_count'] + 1
        seconds = now // 1000
        ri = encode_ri(seconds, secrets.token_bytes(22))
        signed = refresh_root(headers['x-sap-sec'], headers['x-sap-ri'], ri,
            new_mask=derive_request_key(seconds, secrets.token_bytes(4)),
            new_random=secrets.token_bytes(8), raw_url=url.path + '?' + url.query,
            timestamp_ms=now, refresh_collection=refresh_collection, advance_request=True,
            request_rate=60 * total / max(60, elapsed), elapsed_seconds=elapsed,
            root_request_rate=60 * count / max(60, (now - start) // 1000))
        fresh = {**headers, **signed, 'af-ac-enc-dat': secrets.token_bytes(8).hex()}
        next_state = {**current, 'headers': dict(fresh), 'last_ms': now, 'path_count': total,
                      'window_started_ms': start, 'window_count': count}
        return fresh, next_state
    except (KeyError, TypeError, OverflowError, IndexError):
        raise ValueError('Signing state is invalid') from None
