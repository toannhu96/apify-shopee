import json
from pathlib import Path

import pytest

from shopee_affiliate import sap_signer
from shopee_affiliate.sap_codec import decode_ri
from shopee_affiliate.sap_request import recover_key_timestamp


def seed():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    return {**fixture['before_headers'], 'af-ac-enc-sz-token': 'synthetic-token',
            'user-agent': 'Synthetic SDK fixture', 'x-sz-sdk-version': '1.12.21'}


def test_wrapper_aligns_time_entropy_url_and_preserves_input(monkeypatch):
    original = seed()
    seen = {}
    def refresh(*args, **kwargs):
        seen.update(kwargs)
        seen['ri'] = args[2]
        return {'x-sap-ri': args[2], 'x-sap-sec': 'synthetic-new-header'}
    monkeypatch.setattr(sap_signer, 'refresh_root', refresh)
    monkeypatch.setattr(sap_signer, '_collection_refresher', lambda: object())
    monkeypatch.setattr(sap_signer.time, 'time_ns', lambda: 1800000012345000000)
    monkeypatch.setattr(sap_signer.secrets, 'token_bytes', lambda n: bytes(range(n)))
    result = sap_signer.refresh_headers('26609048170', original)
    assert result['x-sap-sec'] == 'synthetic-new-header'
    assert result['af-ac-enc-dat'] == '0001020304050607'
    assert result['af-ac-enc-sz-token'] == original['af-ac-enc-sz-token']
    assert seen['timestamp_ms'] == 1800000012345
    assert decode_ri(seen['ri']).timestamp_seconds == recover_key_timestamp(seen['new_mask']) == 1800000012
    assert seen['raw_url'] == '/api/v3/offer/product?item_id=26609048170'
    assert seen['advance_request'] is True
    assert seen['elapsed_seconds'] == 12
    assert seen['request_rate'] == 2
    assert seen['root_request_rate'] == 2
    assert original == seed()


@pytest.mark.parametrize('change', [
    {'Cookie': 'secret'}, {'x-other': 'value'}, {'user-agent': 'bad\r\nheader'},
    {'x-sap-ri': ''}, {'af-ac-enc-sz-token': ''}, {'X-SAP-RI': 'duplicate'},
    {'x-sz-sdk-version': 'unsupported'},
])
def test_invalid_seed_does_not_reach_refresh(monkeypatch, change):
    monkeypatch.setattr(sap_signer, 'refresh_root', lambda *a, **k: pytest.fail('invalid seed reached refresh'))
    with pytest.raises(ValueError):
        sap_signer.refresh_headers('26609048170', {**seed(), **change})


@pytest.mark.parametrize('item_id', ['0', '../other', True, 'https://example.com/'])
def test_invalid_product(item_id):
    with pytest.raises(ValueError):
        sap_signer.refresh_headers(item_id, seed())


def test_aged_seed_resets_the_two_minute_path_window():
    elapsed, rate, window_rate = sap_signer._request_clock(seed(), 1800000200000)
    assert (elapsed, rate, window_rate) == (200, 0.6, 1)


def test_one_minute_old_first_request_window():
    elapsed, rate, window_rate = sap_signer._request_clock(seed(), 1800000070000)
    assert (elapsed, rate, window_rate) == (70, 120 / 70, 120 / 70)


def test_complete_wrapper_matches_consecutive_original_sdk_request(monkeypatch):
    from shopee_affiliate.sap_codec import decode_sec
    from shopee_affiliate.sap_state import decode_payload
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_collection_sequence_time_golden.json').read_text())
    target = fixture['target_headers']
    state = decode_payload(decode_sec(target['x-sap-sec'], target['x-sap-ri']).payload,
                           target['x-sap-ri'])
    vectors = [bytes.fromhex(value['hex']) for value in state['vectors']]
    entropy = iter((decode_ri(target['x-sap-ri']).raw[4:], vectors[1][1::2],
                    vectors[14], vectors[2], bytes(8)))
    def random_bytes(count):
        value = next(entropy)
        assert len(value) == count
        return value
    monkeypatch.setattr(sap_signer.time, 'time_ns', lambda: fixture['timestamp_ms'] * 1000000)
    monkeypatch.setattr(sap_signer.secrets, 'token_bytes', random_bytes)
    original = {**seed(), **fixture['before_headers']}
    result = sap_signer.refresh_headers(fixture['raw_url'].split('item_id=')[1], original)
    assert {key: result[key] for key in target} == target
