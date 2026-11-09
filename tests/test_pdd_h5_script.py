import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit


path = Path(__file__).parents[1] / "scripts" / "拼多多果园公众号版_code版.py"
spec = importlib.util.spec_from_file_location("pdd_h5_script", path)
pdd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdd)


class PDDH5RequestTests(unittest.TestCase):
    def result(self, **changes):
        result = {"code": "synthetic-h5", "state": pdd.H5_STATE, "redirect_uri": pdd.H5_REDIRECT_URI}
        result.update(changes)
        return {"code": 0, "data": {"result": result}}

    def test_yyb_uses_one_h5_request_with_matching_callback(self):
        reply = Mock(status_code=200)
        reply.json.return_value = self.result()
        with patch.object(pdd.requests, "post", return_value=reply) as request:
            self.assertEqual(pdd.get_code("http://127.0.0.1:8000@9"), "synthetic-h5")
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args, ("http://127.0.0.1:8000/wx/h5code",))
        self.assertEqual(kwargs["json"], {
            "ref": "9", "app_id": "wx839691cce7c102bb",
            "payload": {"redirect_uri": pdd.H5_REDIRECT_URI, "scope": "snsapi_base", "state": pdd.H5_STATE},
        })

    def test_errors_and_mismatches_never_fall_back_to_mini_program(self):
        for status, body in (
            (502, {"msg": "synthetic-h5-failure"}),
            (200, self.result(code=200)),
            (200, self.result(state="different")),
            (200, self.result(redirect_uri="https://other.example/login")),
        ):
            with self.subTest(status=status, body=body):
                reply = Mock(status_code=status)
                reply.json.return_value = body
                with patch.object(pdd.requests, "post", return_value=reply) as request:
                    with self.assertRaises(RuntimeError):
                        pdd.get_code("http://127.0.0.1:8000@9")
                request.assert_called_once()
                self.assertTrue(request.call_args.args[0].endswith("/wx/h5code"))

    def test_login_consumes_code_at_the_same_callback(self):
        import requests
        response = Mock()
        response.cookies = requests.cookies.cookiejar_from_dict({"PDDAccessToken": "synthetic-access", "pdd_user_id": "synthetic-user"})
        account = pdd.AccountSession("http://127.0.0.1:8000@9")
        with patch("builtins.print"), patch.object(pdd, "get_code", return_value="synthetic-code"), patch.object(pdd, "request_with_proxy", return_value=response) as request, patch.object(pdd, "load_token_cache", return_value={}), patch.object(pdd, "save_token_cache"):
            account.login(None)
        query = parse_qs(urlsplit(request.call_args.args[1]).query)
        expected = parse_qs(urlsplit(pdd.H5_REDIRECT_URI).query)
        self.assertEqual(query.pop("code"), ["synthetic-code"])
        self.assertEqual(query.pop("state"), [pdd.H5_STATE])
        self.assertEqual(query, expected)


if __name__ == "__main__":
    unittest.main()
