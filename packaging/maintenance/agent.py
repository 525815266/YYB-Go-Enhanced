#!/usr/bin/env python3
"""Opt-in Docker Compose maintenance agent. Unix socket only, no shell endpoint."""
import argparse
import json
import os
import re
import socketserver
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

OFFICIAL_IMAGE = 'ghcr.io/525815266/yyb-go-enhanced'
SEMVER = re.compile(r'[0-9]{1,4}.[0-9]{1,4}.[0-9]{1,4}')


class Agent:
    def __init__(self, compose, service):
        self.compose = str(Path(compose).resolve(strict=True))
        self.service = service
        self.lock = threading.Lock()
        self.job = {'running': False, 'message': '尚未执行维护操作。'}

    def command(self, *args, timeout=120):
        # Never invoke a shell or accept command text from the browser.
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                cwd=str(Path(self.compose).parent))
        if result.returncode:
            # Docker output may contain credentials from environment/config.
            raise RuntimeError(f'{args[0]} 操作失败（退出码 {result.returncode}），请在宿主机检查 Docker 状态。')
        return result.stdout.strip()

    def compose_command(self, *args, override=None, timeout=120):
        cmd = ['docker', 'compose', '-f', self.compose]
        if override:
            cmd += ['-f', override]
        return self.command(*cmd, *args, timeout=timeout)

    def container(self):
        ids = self.compose_command('ps', '-a', '-q', self.service).splitlines()
        if len(ids) != 1:
            raise RuntimeError('维护目标必须是一个已存在的 Compose 容器，未修改服务。')
        return json.loads(self.command('docker', 'inspect', ids[0]))[0]

    def snapshot(self):
        with self.lock:
            return {'mode': 'docker-compose', 'service': self.service, 'job': dict(self.job)}

    def message(self, text):
        with self.lock:
            self.job['message'] = text

    def submit(self, body):
        if not isinstance(body, dict) or set(body) - {'action', 'request_id', 'expected_version'}:
            return 400, {'message': '无效操作'}
        action, request_id = body.get('action'), body.get('request_id', '')
        if action not in ('update', 'restart') or not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', request_id):
            return 400, {'message': '无效操作'}
        expected = body.get('expected_version', '')
        if action == 'update' and (not isinstance(expected, str) or not SEMVER.fullmatch(expected)):
            return 400, {'message': '目标版本无效'}
        with self.lock:
            if self.job.get('request_id') == request_id:
                return 202, dict(self.job)
            if self.job['running']:
                return 409, {'message': '已有维护任务正在执行，请勿重复提交'}
            self.job = {'running': True, 'action': action, 'request_id': request_id,
                        'started_at': int(time.time()), 'message': '正在检查部署配置…'}
        threading.Thread(target=self.execute, args=(action, expected), daemon=True).start()
        return 202, self.snapshot()['job']

    def apply_image(self, image):
        # Pin this recreation to the verified image ID. Do not mutate compose.yml.
        # An explicit -f list preserves the original project directory/name.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', encoding='utf-8') as override:
            json.dump({'services': {self.service: {'image': image, 'pull_policy': 'never'}}}, override)
            override.flush()
            self.compose_command('up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                                 self.service, override=override.name, timeout=150)

    def wait_healthy(self, expected_image, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            container = self.container()
            state = container.get('State', {})
            if container.get('Image') != expected_image:
                raise RuntimeError('运行镜像与目标不一致')
            if state.get('Running') and state.get('Health', {}).get('Status') == 'healthy':
                return
            time.sleep(3)
        raise RuntimeError('服务健康检查超时')

    def execute(self, action, expected):
        changed = False
        old_image = None
        try:
            config = json.loads(self.compose_command('config', '--format', 'json'))
            service = config.get('services', {}).get(self.service, {})
            image = service.get('image', '')
            before = self.container()
            if not before.get('Config', {}).get('Healthcheck', {}).get('Test') or before.get('State', {}).get('Health', {}).get('Status') != 'healthy':
                raise RuntimeError('原服务未通过健康检查，未修改服务。请先检查 healthcheck。')
            old_image = before['Image']
            if action == 'restart':
                self.message('正在重启 YYB 服务…')
                self.compose_command('restart', self.service)
                self.wait_healthy(old_image)
                self.message('重启完成，服务健康检查通过。')
                return
            if image not in (OFFICIAL_IMAGE + ':latest', OFFICIAL_IMAGE + ':main'):
                raise RuntimeError('在线更新仅支持官方 latest/main 镜像。请先调整 Compose 镜像配置；未修改服务。')
            self.message('正在下载官方镜像，原服务继续运行…')
            self.command('docker', 'pull', image, timeout=300)
            pulled = json.loads(self.command('docker', 'image', 'inspect', image))[0]
            actual = pulled.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.version', '')
            if actual != expected:
                raise RuntimeError('目标版本镜像尚未发布或版本不匹配，原服务未停止，请等待构建完成后再试。')
            if pulled['Id'] == old_image:
                self.message('当前已运行相同镜像，无需更新。')
                return
            self.message('镜像验证通过，正在切换容器并检查健康状态…')
            changed = True
            self.apply_image(pulled['Id'])
            self.wait_healthy(pulled['Id'])
            self.message(f'更新至 v{actual} 完成，服务健康检查通过。')
        except Exception as exc:
            # Error text is generated locally, never include inspect/env output.
            reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            if changed and old_image:
                try:
                    self.message('更新未通过检查，正在恢复旧镜像…')
                    self.apply_image(old_image)
                    self.wait_healthy(old_image)
                    reason += '；已恢复旧镜像。数据库未回滚，请核对数据兼容性。'
                except Exception:
                    reason += '；自动恢复失败，请立即在宿主机检查服务。旧镜像仍保留。'
            self.message('操作失败：' + reason)
        finally:
            with self.lock:
                self.job['running'] = False
                self.job['finished_at'] = int(time.time())


def handler_for(agent):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_json(200, agent.snapshot()) if self.path == '/status' else self.send_json(404, {'message': 'not found'})

        def do_POST(self):
            if self.path != '/jobs':
                return self.send_json(404, {'message': 'not found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                status, data = agent.submit(body)
            except (ValueError, TypeError):
                status, data = 400, {'message': 'invalid request'}
            self.send_json(status, data)
    return Handler


def main():
    import fcntl  # Host agent is Linux-only; tests can run on Windows.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', required=True, help='Absolute Compose file; include all deployment settings in this file')
    parser.add_argument('--service', default='yyb-go')
    parser.add_argument('--socket', required=True)
    parser.add_argument('--gid', required=True, type=int, help='Group allowed to connect; match the YYB container group')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', args.service):
        parser.error('invalid service name')
    agent = Agent(args.compose, args.service)
    path = Path(args.socket).absolute()
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(path.parent, 0, args.gid)
    os.chmod(path.parent, 0o750)
    # Lock persists for the lifetime of this process; only its stale socket can
    # be removed. Never unlink another running agent's control endpoint.
    lock = open(path.with_suffix('.lock'), 'a')
    os.chmod(path.with_suffix('.lock'), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if path.exists():
        if not path.is_socket():
            raise RuntimeError('socket path is occupied by a non-socket file')
        path.unlink()
    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address
    server = Server(str(path), handler_for(agent))
    os.chown(path, 0, args.gid)
    os.chmod(path, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
