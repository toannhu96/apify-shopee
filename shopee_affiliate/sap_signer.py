"""Refresh a caller-owned SAP state seed without a browser or vendor JavaScript.

Device facts and the separate cached token come from the supplied seed. This
does not mint login cookies, issue tokens or manufacture a browser fingerprint.
"""
from collections.abc import Mapping
import secrets
import time
from urllib.parse import urlsplit

from .http_transport import _request_headers, product_url
from .sap_codec import decode_ri, decode_sec, encode_ri
from .sap_crypto import decode_collection
from .sap_collections import collection_request_state
from .sap_refresh import refresh_root
from .sap_request import derive_request_key, recover_key_timestamp, xor_repeating
from .sap_state import decode_payload

REQUIRED = frozenset({'x-sap-ri', 'x-sap-sec', 'af-ac-enc-sz-token',
                      'user-agent', 'x-sz-sdk-version'})
SUPPORTED_SDK = '1.12.21'


def _seed(headers: Mapping[str, str]) -> dict[str, str]:
    # Shared wire validation rejects cookies, pseudo headers and unknown names.
    _request_headers(headers, 'validation=1')
    result = {key.lower(): value for key, value in headers.items()}
    if not REQUIRED <= result.keys() or any(not result[key] for key in REQUIRED):
        raise ValueError('Signing seed is missing required headers')
    if result['x-sz-sdk-version'] != SUPPORTED_SDK:
        raise ValueError('Unsupported signing SDK version')
    ri = decode_ri(result['x-sap-ri'])
    if (ri.protocol_version, ri.marker, ri.mode) != (3, 5, 1):
        raise ValueError('Unsupported signing seed version')
    state = decode_payload(decode_sec(result['x-sap-sec'], result['x-sap-ri']).payload,
                           result['x-sap-ri'])
    mask = bytes.fromhex(state['vectors'][1]['hex'])
    if recover_key_timestamp(mask) != ri.timestamp_seconds:
        raise ValueError('Signing seed timestamps disagree')
    host = xor_repeating(bytes.fromhex(state['vectors'][7]['hex']), mask)
    if host != b'affiliate.shopee.vn':
        raise ValueError('Signing seed belongs to a different origin')
    return result


def _collection_refresher():
    from .sap_collections import refresh_collection
    return refresh_collection


def _request_clock(headers, timestamp_ms):
    state = decode_payload(decode_sec(headers['x-sap-sec'], headers['x-sap-ri']).payload,
                           headers['x-sap-ri'])
    vectors = tuple(bytes.fromhex(value['hex']) for value in state['vectors'])
    mask, random = vectors[1], vectors[14]
    seed_ms = int.from_bytes(xor_repeating(vectors[4], mask), 'little')
    if timestamp_ms < seed_ms:
        raise ValueError('Current time precedes the signing seed')
    request = collection_request_state(
        decode_collection(vectors[12], vectors[13], 'custom', mask, random), mask)
    # The seed exposes whole elapsed seconds, not the original performance.now
    # fraction. Continue its clock with wall-clock delta; uncertainty is <1 s.
    elapsed = request['elapsed_seconds'] + (timestamp_ms - seed_ms) // 1000
    if elapsed >= 1 << 32 or request['path_count'] >= (1 << 32) - 1:
        raise ValueError('Signing seed counters exceed supported range')
    rate = 60 * (request['path_count'] + 1) / max(60, elapsed)
    delta_ms = timestamp_ms - seed_ms
    if delta_ms > 120000:
        window_rate = 1.0
    elif request['path_count'] == 1 and state['canonical_fields'][5]['value'] == 1:
        # First request on this path establishes its separate two-minute window.
        window_rate = 120 / max(60, delta_ms // 1000)
    else:
        raise ValueError('Export the first product request after a page reload; its window anchor is required')
    return elapsed, rate, window_rate


def refresh_headers(item_id: str, seed_headers: Mapping[str, str]) -> dict[str, str]:
    """Generate fresh request fields, preserving verified seed device facts.

    No network or credential persistence occurs. A seed is more than cookies:
    it includes an accepted SAP payload and cached token for this exact SDK.
    Each call draws fresh entropy and aligns RI/mask/millisecond timestamps.
    """
    url = urlsplit(product_url(item_id))
    headers = _seed(seed_headers)
    timestamp_ms = time.time_ns() // 1_000_000
    elapsed, rate, window_rate = _request_clock(headers, timestamp_ms)
    seconds = timestamp_ms // 1000
    ri = encode_ri(seconds, secrets.token_bytes(22))
    mask = derive_request_key(seconds, secrets.token_bytes(4))
    signed = refresh_root(headers['x-sap-sec'], headers['x-sap-ri'], ri,
        new_mask=mask, new_random=secrets.token_bytes(8),
        raw_url=url.path + '?' + url.query, timestamp_ms=timestamp_ms,
        refresh_collection=_collection_refresher(), advance_request=True,
        request_rate=rate, elapsed_seconds=elapsed, root_request_rate=window_rate)
    return {**headers, **signed, 'af-ac-enc-dat': secrets.token_bytes(8).hex()}
