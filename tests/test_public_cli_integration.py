"""通过真实 SDK 和回环 HTTP 服务验证公开 CLI，不访问任何模型服务。"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 30
MAX_REQUEST_BYTES = 1024 * 1024
SERVER_JOIN_SECONDS = 5
DUMMY_KEY = "offline-loopback-test-key"


@contextmanager
def local_provider(mode="write"):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST_BYTES:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            requests.append(body)
            code = 500 if mode == "error" else 200
            if mode == "error":
                payload = {"error": {"message": "offline provider failure", "type": "server_error", "code": "offline"}}
            else:
                has_result = any(item.get("role") == "tool" for item in body["messages"])
                message = {"role": "assistant", "content": "已根据工具结果回答。"}
                reason = "length" if mode == "truncated" else "stop"
                if mode == "write" and not has_result:
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "write-note", "type": "function", "function": {
                            "name": "write_file", "arguments": json.dumps({"path": "note.txt", "content": "离线 SDK 闭环"}, ensure_ascii=False),
                        },
                    }]}
                    reason = "tool_calls"
                payload = {"id": "offline-response", "object": "chat.completion", "created": 0,
                           "model": "offline-provider", "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=SERVER_JOIN_SECONDS)


class PublicCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()

    def run_cli(self, endpoint, approval="ALLOW"):
        environment = os.environ.copy()
        environment.update({
            "OPENAI_API_KEY": DUMMY_KEY, "OPENAI_BASE_URL": endpoint,
            "OPENAI_MODEL": "offline-provider", "AGENT_WORKSPACE": str(self.workspace),
            "MINI_AGENT_STATE_DIR": str(self.state), "TOOL_APPROVAL_MODE": approval,
            "CONTEXT_MODE": "WRITE_ONLY", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
            "NO_PROXY": "*", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "GIT_CEILING_DIRECTORIES": str(self.root),
        })
        return subprocess.run(
            [sys.executable, str(ROOT / "cli.py"), "--task", "创建 note.txt 并汇报实际结果", "--workspace", str(self.workspace)],
            cwd=self.root, env=environment, input="", capture_output=True, text=True,
            encoding="utf-8", timeout=CHILD_TIMEOUT_SECONDS, check=False,
        )

    def test_sdk_tool_round_trip_persists_session_and_undo(self):
        with local_provider() as (endpoint, requests):
            process = self.run_cli(endpoint)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["verification_status"], "not_configured")
        self.assertEqual((self.workspace / "note.txt").read_text(encoding="utf-8"), "离线 SDK 闭环")
        self.assertEqual(len(requests), 2)
        self.assertTrue(any(item.get("role") == "tool" for item in requests[1]["messages"]))
        saved = json.loads((self.state / "sessions" / f"{result['session_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["workspace"], str(self.workspace.resolve()))
        self.assertTrue((self.state / "journals" / f"{result['run_id']}.json").is_file())
        self.assertNotIn(DUMMY_KEY, process.stdout + process.stderr)

    def test_deny_round_trip_never_creates_requested_file(self):
        with local_provider() as (endpoint, requests):
            process = self.run_cli(endpoint, approval="DENY")
        result = json.loads(process.stdout)
        self.assertFalse((self.workspace / "note.txt").exists())
        self.assertEqual(len(requests), 2)
        self.assertTrue(any(event.get("approval") == "DENY" for event in result["trace"]["events"]))
        self.assertIsNone(result["acceptance"])

    def test_truncated_wire_response_is_not_success(self):
        with local_provider("truncated") as (endpoint, requests):
            process = self.run_cli(endpoint)
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(requests), 1)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_provider_error_has_one_request_and_valid_json(self):
        with local_provider("error") as (endpoint, requests):
            process = self.run_cli(endpoint)
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(requests), 1)
        self.assertIsNone(result["trace"]["total_tokens"])
        self.assertFalse(result["trace"]["usage_complete"])

    def test_session_management_does_not_require_original_workspace(self):
        missing = self.root / "deleted-project"
        environment = os.environ.copy()
        environment.update({
            "MINI_AGENT_STATE_DIR": str(self.state), "AGENT_WORKSPACE": str(missing),
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        })
        process = subprocess.run(
            [sys.executable, str(ROOT / "launcher.py"), "sessions", "list"],
            cwd=self.root, env=environment, capture_output=True, text=True,
            encoding="utf-8", timeout=CHILD_TIMEOUT_SECONDS, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"sessions": []})
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
