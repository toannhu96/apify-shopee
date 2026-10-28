import json
from pathlib import Path
import unittest
from shopee_affiliate.sap_codec import decode_ri, decode_sec, encode_ri, encode_sec, inspect_flatbuffer, xxh32
GOLDEN = json.loads((Path(__file__).parent / 'fixtures' / 'sap_golden.json').read_text())

class OriginalSDKGoldenTests(unittest.TestCase):
    def test_ri_matches_original_sdk(self):
        self.assertEqual(encode_ri(GOLDEN['timestamp_seconds'], bytes.fromhex(GOLDEN['entropy_hex'])), GOLDEN['headers']['x-sap-ri'])
        ri = decode_ri(GOLDEN['headers']['x-sap-ri'])
        self.assertEqual(ri.xor_key, GOLDEN['xor_key'])
        self.assertEqual((ri.protocol_version, ri.marker, ri.mode), (3, 5, 1))

    def test_sec_matches_original_sdk_not_just_roundtrip(self):
        payload = bytes.fromhex(GOLDEN['payload_hex'])
        self.assertEqual(encode_sec(payload, GOLDEN['xor_key']), GOLDEN['headers']['x-sap-sec'])
        decoded = decode_sec(GOLDEN['headers']['x-sap-sec'], GOLDEN['headers']['x-sap-ri'])
        self.assertEqual(decoded.payload, payload)
        self.assertEqual(decoded.checksum, GOLDEN['xxh32_expected'])
        self.assertEqual(decode_sec(GOLDEN['headers']['x-sap-sec']), decoded)

    def test_standard_xxh32_vectors(self):
        self.assertEqual(xxh32(b''), 0x02CC5D05)
        self.assertEqual(xxh32(b'a'), 0x550D7456)
        self.assertEqual(xxh32(b'abc'), 0x32D153FF)

    def test_flatbuffer_structure(self):
        payload = bytes.fromhex(GOLDEN['payload_hex'])
        view = inspect_flatbuffer(payload)
        self.assertEqual(view['root_offset'], 124)
        self.assertEqual(view['vtable_offset'], 6)
        self.assertEqual(view['vtable_size'], 118)
        self.assertEqual(len(view['field_offsets']), 57)

    def test_reject_corruption(self):
        sec=GOLDEN['headers']['x-sap-sec']
        with self.assertRaises(ValueError): decode_sec(('s' if sec[0]!='s' else 'h')+sec[1:])
        for value in ['', '!!!', 's']:
            with self.assertRaises(ValueError): decode_sec(value)
        for value in ['', 'z'*52]:
            with self.assertRaises(ValueError): decode_ri(value)
        with self.assertRaises(ValueError): encode_ri(-1,b'x'*22)
        with self.assertRaises(ValueError): encode_ri(1,b'x')
        with self.assertRaises(ValueError): encode_sec(b'x'*65536,1)
        with self.assertRaises(ValueError): encode_sec(b'x',256)
        with self.assertRaises(ValueError): encode_sec(b'x',1,-1)
        for value in [b'',b'\xff'*8]:
            with self.assertRaises(ValueError): inspect_flatbuffer(value)

if __name__ == '__main__': unittest.main()

# Public API boundary tests; malformed inputs must not allocate from integers.
import pytest


@pytest.mark.parametrize('data', [None, 1000000, 'abc', [1, 2, 3]])
def test_reject_non_byte_payload(data):
    for operation in (lambda: encode_sec(data, 1), lambda: xxh32(data),
                      lambda: inspect_flatbuffer(data)):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize('value', [True, False, None, '1', -1, 2**32])
def test_reject_bad_timestamp(value):
    with pytest.raises(ValueError):
        encode_ri(value, b'x' * 22)


@pytest.mark.parametrize('value', [True, False, None, '1', -1, 256])
def test_reject_bad_xor_key(value):
    with pytest.raises(ValueError):
        encode_sec(b'', value)


@pytest.mark.parametrize('value', [None, 42, b'aa', 'x' * 90000])
def test_reject_invalid_header_type_and_size(value):
    with pytest.raises(ValueError):
        decode_sec(value)
    with pytest.raises(ValueError):
        decode_ri(value)


def test_reject_noncanonical_padding_bits():
    from shopee_affiliate.sap_codec import ALPHABET
    encoded = encode_sec(b'', 0)  # 10 bytes -> two base64 padding chars.
    index = ALPHABET.index(encoded[-3])
    noncanonical = encoded[:-3] + ALPHABET[index + 1] + '=='
    with pytest.raises(ValueError):
        decode_sec(noncanonical)


def test_reject_unsupported_envelope_version():
    with pytest.raises(ValueError):
        encode_sec(b'abc', 4, 123)


@pytest.mark.parametrize('payload', [
    __import__('struct').pack('<IHHI', 8, 4, 0, 4),
    __import__('struct').pack('<IHHII', 8, 6, 8, 4, 0),
])
def test_reject_impossible_flatbuffer_tables(payload):
    with pytest.raises(ValueError):
        inspect_flatbuffer(payload)
