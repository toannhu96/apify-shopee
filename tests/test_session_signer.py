"""Offline cumulative signing tests, using public synthetic SDK fixtures."""
import copy
import json
from pathlib import Path

import pytest

from shopee_affiliate import session_signer as signer
from shopee_affiliate.sap_codec import decode_ri, decode_sec, encode_sec
from shopee_affiliate.sap_collections import collection_request_state
from shopee_affiliate.sap_crypto import decode_collection
from shopee_affiliate.sap_payload import build_payload
from shopee_affiliate.sap_request import derive_slot_rotation, xor_repeating
from shopee_affiliate.sap_state import decode_payload


def fixture(name='sap_root_refresh_golden.json'):
    return json.loads((Path(__file__).parent / 'fixtures' / name).read_text())


def headers(value=None):
    return {**(value or fixture()['before_headers']), 'af-ac-enc-sz-token': 'synthetic-token',
            'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}


def decoded(value):
    state = decode_payload(decode_sec(value['x-sap-sec'], value['x-sap-ri']).payload,
                           value['x-sap-ri'])
    vectors = [bytes.fromhex(v['hex']) for v in state['vectors']]
    request = collection_request_state(
        decode_collection(vectors[12], vectors[13], 'custom', vectors[1], vectors[14]), vectors[1])
    return state, request


def direct_sdk_refresh(seed, reference):
    """Independent standalone fixtures omit the browser's XHR send wrapper."""
    target = reference['target_headers']
    target_state, request = decoded(target)
    vectors = [bytes.fromhex(v['hex']) for v in target_state['vectors']]
    return signer.refresh_root(seed['x-sap-sec'], seed['x-sap-ri'], target['x-sap-ri'],
        new_mask=vectors[1], new_random=vectors[14], raw_url=reference['raw_url'],
        timestamp_ms=reference['timestamp_ms'], refresh_collection=signer.refresh_collection,
        detector_padding=vectors[2], advance_request=True,
        request_rate=request['request_rate'], elapsed_seconds=request['elapsed_seconds'],
        root_request_rate=target_state['canonical_fields'][5]['value'])


@pytest.mark.parametrize('name', ['sap_collection_sequence_time_golden.json',
                                 'sap_root_window_aged_golden.json'])
def test_lowlevel_direct_sequence_matches_original_sdk_fixture(name):
    reference = fixture(name)
    fresh = direct_sdk_refresh(reference['before_headers'], reference)
    assert fresh == reference['target_headers']


def test_many_requests_accumulate_and_restore_across_windows():
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    snapshots = []
    for timestamp in (1800000012000, 1800000024000, 1800000130000, 1800000136000):
        fresh, state = signer.reserve_request('26609048170', json.loads(json.dumps(state)), now_ms=timestamp)
        raw, request = decoded(fresh)
        snapshots.append((request['path_count'], request['elapsed_seconds'], raw['canonical_fields'][5]['value']))
    assert snapshots == [(2, 12, 2), (3, 24, 3), (4, 130, 1), (5, 136, 2)]
    assert state['window_count'] == 2
    assert state['window_started_ms'] == 1800000130000


def test_reservation_is_not_reverted_when_request_has_unknown_outcome():
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    _, reserved = signer.reserve_request('1', state, now_ms=1800000006000)
    # Persist this reservation before opening a socket, even if the socket later times out.
    fresh, next_state = signer.reserve_request('1', reserved, now_ms=1800000012000)
    assert decoded(fresh)[1]['path_count'] == 3
    assert next_state['path_count'] == 3


def test_nonfirst_recent_seed_rejected_but_aged_seed_can_reset():
    target = headers(fixture('sap_collection_sequence_time_golden.json')['target_headers'])
    with pytest.raises(ValueError, match='first product request'):
        signer.initialize_session(target, now_ms=1800000013000)
    state = signer.initialize_session(target, now_ms=1800000200000)
    fresh, updated = signer.reserve_request('1', state, now_ms=1800000200000)
    assert decoded(fresh)[0]['canonical_fields'][5]['value'] == 1
    assert updated['window_count'] == 1


@pytest.mark.parametrize('timestamp', [True, -1, 1.1, '123', None, 2**32 * 1000])
def test_invalid_explicit_clock(timestamp, monkeypatch):
    if timestamp is None:
        monkeypatch.setattr(signer.time, 'time_ns', lambda: -1)
    with pytest.raises(ValueError):
        signer.initialize_session(headers(), now_ms=timestamp)


def test_clock_rollback_and_bad_item_rejected():
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    with pytest.raises(ValueError):
        signer.reserve_request('1', state, now_ms=1799999999999)
    with pytest.raises(ValueError):
        signer.reserve_request('http://evil.test', state, now_ms=1800000000000)


@pytest.mark.parametrize('patch', [
    {'version': 2}, {'path_count': True}, {'path_count': 100}, {'last_ms': 1800000000001},
    {'clock_anchor_ms': -1}, {'window_count': -1}, {'window_count': 2},
    {'window_started_ms': 1800000000001}, {'headers': {}}, {'unknown': 'secret'},
])
def test_corrupt_persisted_state_rejected(patch):
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    with pytest.raises(ValueError):
        signer.reserve_request('1', {**state, **patch}, now_ms=1800000010000)


def test_input_seed_copied_and_bad_version_rejected():
    original = headers()
    state = signer.initialize_session(original, now_ms=1800000000000)
    original['user-agent'] = 'changed'
    assert state['headers']['user-agent'] != 'changed'
    with pytest.raises(ValueError):
        signer.initialize_session({**headers(), 'x-sz-sdk-version': 'other'}, now_ms=1800000000000)


def rewritten_vector(index, plaintext):
    original = headers()
    raw, _ = decoded(original)
    mask = bytes.fromhex(raw['vectors'][1]['hex'])
    vectors = [{**value, 'hex': xor_repeating(plaintext, mask).hex()} if i == index else value
               for i, value in enumerate(raw['vectors'])]
    fields = [{'index': i, **value} for i, value in raw['canonical_fields'].items()]
    rebuilt = build_payload(vectors, fields, rotation=derive_slot_rotation(original['x-sap-ri']),
                            renderer_placeholder=raw['renderer_placeholder'])
    return {**original, 'x-sap-sec': encode_sec(rebuilt, decode_ri(original['x-sap-ri']).xor_key)}


def test_origin_and_disagreeing_embedded_clock_rejected():
    foreign = rewritten_vector(7, b'other.example')
    with pytest.raises(ValueError, match='origin'):
        signer.initialize_session(foreign, now_ms=1800000000000)
    bad_clock = rewritten_vector(4, (1799999999000).to_bytes(8, 'little'))
    with pytest.raises(ValueError, match='timestamp'):
        signer.initialize_session(bad_clock, now_ms=1800000000000)


def test_exact_window_boundary_and_next_millisecond_reset():
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    _, state = signer.reserve_request('1', state, now_ms=1800000060000)
    fresh, state = signer.reserve_request('1', state, now_ms=1800000120000)
    assert decoded(fresh)[0]['canonical_fields'][5]['value'] == 1.5
    fresh, state = signer.reserve_request('1', state, now_ms=1800000120001)
    assert decoded(fresh)[0]['canonical_fields'][5]['value'] == 1
    assert state['window_count'] == 1


def test_initial_clock_cannot_precede_seed_and_fake_reset_rejected():
    with pytest.raises(ValueError):
        signer.initialize_session(headers(), now_ms=1799999999000)
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    with pytest.raises(ValueError, match='aged seed'):
        signer.reserve_request('1', {**state, 'window_count': 0}, now_ms=1800000000000)


def test_restored_window_rate_cannot_disagree_with_headers():
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    _, state = signer.reserve_request('1', state, now_ms=1800000010000)
    with pytest.raises(ValueError, match='rate disagrees'):
        signer.reserve_request('1', {**state, 'window_count': 1}, now_ms=1800000020000)


def test_internal_counters_must_not_wrap(monkeypatch):
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    original_metadata = signer._metadata
    def exhausted(value):
        last, request, rate, counters = original_metadata(value)
        return last, request, rate, [*counters, 2**32 - 1]
    monkeypatch.setattr(signer, '_metadata', exhausted)
    with pytest.raises(ValueError, match='counters'):
        signer.reserve_request('1', state, now_ms=1800000010000)


def test_response_completion_matches_original_sdk_xhr_handler():
    reference = fixture('sap_response_sequence_golden.json')
    state = signer.initialize_session(headers(reference['before_headers']), now_ms=1800000000000)
    before = copy.deepcopy(state)
    completed = signer.record_response(state)
    assert state == before
    assert {k: v for k, v in completed.items() if k != 'headers'} == {
        k: v for k, v in state.items() if k != 'headers'}
    fresh = direct_sdk_refresh(completed['headers'], reference)
    assert fresh == reference['target_headers']


def test_response_completion_preserves_tokens_and_detects_invalid_state():
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    completed = signer.record_response(state)
    assert completed['headers']['af-ac-enc-sz-token'] == state['headers']['af-ac-enc-sz-token']
    assert completed['headers']['x-sap-ri'] == state['headers']['x-sap-ri']
    assert decoded(completed['headers'])[1] == decoded(state['headers'])[1]
    before_raw, after_raw = decoded(state['headers'])[0], decoded(completed['headers'])[0]
    assert before_raw['canonical_fields'] == after_raw['canonical_fields']
    assert [i for i, (before, after) in enumerate(zip(before_raw['vectors'], after_raw['vectors']))
            if before != after] == [12, 13]
    with pytest.raises(ValueError):
        signer.record_response({**state, 'path_count': 99})


def test_response_completion_accepts_aged_import_with_reset_window():
    seed = headers(fixture('sap_collection_sequence_time_golden.json')['target_headers'])
    imported = signer.initialize_session(seed, now_ms=1800000200000)
    before = copy.deepcopy(imported)
    completed = signer.record_response(imported)
    assert imported == before
    assert completed['last_ms'] == 1800000012345
    assert completed['window_started_ms'] == 1800000200000
    assert completed['window_count'] == 0
    fresh, reserved = signer.reserve_request('1', completed, now_ms=1800000200000)
    assert decoded(fresh)[0]['canonical_fields'][5]['value'] == 1
    assert reserved['window_count'] == 1


@pytest.mark.parametrize('window_start', [True, '1800000200000', -1, 2**32 * 1000])
def test_response_completion_rejects_invalid_window_clock(window_start):
    state = signer.initialize_session(headers(), now_ms=1800000000000)
    with pytest.raises(ValueError):
        signer.record_response({**state, 'window_started_ms': window_start})


def test_managed_requests_match_five_original_sdk_xhr_lifecycles(monkeypatch):
    reference = fixture('sap_xhr_lifecycle_golden.json')['requests']
    state = signer.initialize_session(headers(reference[0]['headers']),
                                      now_ms=reference[0]['timestamp_ms'])
    for target in reference[1:]:
        state = signer.record_response(json.loads(json.dumps(state)))
        target_headers = target['headers']
        target_state, _ = decoded(target_headers)
        vectors = [bytes.fromhex(v['hex']) for v in target_state['vectors']]
        entropy = iter((decode_ri(target_headers['x-sap-ri']).raw[4:], vectors[1][1::2],
                        vectors[14], vectors[2], bytes(8)))
        monkeypatch.setattr(signer.secrets, 'token_bytes', lambda count, entropy=entropy: next(entropy))
        fresh, state = signer.reserve_request('26609048170', state, now_ms=target['timestamp_ms'])
        assert {k: fresh[k] for k in target_headers} == target_headers
