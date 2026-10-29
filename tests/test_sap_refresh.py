import json
from pathlib import Path

from shopee_affiliate.sap_codec import decode_sec
from shopee_affiliate.sap_refresh import rekey_scalar_fields
from shopee_affiliate.sap_state import decode_payload


def test_all_root_fields_match_original_sdk_key_change():
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_rekey_golden.json').read_text())
    def decode(headers):
        return decode_payload(decode_sec(headers['x-sap-sec'], headers['x-sap-ri']).payload, headers['x-sap-ri'])
    before, after = decode(fixture['before_headers']), decode(fixture['after_headers'])
    assert rekey_scalar_fields(before['canonical_fields'], bytes.fromhex(fixture['old_key_hex']),
                              bytes.fromhex(fixture['new_key_hex'])) == after['canonical_fields']


def test_root_assembly_matches_original_sdk_with_oracle_collection_callback():
    # This validates root assembly, not collection semantic refresh. Plaintext
    # targets come from independent original SDK runs with synthetic state.
    from shopee_affiliate.sap_refresh import refresh_root
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    before = fixture['before_headers']
    for case in fixture['cases']:
        calls = []
        def oracle(kind, plaintext, context, calls=calls, case=case):
            calls.append(kind)
            assert plaintext
            assert context.old_ri == before['x-sap-ri']
            assert context.new_ri == case['target_headers']['x-sap-ri']
            assert context.raw_url == case['raw_url']
            assert context.timestamp_ms == case['timestamp_ms']
            return bytes.fromhex(case['oracle_refreshed_plaintext_hex'][kind])
        result = refresh_root(before['x-sap-sec'], before['x-sap-ri'],
            case['target_headers']['x-sap-ri'], new_mask=bytes.fromhex(case['new_mask_hex']),
            new_random=bytes.fromhex(case['new_random_hex']), raw_url=case['raw_url'],
            timestamp_ms=case['timestamp_ms'], refresh_collection=oracle,
            detector_padding=bytes.fromhex(case['detector_padding_hex']))
        assert calls == ['custom', 'salsa']
        assert result == case['target_headers']


def test_root_refresh_rejects_missing_semantic_callback():
    import pytest

    from shopee_affiliate.sap_refresh import refresh_root
    with pytest.raises(ValueError, match='callback'):
        refresh_root('', '', '', new_mask=bytes(8), new_random=bytes(8),
            raw_url='/api/v3/offer/product', timestamp_ms=0, refresh_collection=None)


def test_root_refresh_preserves_original_renderer_placeholder():
    from shopee_affiliate.sap_codec import decode_ri, encode_sec
    from shopee_affiliate.sap_refresh import refresh_root
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_renderer_golden.json').read_text())
    ri = fixture['ri']
    payload = bytes.fromhex(fixture['payload_hex'])
    old_sec = encode_sec(payload, decode_ri(ri).xor_key)
    state = decode_payload(payload, ri)
    vectors = [bytes.fromhex(v['hex']) for v in state['vectors']]
    assert state['renderer_placeholder']
    result = refresh_root(old_sec, ri, ri, new_mask=vectors[1], new_random=vectors[14],
        raw_url='/api/v3/offer/product?item_id=26609048170', timestamp_ms=1800000000000,
        refresh_collection=lambda kind, plaintext, context: plaintext,
        detector_padding=vectors[2])
    assert result == {'x-sap-ri': ri, 'x-sap-sec': old_sec}


def test_root_refresh_rejects_invalid_callback_output():
    import pytest

    from shopee_affiliate.sap_refresh import refresh_root
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    before, case = fixture['before_headers'], fixture['cases'][0]
    for invalid in (None, bytearray(5), bytes(65536)):
        with pytest.raises(ValueError, match='bounded bytes'):
            refresh_root(before['x-sap-sec'], before['x-sap-ri'], case['target_headers']['x-sap-ri'],
                new_mask=bytes.fromhex(case['new_mask_hex']), new_random=bytes.fromhex(case['new_random_hex']),
                raw_url=case['raw_url'], timestamp_ms=case['timestamp_ms'],
                refresh_collection=lambda kind, plaintext, context, invalid=invalid: invalid)


def test_advanced_request_keeps_root_window_rate_separate_from_total_rate():
    import struct

    from shopee_affiliate.sap_refresh import refresh_root
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_refresh_golden.json').read_text())
    before, case = fixture['before_headers'], fixture['cases'][0]
    supplied_rate = 0.123456789
    root_rate = 1.25
    seen = []
    def oracle(kind, plaintext, context):
        seen.append((context.advance_request, context.request_rate, context.elapsed_seconds, context.root_request_rate))
        return bytes.fromhex(case['oracle_refreshed_plaintext_hex'][kind])
    result = refresh_root(before['x-sap-sec'], before['x-sap-ri'], case['target_headers']['x-sap-ri'],
        new_mask=bytes.fromhex(case['new_mask_hex']), new_random=bytes.fromhex(case['new_random_hex']),
        raw_url=case['raw_url'], timestamp_ms=case['timestamp_ms'], refresh_collection=oracle,
        detector_padding=bytes.fromhex(case['detector_padding_hex']),
        advance_request=True, request_rate=supplied_rate, elapsed_seconds=12, root_request_rate=root_rate)
    decoded = decode_payload(decode_sec(result['x-sap-sec'], result['x-sap-ri']).payload, result['x-sap-ri'])
    assert seen == [(True, supplied_rate, 12, root_rate), (True, supplied_rate, 12, root_rate)]
    assert decoded['canonical_fields'][5]['value'] == struct.unpack('<f', struct.pack('<f', root_rate))[0]


