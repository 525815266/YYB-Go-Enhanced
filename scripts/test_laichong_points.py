#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: test_laichong_points

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import laichong_points as script


class LaichongPointsTest(unittest.TestCase):
    def test_video_and_share_progress_and_claim(self):
        client = Mock()
        client.get_wx_code.return_value = "wx-code"
        client.points.side_effect = [2, 16]
        client.sign_info.return_value = {"id": 1, "current_progress": 1, "is_signed": True}
        client.tasks.side_effect = [
            [
                {"id": 2, "task_type": 5, "current_progress": 0, "target_progress": 2, "unclaimed_points": 0},
                {"id": 3, "task_type": 8, "current_progress": 0, "target_progress": 2, "unclaimed_points": 0},
            ],
            [
                {"id": 2, "task_type": 5, "current_progress": 2, "target_progress": 2, "unclaimed_points": 0},
                {"id": 3, "task_type": 8, "current_progress": 2, "target_progress": 2, "unclaimed_points": 0},
            ],
        ]
        client.complete.side_effect = [
            {"current_progress": 1, "unclaimed_points": 0},
            {"current_progress": 2, "unclaimed_points": 0},
            {"current_progress": 1, "unclaimed_points": 2},
            {"current_progress": 2, "unclaimed_points": 2},
        ]
        account = script.Account(1, "http://yyb-go:8000", "1")
        with patch.object(script, "Client", return_value=client), \
             patch.object(script, "mark_ready"), \
             patch.object(script.time, "sleep"):
            result = script.run_account(account)

        self.assertTrue(result["success"])
        self.assertEqual(result["video"], "2/2")
        self.assertEqual(result["share"], "2/2")
        self.assertEqual(result["claimed"], ["8+2", "8+2"])
        self.assertEqual(result["actions"], ["今日已签到", "5接口提交2次，进度2/2", "8接口提交2次，进度2/2"])
        self.assertEqual([call.args[0] for call in client.complete.call_args_list], [2, 2, 3, 3])
        self.assertEqual([call.args[0] for call in client.claim.call_args_list], [3, 3])

    def test_stops_when_server_progress_does_not_increase(self):
        client = Mock()
        client.complete.return_value = {"current_progress": 1, "unclaimed_points": 0}
        actions = []
        script.submit_task_progress(
            client,
            {"id": 2, "task_type": 5, "current_progress": 1, "target_progress": 5},
            [],
            actions,
        )
        client.complete.assert_called_once_with(2)
        self.assertEqual(actions, ["5服务端进度未增加(1/5)，停止提交"])

    def test_video_failure_does_not_skip_share(self):
        client = Mock()
        client.get_wx_code.return_value = "wx-code"
        client.points.side_effect = [7, 9]
        client.sign_info.return_value = {"id": 1, "current_progress": 1, "is_signed": True}
        client.tasks.side_effect = [
            [
                {"id": 2, "task_type": 5, "current_progress": 0, "target_progress": 1},
                {"id": 3, "task_type": 8, "current_progress": 0, "target_progress": 1},
            ],
            [],
        ]
        client.complete.side_effect = [script.ScriptError("视频受限"), {"current_progress": 1, "unclaimed_points": 2}]
        account = script.Account(1, "http://yyb-go:8000", "1")
        with patch.object(script, "Client", return_value=client), \
             patch.object(script, "mark_ready"), \
             patch.object(script.time, "sleep"):
            result = script.run_account(account)

        self.assertTrue(result["success"])
        self.assertEqual(result["actions"], ["今日已签到", "5接口失败：视频受限", "8接口提交1次，进度1/1"])
        self.assertEqual(result["share"], "1/1")
        self.assertEqual(result["claimed"], ["8+2"])
        client.claim.assert_called_once_with(3)

    def test_streak_progress_does_not_mean_signed_today(self):
        client = Mock()
        client.get_wx_code.return_value = "wx-code"
        client.points.side_effect = [12, 14]
        client.sign_info.side_effect = [
            {"id": 1, "current_progress": 3, "is_signed": False},
            {"id": 1, "current_progress": 4, "is_signed": True},
        ]
        client.tasks.return_value = []
        account = script.Account(1, "http://yyb-go:8000", "1")
        with patch.object(script, "Client", return_value=client), \
             patch.object(script, "mark_ready"), \
             patch.object(script.time, "sleep"):
            result = script.run_account(account)

        client.complete.assert_called_once_with(1)
        self.assertEqual(result["actions"], ["签到完成"])

    def test_sign_is_not_reported_done_without_server_confirmation(self):
        client = Mock()
        client.get_wx_code.return_value = "wx-code"
        client.points.side_effect = [12, 12]
        client.sign_info.side_effect = [
            {"id": 1, "current_progress": 3, "is_signed": False},
            {"id": 1, "current_progress": 3, "is_signed": False},
        ]
        client.tasks.return_value = []
        account = script.Account(1, "http://yyb-go:8000", "1")
        with patch.object(script, "Client", return_value=client), \
             patch.object(script, "mark_ready"), \
             patch.object(script.time, "sleep"):
            result = script.run_account(account)

        client.complete.assert_called_once_with(1)
        self.assertEqual(result["actions"], ["签到提交后未确认"])

    def test_main_prints_each_account_before_starting_next(self):
        accounts = [script.Account(1, "http://yyb-go:8000", "1"), script.Account(2, "http://yyb-go:8000", "2")]
        events = []

        def run(account):
            events.append(f"run:{account.ref}")
            return {"success": True, "label": account.label, "before": 1, "after": 1,
                    "delta": 0, "actions": [], "claimed": [], "video": "5/5", "share": "3/3"}

        with patch.object(script, "parse_accounts", return_value=accounts), \
             patch.object(script, "load_remarks"), \
             patch.object(script, "filter_accounts", side_effect=lambda values, *_args, **_kwargs: values), \
             patch.object(script, "run_account", side_effect=run), \
             patch.object(script, "log", side_effect=events.append), \
             patch.object(script, "notify"):
            script.main()

        self.assertLess(events.index("账号1(1)：积分 1 -> 1（+0）；操作=无；已领取=无；视频=5/5；分享=3/3"), events.index("run:2"))

    def test_log_flushes_immediately(self):
        with patch("builtins.print") as output:
            script.log("进度")
        output.assert_called_once_with("进度", flush=True)


if __name__ == "__main__":
    unittest.main()
