import json
from pathlib import Path
import unittest
from shopee_affiliate.sap_request import derive_request_key, recover_key_timestamp, request_url_digest, encode_request_timestamp, xor_repeating, derive_slot_rotation
G=json.loads((Path(__file__).parent / 'fixtures/sap_request_golden.json').read_text())
class RequestFieldGoldenTests(unittest.TestCase):
    def test_original_sdk_key_construction(self):
        key=derive_request_key(G['timestamp_seconds'],bytes.fromhex(G['random_four_hex']))
        self.assertEqual(key.hex(),G['request_key_hex'])
        self.assertEqual(recover_key_timestamp(key),G['timestamp_seconds'])
    def test_original_sdk_url_digest(self):
        self.assertEqual(request_url_digest(G['raw_url'],bytes.fromhex(G['random_eight_hex'])[4:]).hex(),G['url_digest_hex'])
    def test_original_sdk_timestamp(self):
        self.assertEqual(encode_request_timestamp(G['timestamp_ms'],bytes.fromhex(G['request_key_hex'])).hex(),G['timestamp_encoded_hex'])
    def test_original_sdk_host(self):
        self.assertEqual(xor_repeating(G['host'].encode(),bytes.fromhex(G['request_key_hex'])).hex(),G['host_encoded_hex'])
    def test_original_sdk_slot_rotation(self):
        self.assertEqual(derive_slot_rotation(G['rotation_sample']['ri']),G['rotation_sample']['rotation'])
    def test_invalid_arguments(self):
        for fn,args in [(derive_request_key,(-1,b'1234')),(derive_request_key,(1,b'')),(recover_key_timestamp,(b'',)),(xor_repeating,(b'x',b'')),(request_url_digest,('x',b'')),(encode_request_timestamp,(-1,b'12345678'))]:
            with self.assertRaises(ValueError):fn(*args)
if __name__=='__main__': unittest.main()
