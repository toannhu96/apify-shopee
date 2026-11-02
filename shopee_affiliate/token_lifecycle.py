"""Offline reconstruction of the observed token SDK cache/response lifecycle.

This module cannot mint a server risk token, generate a device report, or prove
server acceptance. Callers must supply legitimate issuer responses and the
matching observed device ID. Cookie expiry is persistence metadata, not a server
validity guarantee. Independent original-SDK vectors live in token_lifecycle_golden.json.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

CCD_KEY = 'shopee_webUnique_ccd'
MAX_VALUE = 8192
COOKIE_LIFETIME_SECONDS = 365 * 86400


def _string(value, *, optional=False, maximum=MAX_VALUE):
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise ValueError('Invalid token state value')  # noqa: TRY004
    try:
        units = len(value.encode('utf-16-le')) // 2
    except UnicodeError:
        raise ValueError('Invalid token state value') from None
    if units > maximum:
        raise ValueError('Invalid token state value')


def _js_length(value: str) -> int:
    return len(value.encode('utf-16-le', errors='surrogatepass')) // 2


@dataclass(frozen=True)
class CookieWrite:
    """A decoded cookie update observed in the SDK; no browser mutation occurs."""
    name: str
    value: str = field(repr=False)
    expires_at: int
    path: str = '/'
    domain: None = None
    secure: bool = False
    same_site: None = None

    def __post_init__(self):
        _string(self.value)
        if (self.name not in (CCD_KEY, 'ds') or type(self.expires_at) is not int
                or not 0 <= self.expires_at <= 253402300799 or self.path != '/'
                or self.domain is not None or self.secure is not False
                or self.same_site is not None):
            raise ValueError('Invalid token cookie metadata')


@dataclass(frozen=True)
class TokenLifecycleState:
    """Immutable observed state; storage fields hold JSON-encoded strings.

    Only SDK step 2 selects current_token directly. Other steps select the
    decoded cookie, then session storage, then local storage. cookie_writes
    contains updates from the latest ingestion, not a pending retry queue.
    """
    device_id: str = field(repr=False)
    step: int = 0
    current_token: str = field(default='', repr=False)
    cookie_token: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    local_token: str | None = field(default=None, repr=False)
    session_ds: str | None = field(default=None, repr=False)
    cookie_writes: tuple[CookieWrite, ...] = ()
    fallback_ccd_missing: bool = False

    def __post_init__(self):
        if type(self.fallback_ccd_missing) is not bool:
            raise ValueError('Invalid token fallback marker')
        _string(self.device_id, maximum=256)
        if not self.device_id or any(ord(c) < 32 or ord(c) > 126 for c in self.device_id):
            raise ValueError('Invalid device identifier')
        if type(self.step) is not int or not 0 <= self.step <= 10:
            raise ValueError('Invalid token lifecycle step')
        _string(self.current_token)
        for value in (self.cookie_token, self.session_token, self.local_token, self.session_ds):
            _string(value, optional=True)
        if (not isinstance(self.cookie_writes, tuple) or len(self.cookie_writes) > 2
                or any(not isinstance(c, CookieWrite) for c in self.cookie_writes)):
            raise ValueError('Invalid token cookie updates')


def _validate(state, now_ms):
    if not isinstance(state, TokenLifecycleState):
        raise ValueError('Invalid token lifecycle state')  # noqa: TRY004
    if type(now_ms) is not int or not 0 <= now_ms <= 253370764799999:
        raise ValueError('Invalid token clock')


def _persist(state: TokenLifecycleState, token: str, now_ms: int, step: int):
    # Compatibility digest, not a password hash or a new token signature.
    ds = hashlib.sha1((token + '&' + state.device_id).encode('utf-8')).hexdigest()[:32]
    expires = now_ms // 1000 + COOKIE_LIFETIME_SECONDS
    return replace(state, step=step, current_token=token, cookie_token=token,
        fallback_ccd_missing=state.fallback_ccd_missing if step != 2 else False,
        session_token=json.dumps(token, ensure_ascii=False, separators=(',', ':')),
        local_token=json.dumps(token, ensure_ascii=False, separators=(',', ':')),
        session_ds=json.dumps(ds), cookie_writes=(
            CookieWrite(CCD_KEY, token, expires), CookieWrite('ds', ds, expires)))


def ingest_response_text(state: TokenLifecycleState, token: str, *, now_ms: int):
    """Match setResponseText's distinct 100 < UTF-16 length < 300 gate.

    This entry point preserves the SDK step. It does not process JSON report
    envelopes. device_id must be the already initialized SDK UUID, not an
    uninitialized storage guess. This does not certify the supplied string came from an issuer.
    """
    _validate(state, now_ms)
    _string(token)
    if 100 < _js_length(token) < 300:
        return _persist(state, token, now_ms, state.step)
    return replace(state, cookie_writes=())


def ingest_report_response(state: TokenLifecycleState, response: Mapping, *, now_ms: int):
    """Ingest a bounded issuer JSON response; strict numeric code 0 is success.

    Report ingestion accepts a nonempty riskToken without setResponseText's
    length gate. Selection separately rejects tokens longer than 300 units.
    Malformed non-string tokens are rejected at this Python API boundary.
    Failed reports mark fallback_ccd_missing: only cached-token behavior is
    reconstructed, not the SDK's full fallback device-check payload.
    """
    _validate(state, now_ms)
    if not isinstance(response, Mapping):
        raise ValueError('Invalid token report response')  # noqa: TRY004
    data = response.get('data')
    if data is not None and not isinstance(data, Mapping):
        raise ValueError('Invalid token report data')
    token = data.get('riskToken') if data is not None else None
    if token is not None:
        _string(token)
    code = response.get('code')
    if type(code) in (int, float) and code == 0 and token:
        return _persist(state, token, now_ms, 2)
    # The real SDK builds a large fallback CCD here. It is intentionally not
    # fabricated: step 7 selects persisted fallback state, never current_token.
    return replace(state, step=7, cookie_writes=(), fallback_ccd_missing=True)


def _decoded(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw, parse_int=float,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        return None


def _truthy(value):
    return not (value is None or value is False
        or isinstance(value, (int, float)) and value == 0
        or isinstance(value, str) and value == '')


def select_degraded_token(state: TokenLifecycleState) -> str:
    """Select SDK state without reading the clock or claiming token freshness.

    This preserves SDK string/control-character semantics; use safe_header_token
    at an HTTP boundary. Storage expiry is controlled by its owner, not this SDK
    getter. Step 2 does not fall back when its in-memory token is empty/oversized.
    """
    if not isinstance(state, TokenLifecycleState):
        raise ValueError('Invalid token lifecycle state')  # noqa: TRY004
    if state.step == 2:
        selected = state.current_token
    elif state.cookie_token:
        selected = state.cookie_token
    else:
        session = _decoded(state.session_token)
        selected = session if _truthy(session) else _decoded(state.local_token)
    return selected if isinstance(selected, str) and _js_length(selected) <= 300 else ''


def safe_header_token(state: TokenLifecycleState) -> str:
    """Require a nonempty printable ASCII token before constructing HTTP headers."""
    token = select_degraded_token(state)
    if not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise ValueError('Invalid HTTP token')
    return token
