"""Original SDK known answers; fixtures contain synthetic identifiers only."""
import json
from dataclasses import FrozenInstanceError
from email.utils import formatdate
from pathlib import Path

import pytest

from shopee_affiliate.token_lifecycle import (
    TokenLifecycleState,
    ingest_report_response,
    ingest_response_text,
    safe_header_token,
    select_degraded_token,
)

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/token_lifecycle_golden.json').read_text())
DEVICE = FIXTURE['provenance']['device_id']
KEY = 'shopee_webUnique_ccd'


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda c: c['name'])
def test_original_sdk_golden(case):
    source, expected = case['input'], case['expected']
    state = TokenLifecycleState(device_id=DEVICE, step=source.get('initial_step', 0))
    if source.get('initial_token'):
        state = TokenLifecycleState(device_id=DEVICE, cookie_token=source['initial_token'],
            session_token=json.dumps(source['initial_token']), local_token=json.dumps(source['initial_token']))
    if source['mode'] == 'report':
        state = ingest_report_response(state, source['response'], now_ms=1800000000501)
    elif source['mode'] == 'raw':
        state = ingest_response_text(state, source['token'], now_ms=1800000000501)
    else:
        state = TokenLifecycleState(device_id=DEVICE, step=source['step'],
            current_token=source['memory'], cookie_token=source.get('cookies', {}).get(KEY),
            session_token=source.get('session', {}).get(KEY),
            local_token=source.get('local', {}).get(KEY))
    assert state.step == expected['step']
    assert select_degraded_token(state) == expected['selected']
    assert state.session_token == expected['sessionToken']
    assert state.local_token == expected['localToken']
    assert state.session_ds == expected['sessionDs']
    assert len(state.cookie_writes) == len(expected['cookieWrites'])
    for actual, original in zip(state.cookie_writes, expected['cookieWrites']):
        assert actual.name == original['name']
        assert actual.value == original['value']
        assert actual.path == '/'
        assert actual.domain is None and not actual.secure and actual.same_site is None
        assert ['expires=' + formatdate(actual.expires_at, usegmt=True), 'path=/'] == original['attributes']


def test_state_is_immutable_and_sensitive_values_are_not_in_repr():
    state = TokenLifecycleState(device_id='PRIVATE_DEVICE', current_token='PRIVATE_TOKEN')
    updated = ingest_response_text(state, 'PRIVATE_TOKEN_' + 'S' * 110, now_ms=0)
    assert state.session_token is None
    with pytest.raises(FrozenInstanceError):
        updated.step = 99
    assert 'PRIVATE' not in repr(state)
    assert 'PRIVATE' not in repr(updated)
    assert 'PRIVATE' not in repr(updated.cookie_writes)


@pytest.mark.parametrize('token', ['A\rB', 'A\nB', 'A\x00B', 'A\x7fB', 'AéB'])
def test_sdk_selection_and_safe_http_gate_are_separate(token):
    state = TokenLifecycleState(device_id=DEVICE, step=2, current_token=token)
    assert select_degraded_token(state) == token
    with pytest.raises(ValueError, match='HTTP token'):
        safe_header_token(state)


def test_no_local_age_based_token_renewal():
    state = ingest_report_response(TokenLifecycleState(device_id=DEVICE),
        {'code': 0, 'data': {'riskToken': 'R' * 123}}, now_ms=0)
    assert safe_header_token(state) == 'R' * 123
    assert state.cookie_writes[0].expires_at == 365 * 86400


@pytest.mark.parametrize('session, expected', [
    ('null', 'LOCAL'), ('false', 'LOCAL'), ('0', 'LOCAL'), ('""', 'LOCAL'),
    ('broken', 'LOCAL'), ('NaN', 'LOCAL'), ('[]', ''), ('{}', ''), ('1', ''),
])
def test_cached_selection_uses_javascript_truthiness(session, expected):
    state = TokenLifecycleState(device_id=DEVICE, session_token=session, local_token='"LOCAL"')
    assert select_degraded_token(state) == expected


def test_cookie_precedence_and_memory_step():
    state = TokenLifecycleState(device_id=DEVICE, step=2, current_token='',
        cookie_token='COOKIE', session_token='"SESSION"')
    assert select_degraded_token(state) == ''
    fallback = TokenLifecycleState(device_id=DEVICE, cookie_token='', session_token='"SESSION"')
    assert select_degraded_token(fallback) == 'SESSION'


@pytest.mark.parametrize('kwargs', [
    {'step': True}, {'step': -1}, {'current_token': 'S' * 8193},
    {'session_token': 123}, {'device_id': None}, {'device_id': ''},
    {'current_token': '\ud800'}, {'cookie_writes': []},
])
def test_state_rejects_invalid_values_without_echo(kwargs):
    with pytest.raises(ValueError) as error:
        TokenLifecycleState(**({'device_id': DEVICE} | kwargs))
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('now', [-1, True, 1.1, 10**100, None])
def test_ingestion_validates_clock(now):
    with pytest.raises(ValueError, match='clock'):
        ingest_response_text(TokenLifecycleState(device_id=DEVICE), 'S' * 123, now_ms=now)


@pytest.mark.parametrize('response', [None, [], {'code': 0, 'data': {'riskToken': []}},
    {'code': 0, 'data': {'riskToken': 'R' * 8193}}])
def test_report_boundary_rejects_malformed_values(response):
    with pytest.raises(ValueError):
        ingest_report_response(TokenLifecycleState(device_id=DEVICE), response, now_ms=0)


def test_failed_response_preserves_cached_success():
    first = ingest_report_response(TokenLifecycleState(device_id=DEVICE),
        {'code': 0, 'data': {'riskToken': 'R' * 123}}, now_ms=0)
    failed = ingest_report_response(first, {'code': 1}, now_ms=1000)
    assert failed.step == 7
    assert failed.fallback_ccd_missing
    assert not first.fallback_ccd_missing
    assert select_degraded_token(failed) == 'R' * 123
    assert failed.cookie_writes == ()
    assert first.step == 2


def test_json_escaped_lone_surrogate_is_selected_but_not_sent():
    state = TokenLifecycleState(device_id=DEVICE, session_token='"\\ud800"')
    assert select_degraded_token(state) == '\ud800'
    with pytest.raises(ValueError, match='HTTP token'):
        safe_header_token(state)


@pytest.mark.parametrize('code, accepted', [(False, False), (0.0, True)])
def test_javascript_numeric_zero(code, accepted):
    state = ingest_report_response(TokenLifecycleState(device_id=DEVICE),
        {'code': code, 'data': {'riskToken': 'R' * 123}}, now_ms=0)
    assert (state.step == 2) is accepted


def test_utf16_length_gate_counts_astral_characters_twice():
    state = TokenLifecycleState(device_id=DEVICE)
    assert ingest_response_text(state, '😀' * 50, now_ms=0).session_token is None
    updated = ingest_response_text(state, '😀' * 51, now_ms=0)
    assert select_degraded_token(updated) == '😀' * 51
    oversized = TokenLifecycleState(device_id=DEVICE, step=2, current_token='😀' * 151)
    assert select_degraded_token(oversized) == ''


def test_raw_setter_preserves_missing_fallback_marker():
    failed = ingest_report_response(TokenLifecycleState(device_id=DEVICE), {'code': 1}, now_ms=0)
    updated = ingest_response_text(failed, 'R' * 123, now_ms=1000)
    assert updated.fallback_ccd_missing
    assert updated.step == 7
    assert select_degraded_token(updated) == 'R' * 123
