#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["YYB_ACCOUNT_STATUS_FILE"] = str(Path(self.tmp.name) / "status.json")
        os.environ.pop("YYB_GUARD_BYPASS", None)
        from yyb_account_guard import set_status
        set_status("1", "ready", "")

    def tearDown(self):
        os.environ.pop("YYB_ACCOUNT_STATUS_FILE", None)
        self.tmp.cleanup()

    def test_classification(self):
        from yyb_account_guard import classify_error
        self.assertEqual(classify_error("该微信账号尚未注册龙湖会员，请先授权手机号")[0], "unregistered")
        self.assertEqual(classify_error("手机号未授权")[0], "unbound")
        self.assertEqual(classify_error("活动太火爆了")[0], "temporary_error")
        self.assertIsNone(classify_error("登录成功"))

    def test_cooldown_and_bypass(self):
        from yyb_account_guard import set_status, should_skip
        set_status("1", "unbound", "手机号未授权")
        self.assertTrue(should_skip("yyb-go:8000@1")[0])
        os.environ["YYB_GUARD_BYPASS"] = "1"
        self.assertFalse(should_skip("1")[0])

    def test_expired(self):
        import yyb_account_guard as guard
        guard.set_status("1", "unbound", "旧状态")
        payload = guard._read()
        payload["accounts"]["1"]["checked_at"] = int(time.time()) - guard.TTL_SECONDS["unbound"] - 1
        guard._write(payload)
        self.assertFalse(guard.should_skip("1")[0])

    def test_prune_keeps_app_scoped_status(self):
        import yyb_account_guard as guard
        guard.set_status("1", "unbound", "手机号未授权", app_id="wx-demo")
        result = guard.prune(refs=["1"])
        self.assertEqual(result["removed"], [])
        self.assertTrue(guard.should_skip("1", app_id="wx-demo")[0])


if __name__ == "__main__":
    unittest.main()
