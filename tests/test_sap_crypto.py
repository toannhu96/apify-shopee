import json
from pathlib import Path

import pytest

from shopee_affiliate.sap_crypto import custom_stream, decode_collection, derive_keys, encode_collection

FIXTURES = Path(__file__).parent / 'fixtures'


def test_original_custom_stream_vectors():
    for case in json.loads((FIXTURES / 'sap_custom_stream_golden.json').read_text()):
        assert custom_stream(bytes.fromhex(case['input']), bytes.fromhex(case['key'])).hex() == case['output']


@pytest.mark.parametrize('kind', ['custom', 'salsa'])
def test_original_sdk_collection_pipeline(kind):
    case = json.loads((FIXTURES / 'sap_crypto_golden.json').read_text())
    mask, random = bytes.fromhex(case['VoJ']), bytes.fromhex(case['VoR'])
    keys = derive_keys(mask, random)
    assert keys[kind].hex() == case[kind]['key']
    plain = bytes.fromhex(case[kind]['input'])
    encrypted, checksum = encode_collection(plain, kind, mask, random)
    assert encrypted.hex() == case[kind + '_masked']
    assert checksum.hex() == case[kind + '_checksum_masked']
    assert decode_collection(encrypted, checksum, kind, mask, random) == plain
    with pytest.raises(ValueError, match='checksum'):
        decode_collection(encrypted, bytes([checksum[0] ^ 1]) + checksum[1:], kind, mask, random)


@pytest.mark.parametrize('key', [b'', None, 'secret', 32])
def test_invalid_stream_key(key):
    with pytest.raises(ValueError):
        custom_stream(b'a', key)


@pytest.mark.parametrize('mask,random', [(b'', b'12345678'), (b'12345678', b''), ('12345678', b'12345678')])
def test_invalid_key_inputs(mask, random):
    with pytest.raises(ValueError):
        derive_keys(mask, random)


def test_invalid_cipher_kind():
    with pytest.raises(ValueError):
        encode_collection(b'a', 'other', b'12345678', b'12345678')
