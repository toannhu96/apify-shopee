import json
from pathlib import Path
import unittest

from shopee_affiliate.sap_payload import build_payload

class TestSerializer(unittest.TestCase):
    def test_original_sdk_golden(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/sap_payload_golden.json').read_text())
        actual = build_payload(fixture['vectors'], fixture['fields'])
        self.assertEqual(actual.hex(), fixture['expected_payload_hex'])

    def test_invalid_field_index(self):
        with self.assertRaises(ValueError):
            build_payload([], [{'index':57,'kind':'uint8','value':1}])

    def test_invalid_offset(self):
        with self.assertRaises(ValueError):
            build_payload([], [{'index':0,'kind':'offset','value':0}])

    def test_duplicate_field(self):
        with self.assertRaises(ValueError):
            build_payload([], [{'index':0,'kind':'uint8','value':1}]*2)

if __name__ == '__main__': unittest.main()

import pytest


@pytest.mark.parametrize('vectors', [None, [{}], [{'kind': 'bytes', 'hex': 2}],
    [{'kind': 'bytes', 'hex': 'f' * 131072}],
    [{'kind': 'bytes', 'hex': 'ff' * 40000}] * 2])
def test_invalid_or_oversized_vectors(vectors):
    with pytest.raises(ValueError):
        build_payload(vectors, [])


@pytest.mark.parametrize('field', [
    {'index': 0, 'kind': [], 'value': 0},
    {'index': 0, 'kind': 'float32', 'value': 10**400},
    {'index': 0, 'kind': 'float32', 'value': float('nan')},
    {'index': True, 'kind': 'uint8', 'value': 1},
    {'index': 0, 'kind': 'int32', 'value': 2**31},
    {'index': 0, 'kind': 'uint16', 'value': -1},
])
def test_invalid_fields(field):
    with pytest.raises(ValueError):
        build_payload([], [field])


def test_inputs_unchanged():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_payload_golden.json').read_text())
    before = json.dumps(fixture)
    build_payload(fixture['vectors'], fixture['fields'])
    assert json.dumps(fixture) == before

REFERENCE_CASES = json.loads((Path(__file__).parent / 'fixtures/sap_serializer_vectors.json').read_text())['cases']


@pytest.mark.parametrize('case', REFERENCE_CASES, ids=lambda case: case['name'])
def test_original_sdk_serializer_reference_cases(case):
    assert build_payload(case['vectors'], case['fields']).hex() == case['expected_payload_hex']


def test_canonical_rotation_matches_original_sdk_payload():
    from shopee_affiliate.sap_request import derive_slot_rotation
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_payload_golden.json').read_text())
    rotation = derive_slot_rotation(fixture['headers']['x-sap-ri'])
    canonical = [{**field, 'index': (field['index'] - rotation) % 57}
                 for field in fixture['fields']]
    assert build_payload(fixture['vectors'], canonical, rotation=rotation).hex() == fixture['expected_payload_hex']


@pytest.mark.parametrize('rotation', [True, -1, 57, '1'])
def test_invalid_rotation(rotation):
    with pytest.raises(ValueError):
        build_payload([], [], rotation=rotation)


def test_original_sdk_nonempty_renderer_keeps_default_allocation():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_renderer_golden.json').read_text())
    actual = build_payload(fixture['vectors'], fixture['fields'], renderer_placeholder=True)
    assert actual.hex() == fixture['payload_hex']


def test_renderer_placeholder_requires_its_vector():
    with pytest.raises(ValueError):
        build_payload([], [], renderer_placeholder=True)