def test_request_counter_inputs_are_validated_before_callbacks():
    import pytest

    from shopee_affiliate.sap_refresh import refresh_root
    for advance, rate in [(True, None), (True, float('nan')), (True, float('inf')),
        (True, -float('inf')), (True, -0.1), (True, 3.5e38), (True, 10**1000), (True, True),
        (True, '1'), (False, 1), (1, None), ('true', None)]:
        with pytest.raises(ValueError, match='request'):
            refresh_root('', '', '', new_mask=bytes(8), new_random=bytes(8),
                raw_url='/api/v3/offer/product', timestamp_ms=0,
                refresh_collection=lambda *args: pytest.fail('callback must not run'),
                advance_request=advance, request_rate=rate)


def test_request_elapsed_seconds_requires_explicit_uint32_when_advancing():
    import pytest

    from shopee_affiliate.sap_refresh import refresh_root
    for advance, elapsed in [(True, None), (True, -1), (True, 2**32),
                             (True, 1.0), (True, True), (True, '1'), (False, 0)]:
        with pytest.raises(ValueError, match='elapsed_seconds'):
            refresh_root('', '', '', new_mask=bytes(8), new_random=bytes(8),
                raw_url='/api/v3/offer/product', timestamp_ms=0,
                refresh_collection=lambda *args: pytest.fail('callback must not run'),
                advance_request=advance, request_rate=1 if advance else None,
                elapsed_seconds=elapsed)


def test_root_window_rate_requires_explicit_finite_float32_when_advancing():
    import pytest

    from shopee_affiliate.sap_refresh import refresh_root
    for advance, root_rate in [(True, None), (True, -1), (True, float('nan')),
                              (True, float('inf')), (True, 3.5e38), (True, True), (False, 1)]:
        with pytest.raises(ValueError, match='root_request_rate'):
            refresh_root('', '', '', new_mask=bytes(8), new_random=bytes(8),
                raw_url='/api/v3/offer/product', timestamp_ms=0,
                refresh_collection=lambda *args: pytest.fail('callback must not run'),
                advance_request=advance, request_rate=1 if advance else None,
                elapsed_seconds=0 if advance else None, root_request_rate=root_rate)


def test_aged_request_matches_original_sdk_with_separate_window_rate():
    from shopee_affiliate.sap_collections import refresh_collection
    from shopee_affiliate.sap_refresh import refresh_root
    fixture = json.loads((Path(__file__).parent / 'fixtures/sap_root_window_aged_golden.json').read_text())
    before, target = fixture['before_headers'], fixture['target_headers']
    state = decode_payload(decode_sec(target['x-sap-sec'], target['x-sap-ri']).payload, target['x-sap-ri'])
    vectors = [bytes.fromhex(v['hex']) for v in state['vectors']]
    assert state['canonical_fields'][5]['value'] == 1
    actual = refresh_root(before['x-sap-sec'], before['x-sap-ri'], target['x-sap-ri'],
        new_mask=vectors[1], new_random=vectors[14], raw_url=fixture['raw_url'],
        timestamp_ms=fixture['timestamp_ms'], refresh_collection=refresh_collection,
        detector_padding=bytes(fixture['padding']), advance_request=True,
        request_rate=120/415, elapsed_seconds=415, root_request_rate=1)
    assert actual == target


def test_xhr_requires_explicit_boolean_and_request_advancement():
    import pytest

    from shopee_affiliate.sap_refresh import refresh_root
    for advance_xhr, advance_request in [(True, False), (1, True), ('true', True)]:
        with pytest.raises(ValueError, match='advance_xhr'):
            refresh_root('', '', '', new_mask=bytes(8), new_random=bytes(8),
                raw_url='/api/v3/offer/product', timestamp_ms=0,
                refresh_collection=lambda *args: pytest.fail('must validate first'),
                advance_xhr=advance_xhr, advance_request=advance_request)


def test_xhr_page_counter_rejects_overflow_and_invalid_scalar_type():
    import pytest

    from shopee_affiliate.sap_refresh import RefreshContext, _refresh_fields
    sample = json.loads((Path(__file__).parent / 'fixtures/sap_xhr_lifecycle_golden.json').read_text())
    headers = sample['requests'][0]['headers']
    old = decode_payload(decode_sec(headers['x-sap-sec'], headers['x-sap-ri']).payload,
                         headers['x-sap-ri'])
    mask = bytes.fromhex(old['vectors'][1]['hex'])
    random = bytes.fromhex(old['vectors'][14]['hex'])
    context = RefreshContext(mask, mask, random, random, sample['raw_url'],
        sample['requests'][0]['timestamp_ms'], headers['x-sap-ri'], headers['x-sap-ri'],
        advance_request=True, request_rate=1, elapsed_seconds=0, root_request_rate=1,
        advance_xhr=True)
    exhausted = 0xffffffff ^ int.from_bytes(mask[:4], 'little') ^ 2959024693
    exhausted = exhausted if exhausted < 2**31 else exhausted - 2**32
    for value, error in [(exhausted, 'overflow'), (True, 'scalar value'), (1.5, 'scalar value')]:
        altered = {**old, 'canonical_fields': {**old['canonical_fields'],
                   28: {'kind': 'int32', 'value': value}}}
        with pytest.raises(ValueError, match=error):
            _refresh_fields(altered, context, old['vectors'])
