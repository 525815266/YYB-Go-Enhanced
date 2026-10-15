import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import findstars_web_points as fs


def entry(token, *, host="fsapi.ihezu.com", encoded=False, old_header=""):
    body = json.dumps({"code": 200, "status": "success", "data": {"token": token}})
    content = {"text": base64.b64encode(body.encode()).decode() if encoded else body}
    if encoded:
        content["encoding"] = "base64"
    return {"request": {"url": f"https://{host}/api/login/_login", "headers": [
        {"name": "FS-Token", "value": old_header}]}, "response": {"content": content}}


class FindStarsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache.json"
        env = patch.dict(os.environ, {"FINDSTARS_TOKEN_CACHE": str(self.cache)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def har(self, entries):
        path = Path(self.temp.name) / "session.har"
        path.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8-sig")
        return path

    def record(self, token="cached", user_id="1"):
        return {"token": token, "user_id": user_id, "name": "Test"}

    def importer(self, users):
        def factory(token):
            client = MagicMock()
            if isinstance(users[token], Exception):
                client.user.side_effect = users[token]
            else:
                client.user.return_value = users[token]
            return client
        return patch.object(fs, "FindStarsClient", side_effect=factory)

    def test_har_accepts_base64_and_rejects_foreign_hosts(self):
        path = self.har([entry("foreign", host="not-findstars.example"),
                         entry("old"), entry("new", encoded=True, old_header="old")])
        self.assertEqual(fs.extract_tokens_from_har(path), ["new", "old"])

    def test_har_can_use_request_header_without_login_response(self):
        path = self.har([{"request": {"url": "https://fsapi.ihezu.com/api/user/show",
                                     "headers": [{"name": "fs-token", "value": "session"}]}}])
        self.assertEqual(fs.extract_tokens_from_har(path), ["session"])

    def test_import_validates_latest_session_and_does_not_claim(self):
        path = self.har([entry("old"), entry("new")])
        with self.importer({"old": fs.SessionExpired("expired"),
                            "new": {"id": 1, "name": "Test", "coins": 10}}) as factory:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fs.import_har(path, "网页账号"), 1)
            self.assertEqual(fs._read_cache(self.cache)["网页账号"]["token"], "new")
            for call in factory.mock_calls:
                self.assertNotIn("receive", str(call))
        if os.name != "nt":
            self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_expired_import_preserves_existing_file(self):
        fs._save_cache(self.cache, {"A": self.record()})
        original = self.cache.read_bytes()
        with self.importer({"expired": fs.SessionExpired("expired")}):
            with self.assertRaisesRegex(RuntimeError, "仍有效"):
                fs.import_har(self.har([entry("expired")]), "A")
        self.assertEqual(self.cache.read_bytes(), original)

    def test_different_user_cannot_replace_existing_label(self):
        fs._save_cache(self.cache, {"A": self.record(user_id="1")})
        original = self.cache.read_bytes()
        with self.importer({"other": {"id": 2, "name": "Other", "coins": 5}}):
            with self.assertRaisesRegex(RuntimeError, "其他用户"):
                fs.import_har(self.har([entry("other")]), "A")
        self.assertEqual(self.cache.read_bytes(), original)

    def test_multi_user_har_cannot_be_assigned_single_label(self):
        users = {"one": {"id": 1}, "two": {"id": 2}}
        with self.importer(users):
            with self.assertRaisesRegex(RuntimeError, "多个账号"):
                fs.import_har(self.har([entry("one"), entry("two")]), "A")
        self.assertFalse(self.cache.exists())

    def test_same_nickname_does_not_merge_different_users(self):
        users = {"one": {"id": 1, "name": "Same"}, "two": {"id": 2, "name": "Same"}}
        with self.importer(users), contextlib.redirect_stdout(io.StringIO()):
            fs.import_har(self.har([entry("one"), entry("two")]))
        self.assertEqual({r["user_id"] for r in fs._read_cache(self.cache).values()}, {"1", "2"})

    def test_cache_replaces_old_env_and_preserves_other_accounts(self):
        fs._save_cache(self.cache, {"A": self.record("new"), "C": self.record("three", "3")})
        os.environ["FINDSTARS_TOKENS"] = "A#expired&B|two"
        self.assertEqual(fs._load_tokens(), [("A", "new"), ("B", "two"), ("C", "three")])

    def test_wrong_api_cache_is_not_silently_used_or_overwritten(self):
        self.cache.write_text('{"api":"https://other.example","accounts":{}}')
        with self.assertRaisesRegex(RuntimeError, "不匹配"):
            fs._load_tokens()

    def test_progress_and_total_limit_match_website(self):
        self.assertTrue(fs._claimable({"progress": 1, "times": 1, "totalTimes": 0, "totalProgress": 42}))
        self.assertFalse(fs._claimable({"status": "complete", "progress": 0, "times": 1}))
        self.assertFalse(fs._claimable({"status": "complete", "progress": 1, "times": 1,
                                        "totalTimes": 2, "totalProgress": 2}))

    def test_expired_http_response_has_actionable_error_without_token(self):
        client = fs.FindStarsClient("secret-value")
        self.addCleanup(client.session.close)
        response = MagicMock(status_code=401)
        response.json.return_value = {"code": 400011, "data": {"token": "secret-value"}}
        with patch.object(client.session, "post", return_value=response):
            with self.assertRaises(fs.SessionExpired) as caught:
                client.user()
        self.assertIn("--import-har", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))

    def client(self, *, tasks=None):
        client = MagicMock()
        client.user.side_effect = [{"id": 1, "name": "Test", "coins": 100},
                                   {"id": 1, "name": "Test", "coins": 109}]
        client.tasks.side_effect = [tasks or [{"id": 4, "name": "奖励", "progress": 1, "times": 1}],
                                    [{"id": 4, "name": "奖励", "progress": 0, "times": 1}]]
        return client

    def test_claim_updates_points_and_checks_task_state(self):
        client = self.client()
        self.assertIn("变化 +9", fs._run_client("A", client))
        client.receive.assert_called_once_with(4)
        self.assertEqual(client.tasks.call_count, 2)

    def test_dry_run_never_claims(self):
        client = self.client()
        os.environ["FINDSTARS_DRY_RUN"] = "1"
        self.assertIn("dry-run", fs._run_client("A", client))
        client.receive.assert_not_called()

    def test_timeout_is_not_replayed_or_reported_as_success(self):
        client = self.client()
        client.receive.side_effect = requests.ReadTimeout()
        with self.assertRaisesRegex(RuntimeError, "未自动重试"):
            fs._run_client("A", client)
        client.receive.assert_called_once_with(4)

    def test_success_response_with_unchanged_task_is_reported(self):
        client = self.client()
        ready = [{"id": 4, "name": "奖励", "progress": 1, "times": 1}]
        client.tasks.side_effect = [ready, ready]
        with self.assertRaisesRegex(RuntimeError, "状态未变化"):
            fs._run_client("A", client)
        client.receive.assert_called_once_with(4)

    def test_main_returns_failure_for_expired_accounts(self):
        with patch.object(fs, "_load_tokens", return_value=[("A", "expired")]), \
                patch.object(fs, "run_one", side_effect=fs.SessionExpired("expired")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(fs.main([]), 1)


if __name__ == "__main__":
    unittest.main()
