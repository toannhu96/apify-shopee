import json
from pathlib import Path

import pytest

from shopee_affiliate.sap_detector import decode_detectors, encode_detectors


def test_original_sdk_key_and_padding_cases():
    cases = json.loads((Path(__file__).parent / 'fixtures/sap_detector_golden.json').read_text())['cases']
    decoded = []
    for case in cases:
        key, block, padding, values = (bytes.fromhex(case[k]) for k in ('key', 'block', 'padding', 'values'))
        assert decode_detectors(block, key) == values
        assert encode_detectors(values, key, padding) == block
        decoded.append(values)
    assert decoded[0] == decoded[1]


@pytest.mark.parametrize('block,key', [(bytes(20), bytes(8)), (bytes(21), bytes(7)), ('x', bytes(8))])
def test_invalid_block(block, key):
    with pytest.raises(ValueError):
        decode_detectors(block, key)


def test_random_padding_preserves_supplied_facts():
    facts, key = bytes(range(20)), bytes(range(8))
    block = encode_detectors(facts, key)
    assert decode_detectors(block, key) == facts
    assert facts == bytes(range(20))
