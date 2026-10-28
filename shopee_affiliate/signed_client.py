"""Experimental browser-free product client using the pure-Python SAP signer.

Supply a cookie-free header seed in --state-file and login cookies separately.
The signer derives fresh request fields from that seed; this client never starts
Chrome or Node, invokes vendor JavaScript, retries, or falls back to old headers.
A usable seed and live server acceptance are separate from local algorithm tests.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import json
import math
from pathlib import Path
import sys

from . import direct, http_transport

MAX_STATE_BYTES = 128 * 1024
MAX_COOKIE_INPUT_BYTES = 1024 * 1024
REQUIRED_SEED_HEADERS = frozenset({'x-sap-ri', 'x-sap-sec', 'af-ac-enc-sz-token',
                                  'user-agent', 'x-sz-sdk-version'})
Signer = Callable[[str, Mapping[str, str]], Mapping[str, str]]


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate JSON field')
        result[name] = value
    return result


def _constant(_value):
    raise ValueError('Invalid JSON constant')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Invalid JSON number')
    return number


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    # Validate the same wire restrictions as the transport, without a request.
    http_transport._request_headers(headers, 'validation=1')
    result = {name.lower(): value for name, value in headers.items()}
    if not REQUIRED_SEED_HEADERS <= result.keys() or any(not result[k] for k in REQUIRED_SEED_HEADERS):
        raise ValueError('Required signing headers are missing')
    return result


def load_state(path: str | Path) -> dict[str, str]:
    """Read bounded strict JSON shaped exactly as {"headers": {...}}.

    Cookie fields, unknown root fields, duplicate/case-colliding header names,
    pseudo headers and unsupported request headers are rejected.
    """
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('State size limit')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_finite_float)
        if not isinstance(value, dict) or set(value) != {'headers'}:
            raise ValueError('Invalid state shape')
        return _normalized_headers(value['headers'])
    except (OSError, ValueError, TypeError, RecursionError):
        raise direct.DirectError('Signing state is invalid or unreadable') from None


def _cookie_header(source: str) -> str:
    try:
        if not isinstance(source, str) or len(source) > MAX_COOKIE_INPUT_BYTES:
            raise ValueError('Cookie input size limit')
        if len(source.encode('utf-8')) > MAX_COOKIE_INPUT_BYTES:
            raise ValueError('Cookie input size limit')
        if source.lstrip().startswith(('[', '{')):
            json.loads(source, object_pairs_hook=_pairs, parse_constant=_constant,
                       parse_float=_finite_float)
        cookie = direct.cookie_header(source)
        http_transport._request_headers({}, cookie)
        return cookie
    except (ValueError, TypeError, RecursionError):
        raise direct.DirectError('Cookie input is invalid') from None


def _default_signer() -> Signer:
    try:
        from .sap_signer import refresh_headers
        return refresh_headers
    except ImportError:
        raise direct.DirectError('The pure-Python signer is unavailable') from None


def fetch_product(item_id: str | int, *, seed_headers: Mapping[str, str],
                  cookie_source: str, signer: Signer | None = None) -> dict:
    """Sign once, GET once, and validate the requested item's product payload."""
    try:
        identifier = direct._item_id(item_id)
        seed = _normalized_headers(seed_headers)
    except (ValueError, TypeError):
        raise direct.DirectError('Product ID or signing seed headers are invalid') from None
    cookie = _cookie_header(cookie_source)
    signer = _default_signer() if signer is None else signer
    try:
        generated = _normalized_headers(signer(identifier, seed))
    except Exception:
        raise direct.DirectError('Could not generate fresh signing headers') from None
    try:
        response = http_transport.request_product(identifier, headers=generated, cookie=cookie)
    except http_transport.TransportError as error:
        raise direct.DirectError(str(error)) from None
    except ValueError:
        raise direct.DirectError('Generated request inputs are invalid') from None
    if response.status in (401, 403, 429) or 300 <= response.status < 400:
        raise direct.DirectBlockedError(f'Request blocked or redirected (HTTP {response.status})')
    if response.status != 200:
        raise direct.DirectError(f'Unexpected HTTP status {response.status}')
    return direct._validate(response.body, identifier)


def _read_cookie(path: Path | None) -> str:
    try:
        if path is None:
            raw = sys.stdin.buffer.read(MAX_COOKIE_INPUT_BYTES + 1)
        else:
            with path.open('rb') as stream:
                raw = stream.read(MAX_COOKIE_INPUT_BYTES + 1)
        if len(raw) > MAX_COOKIE_INPUT_BYTES:
            raise ValueError('Cookie input size limit')
        return raw.decode('utf-8')
    except (OSError, ValueError):
        raise direct.DirectError('Cookie input is invalid or unreadable') from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('item_id', help='One positive decimal product ID')
    parser.add_argument('--state-file', type=Path, required=True,
                        help='Local JSON containing only a headers object; no cookies')
    cookies = parser.add_mutually_exclusive_group(required=True)
    cookies.add_argument('--cookie-file', type=Path)
    cookies.add_argument('--cookie-stdin', action='store_true')
    args = parser.parse_args(argv)
    try:
        state = load_state(args.state_file)
        cookie_source = _read_cookie(args.cookie_file)
        result = fetch_product(args.item_id, seed_headers=state, cookie_source=cookie_source)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except direct.DirectBlockedError as error:
        print(str(error), file=sys.stderr)
        return 2
    except direct.DirectError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print('Could not write product output', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
