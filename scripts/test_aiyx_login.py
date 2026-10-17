import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("aiyx", Path(__file__).with_name("爱隐形_code版.py"))
aiyx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aiyx)


class LoginTests(unittest.TestCase):
    def login(self, payload, status=200):
        response = Mock(status_code=status)
        response.json.return_value = payload
        with patch.object(aiyx, "request_with_proxy", return_value=response), contextlib.redirect_stdout(io.StringIO()) as output:
            result = aiyx.login_by_code("example@1", "private-code", None)
        return result, output.getvalue()

    def test_user_not_found_is_not_diagnosed_as_yyb_or_proxy_failure(self):
        (userid, error), output = self.login({"error_code": 1, "error_msg": "用户不存在", "code": "secret"})
        self.assertIsNone(userid)
        self.assertIn("参数尚未验证", error["error_msg"])
        self.assertIn("不能据此认定", output)
        self.assertNotIn("secret", output)

    def test_generic_id_is_not_a_verified_userid(self):
        (userid, error), _ = self.login({"error_code": 10001, "data": {"id": 123}})
        self.assertIsNone(userid)
        self.assertIn("未包含明确的 userid", error["error_msg"])

    def test_explicit_userid_and_string_status(self):
        (userid, _), _ = self.login({"error_code": "10001", "data": {"user_id": "123"}})
        self.assertEqual(userid, "123")

    def test_http_error_does_not_accept_userid(self):
        (userid, error), _ = self.login({"error_code": 10001, "data": {"userid": "123"}}, 503)
        self.assertIsNone(userid)
        self.assertIn("HTTP 503", error["error_msg"])

    def test_malformed_response_is_not_printed(self):
        (userid, error), output = self.login(["secret-token"])
        self.assertIsNone(userid)
        self.assertNotIn("secret-token", output)

    def test_cache_requires_explicit_business_success(self):
        for response in ({"data": {}}, {"error_code": 0}, {"error_code": -1, "error_msg": "JSON解析失败"}):
            with self.subTest(response=response), patch.object(aiyx, "get_cached_token", return_value="cached"), patch.object(aiyx, "api_post", return_value=response), patch.object(aiyx, "get_code") as code, contextlib.redirect_stdout(io.StringIO()):
                userid, error = aiyx.login_with_cache("example@1", None)
            self.assertIsNone(userid)
            code.assert_not_called()
            self.assertIn("保留缓存", error["error_msg"])

    def test_cache_network_failure_does_not_trigger_unverified_login(self):
        with patch.object(aiyx, "get_cached_token", return_value="cached"), patch.object(aiyx, "api_post", side_effect=TimeoutError()), patch.object(aiyx, "get_code") as code, patch.object(aiyx, "set_cached_token") as save, contextlib.redirect_stdout(io.StringIO()):
            userid, error = aiyx.login_with_cache("example@1", None)
        self.assertIsNone(userid)
        code.assert_not_called()
        save.assert_not_called()
        self.assertIn("请求异常不代表", error["error_msg"])


if __name__ == "__main__":
    unittest.main()
