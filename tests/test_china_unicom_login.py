"""Credential compatibility checks; no live requests or activity completion."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

spec = importlib.util.spec_from_file_location('china_unicom', Path(__file__).resolve().parents[1] / 'scripts' / 'chinaUnicom.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.directory.name, 'cache.json')
        self.cache_patch = patch.object(module, 'UNICOM_TOKEN_CACHE_PATH', self.cache_path)
        self.cache_patch.start()
        self.sleep_patch = patch.object(module.time, 'sleep')
        self.sleep_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.sleep_patch.stop)
        self.addCleanup(self.directory.cleanup)

    def user(self, config='original-token'):
        user = module.UserService(1, config)
        user.log = Mock()
        self.addCleanup(user.session.close)
        return user

    def test_password_rsa_format_matches_attachment(self):
        key = RSA.generate(1024)
        user = self.user('18600000000#service-password')
        with patch.object(module, 'LOGIN_PUB_KEY', key.public_key().export_key().decode()):
            encrypted = user.rsa_encrypt('service-password')
        plaintext = PKCS1_v1_5.new(key).decrypt(base64.b64decode(encrypted), None).decode()
        self.assertTrue(plaintext.startswith('service-password'))
        self.assertRegex(plaintext[len('service-password'):], r'^\d{6}$')

    def test_password_then_online_then_cache(self):
        user = self.user('18600000000#service-password')
        user.request = Mock(side_effect=[
            response({'code': '0000', 'token_online': 'login-token'}),
            response({'code': 0, 'ecs_token': 'ecs', 'token_online': 'rotated-token', 'desmobile': '18600000000'}),
        ])
        self.assertTrue(user.ensure_login())
        self.assertEqual(user.request.call_count, 2)
        login = user.request.call_args_list[0]
        self.assertTrue(login.args[1].endswith('/login.htm'))
        self.assertNotEqual(login.kwargs['data']['password'], 'service-password')
        self.assertFalse(login.kwargs['_allow_replay'])
        with open(self.cache_path, encoding='utf-8') as f:
            cache = json.load(f)
        self.assertEqual(cache['18600000000']['token_online'], 'rotated-token')
        self.assertNotIn('service-password', json.dumps(cache))

    def test_password_denial_submitted_once(self):
        user = self.user('18600000000#service-password')
        user.request = Mock(return_value=response({'code': '3006'}))
        self.assertFalse(user.ensure_login())
        self.assertEqual(user.request.call_count, 1)
        self.assertFalse(os.path.exists(self.cache_path))

    def test_success_without_token_is_not_login(self):
        user = self.user('18600000000#service-password')
        user.request = Mock(return_value=response({'code': '0'}))
        self.assertFalse(user.ensure_login())
        self.assertEqual(user.request.call_count, 1)
        self.assertFalse(user.valid)

    def test_token_cache_survives_rotation_without_mobile_in_config(self):
        user = self.user()
        user.request = Mock(return_value=response({'code': '0', 'ecs_token': 'ecs', 'token_online': 'rotated-token', 'desmobile': '18600000000'}))
        self.assertTrue(user.ensure_login())
        again = self.user()
        self.assertTrue(again.load_token_from_cache())
        self.assertEqual(again.token_online, 'rotated-token')
        self.assertEqual(again.session.cookies.get('token_online'), 'rotated-token')

    def test_network_failure_keeps_cache_and_does_not_submit_password(self):
        user = self.user('18600000000#service-password')
        user.valid = True
        user.token_online = 'cached-token'
        user.save_token_to_cache()
        before = Path(self.cache_path).read_bytes()
        user.request = Mock(return_value=None)
        user.unicom_login = Mock()
        self.assertFalse(user.ensure_login())
        user.unicom_login.assert_not_called()
        self.assertFalse(user.valid)
        self.assertEqual(Path(self.cache_path).read_bytes(), before)

    def test_rejected_cached_token_falls_back_to_configured_token(self):
        user = self.user()
        user.valid = True
        user.token_online = 'cached-token'
        user.save_token_to_cache()
        user.request = Mock(side_effect=[response({'code': '1'}), response({'code': '0', 'ecs_token': 'ecs'})])
        self.assertTrue(user.ensure_login())
        self.assertEqual([call.kwargs['data']['token_online'] for call in user.request.call_args_list], ['cached-token', 'original-token'])

    def test_existing_cached_session_does_not_use_password(self):
        user = self.user('18600000000#service-password')
        user.valid = True
        user.token_online = 'cached-token'
        user.save_token_to_cache()
        user.request = Mock(return_value=response({'code': '0', 'ecs_token': 'ecs'}))
        user.unicom_login = Mock()
        self.assertTrue(user.ensure_login())
        user.unicom_login.assert_not_called()

    def test_online_missing_ecs_cannot_poison_cache(self):
        user = self.user()
        user.request = Mock(return_value=response({'code': '0', 'token_online': 'bad-token'}))
        self.assertFalse(user.ensure_login())
        self.assertFalse(user.valid)
        self.assertEqual(user.token_online, 'original-token')
        self.assertFalse(os.path.exists(self.cache_path))

    def test_other_mobile_cannot_replace_configured_account(self):
        user = self.user('original-token#appid#18600000000')
        user.request = Mock(return_value=response({'code': '0', 'ecs_token': 'ecs', 'desmobile': '18600000001'}))
        self.assertFalse(user.ensure_login())
        self.assertEqual(user.account_mobile, '18600000000')
        self.assertEqual(user.request.call_count, 1)
        self.assertFalse(os.path.exists(self.cache_path))

    def test_http_error_not_accepted_as_password_login(self):
        user = self.user('18600000000#service-password')
        user.request = Mock(return_value=response({'code': '0', 'token_online': 'not-valid'}, 503))
        self.assertFalse(user.ensure_login())
        self.assertEqual(user.request.call_count, 1)

    def test_explicit_business_cookie_is_not_overwritten(self):
        user = self.user()
        with patch.object(user.session._session, 'request', return_value=response({})) as request:
            user.request('GET', 'https://example.test', headers={'Cookie': 'ecs_token=selected'})
        self.assertEqual(request.call_args.kwargs['headers']['Cookie'], 'ecs_token=selected')

    def test_auth_network_error_is_not_replayed_by_proxy_failover(self):
        user = self.user()
        user.configure_proxy = Mock()
        with patch.dict(os.environ, {'UNICOM_PROXY_API': 'https://example.test/proxy'}):
            with patch.object(user.session._session, 'request', side_effect=requests.ConnectionError()) as request:
                self.assertIsNone(user.request('POST', 'https://example.test', _allow_replay=False))
        self.assertEqual(request.call_count, 1)
        user.configure_proxy.assert_not_called()

    def test_corrupt_cache_is_preserved(self):
        Path(self.cache_path).write_text('broken', encoding='utf-8')
        user = self.user()
        user.request = Mock(return_value=response({'code': '0', 'ecs_token': 'ecs'}))
        self.assertTrue(user.ensure_login())
        self.assertEqual(Path(self.cache_path).read_text(), 'broken')

    def test_proxy_whitelist_denial_is_not_retried_for_each_account(self):
        user = self.user()
        with patch.dict(module.UNICOM_PROXY_AUTH_ERRORS, {}, clear=True):
            with patch.dict(os.environ, {'UNICOM_PROXY_API': 'https://example.test/extract'}):
                with patch.object(module.requests, 'get', return_value=response({'code': -1, 'message': '请先加入白名单'})) as request:
                    user.configure_proxy()
                    self.user('second-token').configure_proxy()
        self.assertEqual(request.call_count, 1)
        self.assertFalse(user.session.proxies)


if __name__ == '__main__':
    unittest.main()
