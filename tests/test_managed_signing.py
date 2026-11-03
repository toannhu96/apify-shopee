"""Offline equivalence between opt-in managed reservations and the standalone CLI."""
import copy
import json
from pathlib import Path

import pytest

from shopee_affiliate import managed_signing, sap_signer, session_signer


def headers():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    return {**fixture['before_headers'], 'af-ac-enc-sz-token': 'synthetic-token',
            'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}


def frozen(monkeypatch, timestamp):
    monkeypatch.setattr(sap_signer.time, 'time_ns', lambda: timestamp * 1_000_000)
    monkeypatch.setattr(sap_signer.secrets, 'token_bytes', lambda size: bytes(range(size)))


def test_each_reservation_matches_exact_cli_from_original_seed_after_response(monkeypatch):
    original = headers()
    state = managed_signing.initialize(original, mode='standalone-seed')
    baseline = copy.deepcopy(state)
    assert state['seed_headers'] == original
    assert state['last_headers'] is None
    assert (state['sequence'], state['response_count']) == (0, 0)
    for sequence, item in enumerate(('235', '24995347301'), 1):
        frozen(monkeypatch, 1800000000000 + 12_000 * sequence)
        expected = sap_signer.refresh_headers(item, original)
        previous = copy.deepcopy(state)
        fresh, reserved = managed_signing.reserve_request(item, json.loads(json.dumps(state)))
        assert fresh == expected
        assert reserved['last_headers'] == expected
        assert reserved['seed_headers'] == original
        assert (reserved['sequence'], reserved['response_count']) == (sequence, sequence - 1)
        assert state == previous
        state = managed_signing.record_response(reserved)
        assert state['response_count'] == sequence
        assert state['seed_headers'] == original and state['last_headers'] == expected
        assert reserved['response_count'] == sequence - 1
    assert baseline['seed_headers'] == original and baseline['sequence'] == 0


def test_unanswered_reservations_remain_durable_and_do_not_feed_seed(monkeypatch):
    frozen(monkeypatch, 1800000012000)
    state = managed_signing.initialize(headers(), mode='standalone-seed')
    _, reserved = managed_signing.reserve_request('235', state)
    fresh, next_state = managed_signing.reserve_request('235', reserved)
    assert fresh == sap_signer.refresh_headers('235', headers())
    assert (next_state['sequence'], next_state['response_count']) == (2, 0)
    assert (reserved['sequence'], state['sequence']) == (1, 0)


def test_nested_inputs_and_outputs_are_independent(monkeypatch):
    frozen(monkeypatch, 1800000012000)
    seed = headers()
    state = managed_signing.initialize(seed, mode='standalone-seed')
    seed['user-agent'] = 'changed original'
    fresh, next_state = managed_signing.reserve_request('235', state)
    fresh['user-agent'] = 'changed output'
    assert next_state['last_headers']['user-agent'] == headers()['user-agent']
    next_state['seed_headers']['user-agent'] = 'changed next state'
    assert state['seed_headers']['user-agent'] == headers()['user-agent']


def test_cumulative_import_and_operations_delegate_without_modifying_state(monkeypatch):
    initialized, completed, reserved = {'initial': 1}, {'completed': 1}, {'reserved': 1}
    calls = []
    monkeypatch.setattr(session_signer, 'initialize_session', lambda value: calls.append(('initialize', value)) or initialized)
    monkeypatch.setattr(session_signer, 'record_response', lambda value: calls.append(('response', value)) or completed)
    monkeypatch.setattr(session_signer, 'reserve_request', lambda item, value: calls.append(('reserve', item, value)) or ({}, reserved))
    seed = headers()
    assert managed_signing.initialize(seed) is completed
    assert managed_signing.initialize(seed, mode='cumulative') is completed
    assert managed_signing.reserve_request('235', initialized) == ({}, reserved)
    assert managed_signing.record_response(initialized) is completed
    assert calls == [('initialize', seed), ('response', initialized)] * 2 + [
        ('reserve', '235', initialized), ('response', initialized)]


@pytest.mark.parametrize('patch', [
    {'version': 2}, {'version': True}, {'mode': 'other'}, {'sequence': True},
    {'sequence': -1}, {'sequence': 2**63}, {'response_count': True}, {'response_count': -1},
    {'response_count': 1}, {'seed_headers': {}}, {'last_headers': {}}, {'unexpected': 'PRIVATE'},
    {'seed_headers': {**headers(), 'user-agent': 'PRIVATE' * 3000}},
])
@pytest.mark.parametrize('operation', ['reserve', 'response'])
def test_corrupt_state_rejected_without_echo(patch, operation, monkeypatch):
    frozen(monkeypatch, 1800000012000)
    state = managed_signing.initialize(headers(), mode='standalone-seed')
    broken = {**state, **patch}
    with pytest.raises(ValueError) as error:
        if operation == 'reserve':
            managed_signing.reserve_request('235', broken)
        else:
            managed_signing.record_response(broken)
    assert 'PRIVATE' not in str(error.value)


def test_response_requires_outstanding_reservation(monkeypatch):
    frozen(monkeypatch, 1800000012000)
    initial = managed_signing.initialize(headers(), mode='standalone-seed')
    with pytest.raises(ValueError):
        managed_signing.record_response(initial)
    _, reserved = managed_signing.reserve_request('235', initial)
    completed = managed_signing.record_response(reserved)
    with pytest.raises(ValueError):
        managed_signing.record_response(completed)


@pytest.mark.parametrize('mode', ['unknown', 'standalone_seed', None, True])
def test_unknown_initialization_mode_rejected(mode):
    with pytest.raises(ValueError):
        managed_signing.initialize(headers(), mode=mode)


def test_invalid_seed_and_item_rejected(monkeypatch):
    frozen(monkeypatch, 1800000012000)
    with pytest.raises(ValueError):
        managed_signing.initialize({'user-agent': 'PRIVATE'}, mode='standalone-seed')
    with pytest.raises(ValueError):
        managed_signing.initialize({**headers(), 'x-sz-sdk-version': 'bad'}, mode='standalone-seed')
    state = managed_signing.initialize(headers(), mode='standalone-seed')
    with pytest.raises(ValueError):
        managed_signing.reserve_request('https://PRIVATE.invalid', state)


def test_sequence_exhaustion_never_dispatches_signer(monkeypatch):
    frozen(monkeypatch, 1800000012000)
    initial = managed_signing.initialize(headers(), mode='standalone-seed')
    _, reserved = managed_signing.reserve_request('235', initial)
    state = {**reserved, 'sequence': 2**63 - 1}
    monkeypatch.setattr(sap_signer, 'refresh_headers', lambda *args: pytest.fail('Must not sign'))
    with pytest.raises(ValueError):
        managed_signing.reserve_request('235', state)


@pytest.mark.parametrize('operation', [managed_signing.reserve_request, managed_signing.record_response])
def test_non_mapping_state_rejected(operation):
    with pytest.raises(TypeError):
        operation('235', None) if operation is managed_signing.reserve_request else operation(None)


def test_serialized_header_size_bounded():
    seed = {**headers(), **{name: '"' * 16384 for name in
                           ('accept', 'accept-language', 'accept-encoding', 'referer')}}
    with pytest.raises(ValueError, match='Invalid managed signing seed'):
        managed_signing.initialize(seed, mode='standalone-seed')
