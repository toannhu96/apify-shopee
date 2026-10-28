import json
from pathlib import Path
import unittest
from shopee_affiliate.sap_salsa20 import salsa20
G = json.loads((Path(__file__).parent / 'fixtures/sap_salsa_golden.json').read_text())
class CapturedSDKCipherTests(unittest.TestCase):
    def test_original_sdk_ciphertext(self):
        values={k:bytes.fromhex(v['hex']) for k,v in G.items()}
        self.assertEqual(salsa20(values['plaintext'],values['key'],values['nonce']),values['ciphertext'])
    def test_counter_continuation_against_sdk(self):
        key=bytes.fromhex(G['key']['hex']);nonce=bytes.fromhex(G['nonce']['hex'])
        plaintext=bytes.fromhex(G['plaintext']['hex']);ciphertext=bytes.fromhex(G['ciphertext']['hex'])
        self.assertEqual(salsa20(plaintext[64:],key,nonce,counter=1),ciphertext[64:])
    def test_validation(self):
        for key,nonce,counter in [(b'',b'12345678',0),(b'x'*32,b'',0),(b'x'*32,b'x'*8,-1)]:
            with self.assertRaises(ValueError):salsa20(b'x',key,nonce,counter)
if __name__=='__main__':unittest.main()
