import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "yyb-scriptctl.sh"


def shell_path(path):
    return Path(path).as_posix()


def find_bash():
    found = shutil.which("bash")
    if found:
        return found
    windows_git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if windows_git_bash.exists():
        return str(windows_git_bash)
    return None


class FakeQingLongHandler(http.server.BaseHTTPRequestHandler):
    script_body = b""
    crons = []
    api_writes = []
    fail_script = False

    def log_message(self, _format, *_args):
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/scripts/"):
            if self.fail_script:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.script_body)))
            self.end_headers()
            self.wfile.write(self.script_body)
            return
        if self.path.startswith("/open/crons"):
            self.send_json({"code": 200, "data": {"data": self.crons}})
            return
        self.send_error(404)

    def read_payload(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self):
        payload = self.read_payload()
        payload["id"] = "cron-1"
        self.crons.append(payload)
        self.api_writes.append(("POST", payload))
        self.send_json({"code": 200, "data": payload})

    def do_PUT(self):
        payload = self.read_payload()
        self.crons[:] = [payload]
        self.api_writes.append(("PUT", payload))
        self.send_json({"code": 200, "data": payload})


@unittest.skipUnless(find_bash(), "bash is required")
class ScriptControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.destination = self.temp_path / "downloaded"
        self.ql_root = self.temp_path / "ql"
        (self.ql_root / "shell").mkdir(parents=True)

        FakeQingLongHandler.script_body = (
            "#!/usr/bin/env python3\n"
            "# name: test-task\n"
            "# cron: 7 6 * * *\n"
            "print('ok')\n"
        ).encode("utf-8")
        FakeQingLongHandler.crons = []
        FakeQingLongHandler.api_writes = []
        FakeQingLongHandler.fail_script = False
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeQingLongHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        port = self.server.server_port
        (self.ql_root / "shell" / "share.sh").write_text(
            f"load_ql_envs() {{ ql_port={port}; }}\n", encoding="utf-8"
        )
        (self.ql_root / "shell" / "api.sh").write_text(
            "get_token() { __ql_token__=test-token; }\n", encoding="utf-8"
        )
        self.env = os.environ.copy()
        self.env.update(
            {
                "YYB_RAW_BASE": f"http://127.0.0.1:{port}/scripts",
                "YYB_SCRIPT_DIR": shell_path(self.destination),
                "YYB_TASK_PREFIX": "repo/scripts",
                "QL_DIR": shell_path(self.ql_root),
            }
        )

    def run_tool(self, *args):
        return subprocess.run(
            [find_bash(), shell_path(TOOL), *args],
            env=self.env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def test_pull_downloads_without_creating_task(self):
        result = self.run_tool("pull", "测试.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.destination / "测试.py").is_file())
        self.assertEqual(FakeQingLongHandler.api_writes, [])

    def test_sync_copies_scripts_without_creating_task(self):
        source = self.temp_path / "source-repo"
        (source / "scripts").mkdir(parents=True)
        (source / "scripts" / "repo.py").write_text("print('repo')\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "scripts/repo.py"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "fixture"], check=True, capture_output=True)
        self.env["YYB_REPO_URL"] = shell_path(source)
        self.env["YYB_REPO_BRANCH"] = "main"

        result = self.run_tool("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / "repo.py").read_text(encoding="utf-8"), "print('repo')\n")
        self.assertEqual(FakeQingLongHandler.api_writes, [])

    def test_install_creates_then_updates_same_command(self):
        first = self.run_tool("install", "test.py")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_tool("install", "test.py", "--name", "new-task")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([method for method, _ in FakeQingLongHandler.api_writes], ["POST", "PUT"])
        self.assertEqual(len(FakeQingLongHandler.crons), 1)
        self.assertIsNone(FakeQingLongHandler.api_writes[0][1]["sub_id"])
        self.assertEqual(FakeQingLongHandler.crons[0]["name"], "new-task")
        self.assertEqual(FakeQingLongHandler.crons[0]["command"], "task repo/scripts/test.py")

    def test_rejects_path_traversal(self):
        result = self.run_tool("pull", "../bad.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不能包含路径", result.stderr)

    def test_failed_download_does_not_overwrite_existing_file(self):
        self.destination.mkdir(parents=True)
        target = self.destination / "测试.py"
        target.write_text("old-content", encoding="utf-8")
        FakeQingLongHandler.fail_script = True
        result = self.run_tool("pull", "测试.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "old-content")

    def test_missing_cron_downloads_but_does_not_create_task(self):
        FakeQingLongHandler.script_body = b"# name: no schedule\nprint('ok')\n"
        result = self.run_tool("install", "missing.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.destination / "missing.py").is_file())
        self.assertIn("--cron", result.stderr)
        self.assertEqual(FakeQingLongHandler.api_writes, [])


class ScriptMetadataTests(unittest.TestCase):
    def test_all_subscribable_scripts_have_anchored_name_metadata(self):
        missing = []
        for pattern in ("*.py", "*.js"):
            for path in sorted((ROOT / "scripts").glob(pattern)):
                head = path.read_text(encoding="utf-8-sig").splitlines()[:40]
                comment = "//" if path.suffix == ".js" else "#"
                if not any(line.lstrip().startswith(f"{comment} name:") for line in head):
                    missing.append(path.name)
        self.assertEqual(missing, [], f"missing name metadata: {missing}")


if __name__ == "__main__":
    unittest.main()
