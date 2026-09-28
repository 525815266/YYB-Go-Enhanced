# name: test_baihua_vote
import base64
import json
import time
import unittest
from urllib.parse import quote

import baihua_vote as vote


def fake_jwt(**overrides):
    claims = {
        "iss": "http://example.80ow.com/app/auth/123",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "sub": 123,
        "appid": vote.APPID,
        "guard": "app",
    }
    claims.update(overrides)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


class FakeResponse:
    def __init__(self, body, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, replies):
        self.headers = {}
        self.replies = list(replies)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.replies.pop(0))

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.replies.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.replies.pop(0)


class BaihuaVoteTests(unittest.TestCase):
    def test_yyb_refs_and_positional_tokens(self):
        token1, token2 = fake_jwt(sub=1), fake_jwt(sub=2)
        refs = vote.yyb_refs("yyb-go:8000@1\nhttp://yyb-go:8000@openid")
        self.assertEqual(refs, ["1", "openid"])
        self.assertEqual(vote.parse_token_config(token1 + "\n" + token2, refs), {"1": token1, "openid": token2})

    def test_encoded_cookie_token(self):
        token = fake_jwt()
        cookie = "app_access_token=" + quote(quote("Bearer " + token)) + "; path=/"
        self.assertEqual(vote.normalize_token(cookie), token)
        self.assertEqual(vote.token_base_url(token), "http://example.80ow.com")

    def test_captcha_blocks_vote(self):
        session = FakeSession([{"code": 0, "data": {"show_captcha": True}}])
        client = vote.VoteClient(fake_jwt(), session=session)
        self.assertTrue(client.captcha_required("stage", "player"))
        self.assertEqual(len(session.calls), 1)

    def test_vote_posts_once(self):
        session = FakeSession([{"code": 0, "msg": "ok"}])
        client = vote.VoteClient(fake_jwt(), session=session)
        self.assertEqual(client.vote("stage", "568451")["code"], 0)
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["json"], {"player_ids": ["568451"]})

    def test_discovers_dynamic_oauth_url(self):
        oauth = (
            "https://open.weixin.qq.com/connect/oauth2/authorize?"
            f"appid={vote.APPID}&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback&"
            "response_type=code&scope=snsapi_base&state=test#wechat_redirect"
        )
        session = FakeSession(
            [
                FakeResponse({}, 302, {"Location": "https://rotating.example/player"}),
                FakeResponse({}, 302, {"Location": oauth}),
            ]
        )
        self.assertEqual(vote.discover_oauth_url("http://entry", session=session), oauth)

    def test_yyb_probe_reports_unsupported_api(self):
        body = {"code": 0, "data": {"result": {"2": {"2": "invalid api_name"}}}}
        session = FakeSession([FakeResponse(body)])
        with self.assertRaisesRegex(RuntimeError, "不支持公众号"):
            vote.probe_yyb_oauth("http://yyb", "2", "https://oauth", session=session)


if __name__ == "__main__":
    unittest.main()
