# name: test_purcotton_proxy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import requests

spec = importlib.util.spec_from_file_location('purcotton', Path(__file__).with_name('全棉时代_code版.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ProxyTests(unittest.TestCase):
    def test_no_replay_ambiguous_post(self):
        self.assertFalse(m.proxy_retry_safe('POST', requests.exceptions.ReadTimeout()))
        self.assertFalse(m.proxy_retry_safe('POST', requests.exceptions.ConnectionError('reset after write')))
        self.assertTrue(m.proxy_retry_safe('POST', requests.exceptions.ConnectTimeout()))
        self.assertTrue(m.proxy_retry_safe('GET', requests.exceptions.ReadTimeout()))

    def test_replaces_lease_for_later_calls(self):
        proxies = {'https': 'old'}
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.side_effect = [requests.exceptions.ConnectTimeout(), 'success']
        with patch.object(m, 'direct_session', return_value=session), patch.object(m, 'sleep'), patch.object(m, 'get_valid_proxy', return_value=({'https': 'fresh'}, '')) as refresh:
            self.assertEqual(m.request_with_proxy('POST', 'https://example.test', proxies=proxies), 'success')
        self.assertEqual(proxies['https'], 'fresh')
        refresh.assert_called_once_with('', attempts=1)

    def test_bounded_retries_and_explicit_fallback(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.side_effect = requests.exceptions.ConnectTimeout()
        with patch.object(m, 'direct_session', return_value=session), patch.object(m, 'sleep'), patch.object(m, 'ENABLE_DIRECT_FALLBACK', False), patch.object(m, 'PROXY_REQUEST_RETRIES', 3), patch.object(m, 'get_valid_proxy', return_value=({'https': 'fresh'}, '')) as refresh:
            with self.assertRaises(requests.exceptions.ConnectTimeout):
                m.request_with_proxy('POST', 'https://example.test', proxies={'https': 'old'})
            self.assertEqual(refresh.call_count, 3)
            self.assertEqual(session.request.call_count, 4)

if __name__ == '__main__':
    unittest.main()
