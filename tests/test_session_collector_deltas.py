"""Original SDK resource/event deltas must not repeat their imported seed value."""
import json
from pathlib import Path

import pytest

from shopee_affiliate.sap_codec import decode_sec
from shopee_affiliate.sap_collections import (
    collection_request_state,
    parse_collection,
    refresh_collection,
)
from shopee_affiliate.sap_crypto import decode_collection
from shopee_affiliate.sap_refresh import refresh_root
from shopee_affiliate.sap_request import xor_repeating
from shopee_affiliate.sap_state import decode_payload

GOLDEN = json.loads((Path(__file__).parent / 'fixtures/sap_collector_deltas_golden.json').read_text())


def decoded(headers):
    state = decode_payload(decode_sec(headers['x-sap-sec'], headers['x-sap-ri']).payload,
                           headers['x-sap-ri'])
    vectors = [bytes.fromhex(value['hex']) for value in state['vectors']]
    return state, vectors


def refresh(index, **options):
    before = GOLDEN['samples'][index - 1]['headers']
    target = GOLDEN['samples'][index]['headers']
    raw, vectors = decoded(target)
    custom = decode_collection(vectors[12], vectors[13], 'custom', vectors[1], vectors[14])
    request = collection_request_state(custom, vectors[1])
    parameters = {
        'new_mask': vectors[1], 'new_random': vectors[14],
        'raw_url': '/api/v3/offer/product?item_id=52253929694',
        'timestamp_ms': int.from_bytes(xor_repeating(vectors[4], vectors[1]), 'little'),
        'refresh_collection': refresh_collection, 'detector_padding': vectors[2],
        'advance_request': True, 'request_rate': request['request_rate'],
        'elapsed_seconds': request['elapsed_seconds'],
        'root_request_rate': raw['canonical_fields'][5]['value']}
    return refresh_root(before['x-sap-sec'], before['x-sap-ri'], target['x-sap-ri'],
                        **{**parameters, **options})


@pytest.mark.parametrize('index', [1, 2, 3, 4])
def test_complete_signature_matches_original_sdk_delta_sequence(index):
    calls = GOLDEN['samples'][index]['calls']
    actual = refresh(index, resource_entry_delta=calls[0]['delta'],
                     changed_event_types=calls[1]['delta'])
    assert actual == GOLDEN['samples'][index]['headers']


def test_omitting_delta_options_preserves_legacy_rekey_behavior():
    _, vectors = decoded(refresh(2))
    custom = decode_collection(vectors[12], vectors[13], 'custom', vectors[1], vectors[14])
    fields = {field['pc']: field['value'] for field in parse_collection('custom', custom)}
    mask = int.from_bytes(vectors[1][:4], 'little')
    assert fields[38475] ^ mask ^ 1348351202 == 6
    assert fields[38393] ^ mask ^ 1810204798 == 2


@pytest.mark.parametrize('field', ['resource_entry_delta', 'changed_event_types'])
@pytest.mark.parametrize('value', [-1, True, 1.5, '1', 2**32])
def test_invalid_delta_is_rejected(field, value):
    with pytest.raises(ValueError, match='delta|event'):
        refresh(1, **{field: value})


@pytest.mark.parametrize('field', ['resource_entry_delta', 'changed_event_types'])
def test_delta_requires_advancing_a_request(field):
    with pytest.raises(ValueError, match='delta'):
        refresh(1, advance_request=False, request_rate=None, elapsed_seconds=None,
                root_request_rate=None, **{field: 0})


def test_resource_delta_cannot_overflow_absolute_entry_count():
    with pytest.raises(ValueError, match='overflow'):
        refresh(1, resource_entry_delta=2**32 - 1)
