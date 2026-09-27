import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent import Agent, OFFICIAL_IMAGE

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        compose = Path(self.tmp.name) / 'compose.yaml'
        compose.touch()
        self.agent = Agent(compose, 'yyb-go')

    def test_accepts_only_fixed_actions_and_serializes(self):
        self.assertEqual(self.agent.submit({'action': 'shell'})[0], 400)
        body = {'action': 'restart', 'request_id': 'a' * 32}
        with patch('agent.threading.Thread'):
            self.assertEqual(self.agent.submit(body)[0], 202)
            self.assertEqual(self.agent.submit(body)[0], 202)
            self.assertEqual(self.agent.submit(dict(body, request_id='b' * 32))[0], 409)

    def run_update(self, actual='0.2.10', health_fails=False, image=OFFICIAL_IMAGE + ':latest'):
        before = {'Image': 'old-image', 'Config': {'Healthcheck': {'Test': ['CMD', 'check']}}, 'State': {'Health': {'Status': 'healthy'}}}
        pulled = {'Id': 'new-image', 'Config': {'Labels': {'org.opencontainers.image.version': actual}}}
        with patch.object(self.agent, 'compose_command', return_value=json.dumps({'services': {'yyb-go': {'image': image}}})), patch.object(self.agent, 'container', return_value=before), patch.object(self.agent, 'command', return_value=json.dumps([pulled])), patch.object(self.agent, 'apply_image') as apply, patch.object(self.agent, 'wait_healthy', side_effect=[RuntimeError('unhealthy'), None] if health_fails else None):
            self.agent.execute('update', '0.2.10')
            return [call.args[0] for call in apply.call_args_list]

    def test_verified_update(self):
        self.assertEqual(self.run_update(), ['new-image'])
        self.assertIn('更新至 v0.2.10 完成', self.agent.snapshot()['job']['message'])

    def test_old_or_unpublished_image_never_stops_service(self):
        self.assertEqual(self.run_update(actual='0.2.9'), [])

    def test_health_failure_rolls_back(self):
        self.assertEqual(self.run_update(health_fails=True), ['new-image', 'old-image'])
        self.assertIn('已恢复旧镜像', self.agent.snapshot()['job']['message'])

    def test_refuses_custom_image(self):
        self.assertEqual(self.run_update(image='local/test:latest'), [])

if __name__ == '__main__':
    unittest.main()
