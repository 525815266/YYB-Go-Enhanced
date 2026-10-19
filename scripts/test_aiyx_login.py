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
        self.assertIn("不能据此认定", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("并非登录参数错误", output)

    def test_generic_id_is_not_a_verified_userid(self):
        (userid, error), _ = self.login({"error_code": 10001, "data": {"id": 123}})
        self.assertIsNone(userid)
        self.assertIn("未取得会员 user_id", error["error_msg"])

    def test_minalogin_uses_lowercase_code_without_baselogin_fields(self):
        response = Mock(status_code=200)
        response.json.return_value = {"error_code": "", "user_id": "123"}
        with patch.object(aiyx, "request_with_proxy", return_value=response) as request, contextlib.redirect_stdout(io.StringIO()):
            userid, _ = aiyx.login_by_code("example@1", "private-code", None)
        self.assertEqual(userid, "123")
        self.assertEqual(request.call_args.args, ("POST", "https://japi.yinxingyanjing.com/sso/login/minalogin"))
        self.assertEqual(request.call_args.kwargs["json"], {"sourceChannel": "Mina", "code": "private-code"})

    def test_business_failure_must_not_accept_or_cache_userid(self):
        response = Mock(status_code=200)
        response.json.return_value = {"error_code": 40001, "error_msg": "invalid code private-code", "data": {"user_id": "123"}}
        with patch.object(aiyx, "get_cached_token", return_value=None), patch.object(aiyx, "get_code", return_value="private-code"), patch.object(aiyx, "request_with_proxy", return_value=response), patch.object(aiyx, "set_cached_token") as save, contextlib.redirect_stdout(io.StringIO()) as output:
            userid, error = aiyx.login_with_cache("example@1", None)
        self.assertIsNone(userid)
        save.assert_not_called()
        self.assertIn("业务拒绝", error["error_msg"])
        self.assertNotIn("private-code", output.getvalue())

    def test_empty_status_with_top_level_identity_matches_live_response(self):
        (userid, error), output = self.login({"error_code": "", "open_id": "private-openid", "union_id": "private-unionid", "user_id": ""})
        self.assertIsNone(userid)
        self.assertIn("未取得会员 user_id", error["error_msg"])
        self.assertNotIn("private-openid", output)
        self.assertNotIn("private-unionid", output)

    def test_invalid_userid_types_are_not_saved_as_identity(self):
        for candidate in (True, [], {"id": "123"}, " null ", "  ", "0", -1):
            with self.subTest(candidate=candidate):
                (userid, _), _ = self.login({"error_code": 0, "data": {"user_id": candidate}})
                self.assertIsNone(userid)

    def test_top_level_and_nested_success_statuses(self):
        for code in (None, "", 0, "0", 10001, "10001"):
            for identity in ({"user_id": "123"}, {"data": {"user_id": "123"}}):
                with self.subTest(code=code, identity=identity):
                    (userid, _), _ = self.login({"error_code": code, **identity})
                    self.assertEqual(userid, "123")

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

    def test_http_error_cannot_validate_cached_identity(self):
        response = Mock(status_code=503)
        response.json.return_value = {"error_code": 0, "data": {"gold": 100}}
        with patch.object(aiyx, "get_cached_token", return_value="cached"), patch.object(aiyx, "request_with_proxy", return_value=response), patch.object(aiyx, "get_code") as code, contextlib.redirect_stdout(io.StringIO()):
            userid, error = aiyx.login_with_cache("example@1", None)
        self.assertIsNone(userid)
        code.assert_not_called()
        self.assertIn("保留缓存", error["error_msg"])


class SignTests(unittest.TestCase):
    def run_account(self, responses):
        with patch.object(aiyx, "get_valid_proxy", return_value=(None, "")), patch.object(aiyx, "sleep"), patch.object(aiyx, "login_with_cache", return_value=("123", None)), patch.object(aiyx, "api_post", side_effect=responses) as post, contextlib.redirect_stdout(io.StringIO()) as output:
            result = aiyx.run_account(1, 1, "example@1")
        return result, post, output.getvalue()

    def test_missing_today_state_does_not_report_already_signed(self):
        result, post, output = self.run_account([{"error_code": 0, "data": {}}])
        self.assertFalse(result["success"])
        self.assertNotIn("今日已签到", output)
        self.assertEqual(post.call_count, 1)

    def test_missing_query_business_status_does_not_submit(self):
        result, post, _ = self.run_account([{"data": {"todaySignInfo": {"isSign": 0}}}])
        self.assertFalse(result["success"])
        self.assertEqual(post.call_count, 1)

    def test_failed_sign_submission_is_not_account_success(self):
        result, post, _ = self.run_account([
            {"error_code": 0, "data": {"todaySignInfo": {"isSign": 0}}},
            {"error_code": 40001},
        ])
        self.assertFalse(result["success"])
        self.assertEqual(post.call_count, 2)

    def test_sign_success_is_rechecked_and_accepts_string_status(self):
        result, post, _ = self.run_account([
            {"error_code": "0", "data": {"todaySignInfo": {"isSign": "0"}}},
            {"error_code": "0"},
            {"error_code": "0", "data": {"todaySignInfo": {"isSign": "1"}}},
            {"error_code": "0", "data": {"gold": 100}},
        ])
        self.assertTrue(result["success"])
        self.assertIn("已复查", result["signMsg"])
        self.assertEqual(post.call_count, 4)
        self.assertEqual([call.args[1] for call in post.call_args_list].count(aiyx.SIGN_IN_URL), 1)

    def test_unconfirmed_sign_is_not_repeated(self):
        result, post, _ = self.run_account([
            {"error_code": 0, "data": {"todaySignInfo": {"isSign": 0}}},
            {"error_code": 0},
            {"error_code": 0, "data": {"todaySignInfo": {"isSign": 0}}},
        ])
        self.assertFalse(result["success"])
        self.assertEqual(post.call_count, 3)
        self.assertIn("不重复提交", result["error"])

    def test_already_signed_only_queries_assets(self):
        result, post, _ = self.run_account([
            {"error_code": 0, "data": {"todaySignInfo": {"isSign": 1}}},
            {"error_code": 0, "data": {"gold": 100}},
        ])
        self.assertTrue(result["success"])
        self.assertEqual(post.call_count, 2)
        self.assertFalse(any(call.args[1] == aiyx.SIGN_IN_URL for call in post.call_args_list))


if __name__ == "__main__":
    unittest.main()
