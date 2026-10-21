import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("dongcheng", Path(__file__).with_name("东呈_code版.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


class DongChengTests(unittest.TestCase):
    def run_account(self, response):
        with patch.object(dc, "get_valid_proxy", return_value=(None, "")), patch.object(dc, "sleep"), patch.object(dc, "login_with_cache", return_value=("member-token", None)), patch.object(dc, "api_get", return_value=response), contextlib.redirect_stdout(io.StringIO()) as output:
            result = dc.run_account(1, 1, "example@1")
        return result, output.getvalue()

    def test_business_failure_is_not_counted_as_success_or_printed_raw(self):
        for payload in ({"code": 40001, "token": "private-token"}, {}, {"code": False}, {"code": []}):
            with self.subTest(payload=payload):
                result, output = self.run_account(payload)
                self.assertFalse(result["success"])
                self.assertNotIn("private-token", output)

    def test_known_success_code_and_points(self):
        for code in (0, "0"):
            with self.subTest(code=code):
                result, _ = self.run_account({"code": code, "results": 5})
                self.assertTrue(result["success"])
                self.assertEqual(result["points"], "5")

    def test_legacy_compatible_status_does_not_claim_new_points_or_already_signed(self):
        for code in (121300002, "121300002"):
            with self.subTest(code=code):
                result, _ = self.run_account({"code": code, "results": 5})
                self.assertTrue(result["success"])
                self.assertIn("新增积分未确认", result["signMsg"])
                self.assertNotIn("已签到", result["signMsg"])
                self.assertNotEqual(result["points"], "5")

    def test_http_error_cannot_become_sign_in_success(self):
        response = Mock(status_code=503)
        response.json.return_value = {"code": 0, "results": 5}
        with patch.object(dc, "request_with_proxy", return_value=response), contextlib.redirect_stdout(io.StringIO()):
            status, _, _ = dc.do_checkin("example@1", "token", None)
        self.assertFalse(status)

    def test_configured_blackbox_is_sent_as_in_the_source_script(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 0, "results": 5}
        with patch.object(dc, "BLACKBOX", "private-device-data"), patch.object(dc, "request_with_proxy", return_value=response) as request, contextlib.redirect_stdout(io.StringIO()):
            status, _, _ = dc.do_checkin("example@1", "token", None)
        self.assertTrue(status)
        self.assertEqual(request.call_args.kwargs["params"], {"blackbox": "private-device-data"})

    def test_http_error_or_business_failure_cannot_save_a_login_token(self):
        for status, payload in ((503, {"code": 0, "token": "private-token"}), (200, {"code": 40001, "token": "private-token"})):
            with self.subTest(status=status):
                response = Mock(status_code=status)
                response.json.return_value = payload
                with patch.object(dc, "request_with_proxy", return_value=response), patch.object(dc, "get_cached_token", return_value=None), patch.object(dc, "get_code", return_value="private-code"), patch.object(dc, "set_cached_token") as save, contextlib.redirect_stdout(io.StringIO()) as output:
                    token, _ = dc.login_with_cache("example@1", None)
                self.assertIsNone(token)
                save.assert_not_called()
                self.assertNotIn("private-token", output.getvalue())
                self.assertNotIn("private-code", output.getvalue())

    def test_cached_account_without_session_does_not_inherit_previous_account(self):
        with patch.dict(dc.SESSION_ID_BY_SERVER, {"_current": "other-account-session"}, clear=True), patch.object(dc, "get_cached_token", return_value="cached-token"), patch.object(dc, "load_token_cache", return_value={"example@2": {"token": "cached-token"}}):
            token, _ = dc.login_with_cache("example@2", None)
            self.assertEqual(token, "cached-token")
            self.assertNotIn("DOSSENSESSIONID", dc.common_headers(token))

    def test_business_failure_exits_nonzero_after_notifying(self):
        result = {"success": False, "signMsg": "-", "points": "-", "error": "业务未成功"}
        with patch.object(dc, "SERVERS", ["example@1"]), patch.object(dc, "run_account", return_value=result), patch.object(dc, "send_pushplus") as notify, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                dc.main()
        self.assertEqual(error.exception.code, 1)
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
