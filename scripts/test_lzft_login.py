import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("lzft", Path(__file__).with_name("lz飞天.py"))
lzft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lzft)


class LoginTests(unittest.TestCase):
    def test_legacy_shared_context_cache_is_not_reused(self):
        with patch.object(lzft, "load_token_cache", return_value={"example@1": {"token": "legacy-token"}}), contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(lzft.get_cached_token("example@1"))
        with patch.object(lzft, "load_token_cache", return_value={"example@1": {"token": "verified-token", "source": "account_context_v2"}}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lzft.get_cached_token("example@1"), "verified-token")

    def test_context_is_bound_to_each_account(self):
        with patch.dict(os.environ, {"LZ_XCX_DATA": json.dumps({"example@1": "first", "example@2": "second"})}):
            self.assertEqual(lzft.account_context("example@1"), "first")
            self.assertEqual(lzft.account_context("example@2"), "second")
            self.assertEqual(lzft.account_context("example@3"), "")
        with patch.dict(os.environ, {"LZ_XCX_DATA": "single-context"}), patch.object(lzft, "SERVERS", ["example@1", "example@2"]):
            self.assertEqual(lzft.account_context("example@1"), "")
        with patch.dict(os.environ, {"LZ_XCX_DATA": "single-context"}), patch.object(lzft, "SERVERS", ["example@1"]):
            self.assertEqual(lzft.account_context("example@1"), "single-context")

    def test_missing_context_does_not_request_code_or_decode(self):
        with patch.object(lzft, "get_cached_token", return_value=None), patch.object(lzft, "account_context", return_value=""), patch.object(lzft, "get_code") as code, patch.object(lzft, "decode_user_token") as decode, contextlib.redirect_stdout(io.StringIO()):
            token, error, _ = lzft.login_with_cache("example@1", None)
        self.assertIsNone(token)
        self.assertIn("LZ_XCX_DATA", error["msg"])
        code.assert_not_called()
        decode.assert_not_called()

    def test_decoded_but_expired_token_is_not_cached_or_reported_as_login(self):
        with patch.object(lzft, "get_cached_token", return_value=None), patch.object(lzft, "account_context", return_value="private-context"), patch.object(lzft, "decode_user_token", return_value={"code": 200, "data": {"userToken": "private-token"}}), patch.object(lzft, "check_token", return_value={"code": 601, "msg": "登录超时"}), patch.object(lzft, "set_cached_token") as save, patch.object(lzft, "get_code") as code, contextlib.redirect_stdout(io.StringIO()) as output:
            token, _, _ = lzft.login_with_cache("example@1", None)
        self.assertIsNone(token)
        save.assert_not_called()
        code.assert_not_called()
        self.assertNotIn("token 获取成功", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("private-context", output.getvalue())

    def test_validated_token_is_saved_before_requesting_sign_code(self):
        with patch.object(lzft, "get_cached_token", return_value=None), patch.object(lzft, "account_context", return_value="private-context"), patch.object(lzft, "decode_user_token", return_value={"code": 200, "data": {"userToken": "private-token"}}), patch.object(lzft, "check_token", return_value={"code": 200, "data": {"userId": 123}}), patch.object(lzft, "set_cached_token") as save, patch.object(lzft, "get_code", return_value="fresh-code") as code, contextlib.redirect_stdout(io.StringIO()):
            token, error, used_code = lzft.login_with_cache("example@1", None)
        self.assertEqual((token, error, used_code), ("private-token", None, "fresh-code"))
        save.assert_called_once_with("example@1", "private-token")
        code.assert_called_once_with("example@1")

    def test_cache_network_failure_is_preserved_without_login(self):
        for check in ({"code": -1, "msg": "业务请求异常，未确认登录状态"}, {"code": 503}, {"code": 200, "data": None}):
            with self.subTest(check=check), patch.object(lzft, "get_cached_token", return_value="cached"), patch.object(lzft, "check_token", return_value=check), patch.object(lzft, "remove_cached_token") as remove, patch.object(lzft, "decode_user_token") as decode, patch.object(lzft, "get_code") as code, contextlib.redirect_stdout(io.StringIO()):
                token, error, _ = lzft.login_with_cache("example@1", None)
            self.assertIsNone(token)
            self.assertIn("保留", error["msg"])
            remove.assert_not_called()
            decode.assert_not_called()
            code.assert_not_called()

    def test_http_failure_cannot_validate_token(self):
        response = Mock(status_code=503)
        response.json.return_value = {"code": 200, "data": {"userId": 123}}
        with patch.object(lzft, "request_with_proxy", return_value=response):
            self.assertFalse(lzft.valid_member(lzft.check_token("example@1", "private-token", None)))

    def test_explicit_rejection_overrides_success_flags(self):
        for response in ({"success": True, "code": 601}, {"code": False}, {"success": False, "code": 200}, {"status": False}, {"code": 200, "data": None}, {"code": 200, "data": {"message": "ok"}}, {"code": 200, "data": {"userId": True}}):
            with self.subTest(response=response):
                self.assertFalse(lzft.valid_member(response))

    def test_decode_uses_account_context_and_never_puts_credentials_in_referer(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200, "data": "private-token"}
        with patch.object(lzft, "request_with_proxy", return_value=response) as request:
            lzft.decode_user_token("example@1", "private-context", None)
        self.assertEqual(request.call_args.kwargs["data"], {"context": "private-context"})
        self.assertNotIn("private-context", request.call_args.kwargs["headers"]["Referer"])

    def test_sign_auth_failure_stops_without_record_query(self):
        with patch.object(lzft, "api_request", return_value={"code": 601, "msg": "登录超时"}) as request, contextlib.redirect_stdout(io.StringIO()):
            _, signed, rejected = lzft.do_sign("private-token", "private-code", "example@1", None)
        self.assertFalse(signed)
        self.assertTrue(rejected)
        self.assertEqual(request.call_count, 1)

    def test_failed_sign_stops_account_without_retrying_or_running_activities(self):
        with patch.object(lzft, "get_valid_proxy", return_value=(None, "")), patch.object(lzft, "sleep"), patch.object(lzft, "login_with_cache", return_value=("private-token", None, "private-code")) as login, patch.object(lzft, "do_sign", return_value=("签到失败: 登录超时", False, True)) as sign, patch.object(lzft, "remove_cached_token") as remove, patch.object(lzft, "run_activities") as activities, contextlib.redirect_stdout(io.StringIO()):
            result = lzft.run_account(1, 1, "example@1")
        self.assertFalse(result["success"])
        self.assertNotIn("private-token", json.dumps(result))
        login.assert_called_once()
        sign.assert_called_once()
        remove.assert_called_once_with("example@1")
        activities.assert_not_called()


if __name__ == "__main__":
    unittest.main()
