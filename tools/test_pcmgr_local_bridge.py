"""Offline checks for identity-bound PCMgr key rotation; no DLLs or network."""
import unittest

from pcmgr_local_bridge import ServiceRejected, decode_response, field, renewed_credentials, validate_credentials


class PCMgrBridgeTests(unittest.TestCase):
    def setUp(self):
        self.creds = {'schema': 1, 'provider': 'pcmgr', 'account_id': 123,
                      'loginkey': 'synthetic-old-key', 'client_guid': 'ab' * 16}
        self.raw = field(2, 2) + field(3, 123) + field(4, 'synthetic-new-key') + field(5, 5097600)

    def test_rotation_keeps_identity_and_input_unchanged(self):
        new = renewed_credentials(self.raw, self.creds)
        self.assertEqual(new['account_id'], self.creds['account_id'])
        self.assertEqual(new['client_guid'], self.creds['client_guid'])
        self.assertEqual(new['next_renew_len'], 5097600)
        self.assertEqual(new['loginkey'], 'synthetic-new-key')
        self.assertEqual(self.creds['loginkey'], 'synthetic-old-key')

    def test_rejection_does_not_rotate(self):
        with self.assertRaises(ServiceRejected) as caught:
            renewed_credentials(field(1, 5) + self.raw, self.creds)
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(self.creds['loginkey'], 'synthetic-old-key')

    def test_bad_or_other_identity_cannot_rotate(self):
        for raw in [b'', self.raw[:-1], self.raw + field(4, 'duplicate'),
                    field(2, 16) + field(3, 123) + field(4, 'key') + field(5, 100),
                    field(2, 2) + field(3, 124) + field(4, 'key') + field(5, 100),
                    field(2, 2) + field(3, 123) + field(4, '') + field(5, 100),
                    field(2, 2) + field(3, 123) + field(4, 'key'),
                    b'\x80' * 10 + b'\x01']:
            with self.subTest(raw_length=len(raw)), self.assertRaises(ValueError):
                renewed_credentials(raw, self.creds)

    def test_ticket_response_must_not_accept_error_as_success(self):
        with self.assertRaises(ServiceRejected):
            decode_response(field(1, 7) + field(3, 'ticket'), {1: 0, 3: 2})
        with self.assertRaises(ValueError):
            decode_response(field(1, 'wrong-wire'), {1: 0})

    def test_profile_cannot_be_an_yyb_credential_record(self):
        validate_credentials(self.creds)
        with self.assertRaises(ValueError):
            validate_credentials({**self.creds, 'provider': 'yyb'})
        with self.assertRaises(ValueError):
            validate_credentials({**self.creds, 'client_guid': 'AB' * 16})


if __name__ == '__main__':
    unittest.main()
