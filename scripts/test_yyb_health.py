import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

spec = importlib.util.spec_from_file_location("yyb_health", Path(__file__).with_name("YYB账号状态检查.py"))
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class HealthTests(unittest.TestCase):
    def test_uses_public_health_and_validates_envelope(self):
        response = Mock()
        response.json.return_value = {"code": 0, "data": {"ok": True}}
        with patch.object(health.requests, "get", return_value=response) as get:
            health.check_health("http://yyb:8000")
            get.assert_called_once_with("http://yyb:8000/health", timeout=15)
            for body in ({}, {"code": 0, "data": None}, {"code": 0, "data": {"ok": False}}, []):
                response.json.return_value = body
                with self.assertRaises(RuntimeError):
                    health.check_health("http://yyb:8000")

    def test_failed_server_checked_once_without_skipping_other_server(self):
        with patch.object(health, "parse_server_lines", return_value=[("http://bad", "1"), ("http://bad", "2"), ("http://good", "3")]), patch.object(health, "check_health", side_effect=[requests.Timeout("timeout"), None]) as check, patch.object(health, "prune", return_value={"removed": []}) as prune, patch("builtins.print"):
            self.assertEqual(health.main(), 0)
            self.assertEqual(check.call_count, 2)
            prune.assert_called_once_with(refs=["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
