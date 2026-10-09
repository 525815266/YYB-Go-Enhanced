import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests


spec = importlib.util.spec_from_file_location(
    "jd_cookie", Path(__file__).parents[1] / "scripts" / "京东CK采集_code版.py"
)
jd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jd)


def response_for(body, cookies=None, status=200, raw_cookie=""):
    response = Mock(status_code=status)
    response.json.return_value = body
    response.cookies = requests.cookies.cookiejar_from_dict(cookies or {})
    response.raw.headers.getlist.return_value = [raw_cookie] if raw_cookie else []
    return response


class JDCookieTests(unittest.TestCase):
    def login(self, response):
        output = io.StringIO()
        with patch.object(jd, "request_with_proxy", return_value=response), contextlib.redirect_stdout(output):
            result = jd.login_by_code("http://yyb.test@1", "fake-code", None)
        return result, output.getvalue()

    def test_skey_only_is_not_cached_or_classified_as_unbound(self):
        response = response_for(
            {"retcode": 0, "info": {"pinStatus": 0, "skey": "fake-skey-secret"}},
            {"skey": "fake-skey-secret", "pt_pin": "fake-pin-secret"},
        )
        output = io.StringIO()
        with patch.object(jd, "request_with_proxy", return_value=response), patch.object(
            jd, "get_cached_token", return_value=None
        ), patch.object(jd, "get_code", return_value="fake-code"), patch.object(
            jd, "set_cached_token"
        ) as save, contextlib.redirect_stdout(output):
            token, detail = jd.login_with_cache("http://yyb.test@1", None)
        self.assertIsNone(token)
        save.assert_not_called()
        text = output.getvalue() + str(detail)
        self.assertIn("仅返回 skey", text)
        self.assertIn("pt_key=无", text)
        self.assertNotIn("未绑定", text)
        self.assertNotIn("fake-skey-secret", text)
        self.assertNotIn("fake-pin-secret", text)

    def test_complete_cookie_still_succeeds(self):
        for raw in (False, True):
            with self.subTest(raw_header=raw):
                cookies = {"pt_key": "fake-key-secret", "pt_pin": "fake-pin-secret"}
                response = response_for(
                    {"retcode": 0}, cookies=None if raw else cookies,
                    raw_cookie="pt_key=fake-key-secret; Path=/, pt_pin=fake-pin-secret; Path=/" if raw else "",
                )
                (token, _), output = self.login(response)
                self.assertEqual(token, "pt_key=fake-key-secret;pt_pin=fake-pin-secret;")
                self.assertNotIn("fake-key-secret", output)
                self.assertNotIn("fake-pin-secret", output)

    def test_skey_in_json_or_raw_header_is_recognized_without_exposing_it(self):
        responses = [
            response_for({"retcode": 0, "info": {"skey": "fake-secret"}}),
            response_for({"retcode": 0, "skey": "fake-secret"}),
            response_for({"retcode": 0}, raw_cookie="skey=fake-secret; Path=/"),
        ]
        for response in responses:
            (token, detail), output = self.login(response)
            self.assertIsNone(token)
            self.assertIn("仅返回 skey", detail["blocked"])
            self.assertNotIn("fake-secret", output + str(detail))

    def test_http_error_does_not_accept_even_complete_cookies(self):
        response = response_for(
            {"error": "fake-body-secret"},
            {"pt_key": "fake-key-secret", "pt_pin": "fake-pin-secret"}, status=503,
        )
        (token, detail), output = self.login(response)
        self.assertIsNone(token)
        self.assertIn("HTTP 503", detail["blocked"])
        self.assertNotIn("fake-", output + str(detail))

    def test_missing_cookie_diagnostics_do_not_expose_body(self):
        bodies = [
            {"retcode": 21, "retMsg": "get apppwd failed", "info": {"pin": "fake-pin-secret"}},
            {"retcode": "fake-secret", "token": "fake-secret", "info": {"pinStatus": 0}},
        ]
        for body in bodies:
            with self.subTest(body=body):
                (token, detail), output = self.login(response_for(body))
                self.assertIsNone(token)
                self.assertIn("不能", detail["blocked"])
                self.assertNotIn("该微信未绑定", output)
                self.assertNotIn("fake-", output + str(detail))

    def test_exception_does_not_expose_code_url(self):
        output = io.StringIO()
        with patch.object(jd, "request_with_proxy", side_effect=ValueError("?code=fake-secret")), contextlib.redirect_stdout(output):
            token, detail = jd.login_by_code("http://yyb.test@1", "fake-code", None)
        self.assertIsNone(token)
        self.assertIn("ValueError", detail["blocked"])
        self.assertNotIn("fake-secret", output.getvalue() + str(detail))


if __name__ == "__main__":
    unittest.main()
