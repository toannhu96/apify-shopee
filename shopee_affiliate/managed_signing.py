"""Per-account signing modes with immutable, persist-before-send reservations.

Existing unmarked states keep cumulative browser request history. The explicit
standalone mode derives every request from the imported header seed exactly as
the standalone client does. Its durable counters track reservations and received
responses, but never become signer input. Returned headers are confidential.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from . import sap_signer, session_signer
from .signed_client import MAX_STATE_BYTES, _normalized_headers

STANDALONE_MODE = 'standalone_seed'
MAX_SEQUENCE = 2**63 - 1
STATE_KEYS = frozenset({'version', 'mode', 'seed_headers', 'last_headers', 'sequence', 'response_count'})


def _headers(value: Mapping) -> dict:
    headers = _normalized_headers(value)
    if len(json.dumps(headers, separators=(',', ':')).encode('utf-8')) > MAX_STATE_BYTES:
        raise ValueError('Invalid managed signing headers')
    return sap_signer._seed(headers)


def _standalone(state: Mapping) -> bool:
    if not isinstance(state, Mapping):
        raise TypeError('Invalid managed signing state')
    if 'mode' not in state:
        return False
    if state['mode'] != STANDALONE_MODE:
        raise ValueError('Invalid managed signing mode')
    return True


def _validated_state(state: Mapping) -> dict:
    if set(state) != STATE_KEYS or state['mode'] != STANDALONE_MODE:
        raise ValueError('Invalid managed signing state')
    if (type(state['version']) is not int or state['version'] != 1
            or type(state['sequence']) is not int or type(state['response_count']) is not int
            or not 0 <= state['response_count'] <= state['sequence'] <= MAX_SEQUENCE):
        raise ValueError('Invalid managed signing counters')
    seed = _headers(state['seed_headers'])
    if state['sequence'] == 0:
        if state['last_headers'] is not None:
            raise ValueError('Invalid managed signing reservation')
        last = None
    else:
        last = _headers(state['last_headers'])
    return {**state, 'seed_headers': seed, 'last_headers': last}


def initialize(headers: Mapping[str, str], mode: str = 'cumulative') -> dict:
    """Import an accepted request seed; cumulative imports include its response."""
    if mode == 'cumulative':
        return session_signer.record_response(session_signer.initialize_session(headers))
    if mode != 'standalone-seed':
        raise ValueError('Invalid managed signing mode')
    try:
        return {'version': 1, 'mode': STANDALONE_MODE, 'seed_headers': _headers(headers),
                'last_headers': None, 'sequence': 0, 'response_count': 0}
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError('Invalid managed signing seed') from None


def reserve_request(item_id: str | int, state: Mapping) -> tuple[dict[str, str], dict]:
    """Return fresh headers and a reservation to persist under the account lease.

    The caller must durably store this state before HTTP dispatch, even when the
    eventual response is lost. Never feed last_headers back into the seed.
    """
    if not _standalone(state):
        return session_signer.reserve_request(item_id, state)
    try:
        current = _validated_state(state)
        if current['sequence'] == MAX_SEQUENCE:
            raise ValueError('Managed signing sequence exhausted')
        fresh = sap_signer.refresh_headers(item_id, current['seed_headers'])
        next_state = {**current, 'last_headers': _headers(fresh), 'sequence': current['sequence'] + 1}
        return fresh, next_state
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError('Invalid managed signing reservation') from None


def record_response(state: Mapping) -> dict:
    """Count one received response under the same fenced lease, without signing.

    This is not idempotent; the repository must apply it only once per response.
    A timeout leaves the reserved sequence intact without incrementing responses.
    """
    if not _standalone(state):
        return session_signer.record_response(state)
    try:
        current = _validated_state(state)
        if current['response_count'] == current['sequence']:
            raise ValueError('No outstanding signing reservation')
        return {**current, 'response_count': current['response_count'] + 1}
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError('Invalid managed signing response') from None
