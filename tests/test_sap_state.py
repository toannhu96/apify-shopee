import json
from pathlib import Path

import pytest

from shopee_affiliate.sap_codec import decode_sec
from shopee_affiliate.sap_payload import build_payload
from shopee_affiliate.sap_state import decode_payload

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/sap_payload_golden.json').read_text())


def test_decode_original_sdk_payload_exactly_rebuilds():
    raw = bytes.fromhex(FIXTURE['expected_payload_hex'])
    state = decode_payload(raw, FIXTURE['headers']['x-sap-ri'])
    assert state['vectors'] == FIXTURE['vectors']
    assert state['fields'] == FIXTURE['fields']
    assert build_payload(state['vectors'], state['fields']) == raw
    sec = decode_sec(FIXTURE['headers']['x-sap-sec'], FIXTURE['headers']['x-sap-ri'])
    assert sec.payload == raw


@pytest.mark.parametrize('payload,ri', [(b'', '00'*26), (b'\xff'*16, '00'*26),
    (b'\0'*16, '00'*26), (b'\0'*100, 'nothex'), ('secret', '00'*26)])
def test_invalid_state(payload, ri):
    with pytest.raises(ValueError):
        decode_payload(payload, ri)


def test_decoder_preserves_renderer_orphan_allocation():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_renderer_golden.json').read_text())
    raw = bytes.fromhex(fixture['payload_hex'])
    state = decode_payload(raw, fixture['ri'])
    assert state['renderer_placeholder'] is True
    assert build_payload(state['vectors'], state['fields'], renderer_placeholder=state['renderer_placeholder']) == raw


def _layout(payload):
    import struct
    root = struct.unpack_from('<I', payload, 0)[0]
    table = root - struct.unpack_from('<i', payload, root)[0]
    slots = {f['index']: table + 4 + f['index'] * 2 for f in FIXTURE['fields']}
    return root, table, slots


def _field_position(payload, field):
    import struct
    root, _, slots = _layout(payload)
    return root + struct.unpack_from('<H', payload, slots[field['index']])[0]


def _malformed_payload(case):
    import struct
    raw = bytearray.fromhex(FIXTURE['expected_payload_hex'])
    root, table, slots = _layout(raw)
    offsets = [f for f in FIXTURE['fields'] if f['kind'] == 'offset']
    if case == 'unaligned_vtable':
        size = struct.unpack_from('<H', raw, table)[0]
        raw[table-1:table-1+size] = raw[table:table+size]
        struct.pack_into('<i', raw, root, root-table+1)
    elif case == 'unaligned_root':
        raw[root:root] = b'\0'
        struct.pack_into('<I', raw, 0, root+1)
        struct.pack_into('<i', raw, root+1, root+1-table)
    elif case == 'unaligned_field':
        field = next(f for f in FIXTURE['fields'] if f['kind'] == 'uint16')
        slot = slots[field['index']]
        struct.pack_into('<H', raw, slot, struct.unpack_from('<H', raw, slot)[0]+1)
    elif case == 'overlapping_fields':
        a, b = [f for f in FIXTURE['fields'] if f['kind'] == 'uint8'][:2]
        raw[slots[b['index']]:slots[b['index']]+2] = raw[slots[a['index']]:slots[a['index']]+2]
    else:
        first = next(f for f in offsets if f['value'] == 1)
        second = next(f for f in offsets if f['value'] == 14)
        a, b = _field_position(raw, first), _field_position(raw, second)
        target = a + struct.unpack_from('<I', raw, a)[0]
        if case == 'unaligned_vector':
            struct.pack_into('<I', raw, a, target-a+1)
        elif case == 'shared_vector':
            struct.pack_into('<I', raw, b, target-b)
        elif case == 'nested_vector':
            struct.pack_into('<I', raw, target+4, 0)
            struct.pack_into('<I', raw, b, target+4-b)
    return bytes(raw)


@pytest.mark.parametrize('case', ['unaligned_root', 'unaligned_vtable', 'unaligned_field',
    'overlapping_fields', 'unaligned_vector', 'shared_vector', 'nested_vector'])
def test_pinned_schema_rejects_alignment_and_overlapping_allocations(case):
    expected = {'unaligned_root':'root offset', 'unaligned_vtable':'vtable offset',
                'unaligned_field':'field offset', 'unaligned_vector':'vector alignment'}.get(case, 'Overlapping')
    with pytest.raises(ValueError, match=expected):
        decode_payload(_malformed_payload(case), FIXTURE['headers']['x-sap-ri'])
