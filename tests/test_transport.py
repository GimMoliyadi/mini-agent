"""Loopback OpenAI-compatible transport tests for the LangGraph runtime."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable

import pytest

from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


MAX_REQUEST_BYTES = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


Responder = Callable[["LoopbackProvider", dict[str, Any]], tuple[int, dict[str, Any]]]


def _completion(model: str, message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    return {
        "id": "loopback-response",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def _tool_response(
    model: str, name: str, arguments: dict[str, Any], call_id: str
) -> dict[str, Any]:
    return _completion(
        model,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        },
        "tool_calls",
    )


class LoopbackProvider:
    """A deterministic OpenAI chat-completions endpoint used only by tests."""

    def __init__(self, responder: Responder):
        self.responder = responder
        self.requests: list[dict[str, Any]] = []
        self.emitted_tools: list[tuple[str, dict[str, Any]]] = []

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_REQUEST_BYTES:
                        self.send_error(413)
                        return
                    body = json.loads(self.rfile.read(size).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise ValueError("request body must be an object")
                    owner.requests.append(body)
                    status, payload = owner.responder(owner, body)
                    encoded = json.dumps(payload).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (ValueError, json.JSONDecodeError):
                    self.send_error(400, "invalid JSON")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextmanager
def loopback_provider(responder: Responder):
    provider = LoopbackProvider(responder)
    try:
        yield provider
    finally:
        provider.close()


def _settings(tmp_path: Path, provider: LoopbackProvider, *, approval: str = "ALLOW") -> Settings:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    return Settings(
        workspace=workspace,
        state_dir=state,
        api_key="loopback-test-key",
        base_url=provider.base_url,
        model="loopback-model",
        approval_mode=approval,
        max_model_calls=20,
        context_tokens=16_000,
        request_timeout=5,
    )


def _multistep_responder(provider: LoopbackProvider, body: dict[str, Any]):
    model = body["model"]
    tool_count = sum(message.get("role") == "tool" for message in body["messages"])
    steps = [
        ("list_files", {"path": "."}),
        ("search_text", {"query": "needle", "path": ".", "max_results": 20}),
        ("read_file", {"path": "target.txt", "start_line": 1, "max_lines": 10}),
        ("apply_patch", {"path": "target.txt", "old_text": "old", "new_text": "new"}),
        ("read_file", {"path": "target.txt", "start_line": 1, "max_lines": 10}),
    ]
    if tool_count < len(steps):
        name, arguments = steps[tool_count]
        provider.emitted_tools.append((name, arguments))
        return 200, _tool_response(model, name, arguments, f"call-{tool_count}")
    return 200, _completion(model, {"role": "assistant", "content": "任务已完成"}, "stop")


def _cli_responder(provider: LoopbackProvider, body: dict[str, Any]):
    model = body["model"]
    tool_count = sum(message.get("role") == "tool" for message in body["messages"])
    if tool_count == 0:
        arguments = {"path": "note.txt", "content": "loopback"}
        provider.emitted_tools.append(("write_file", arguments))
        return 200, _tool_response(model, "write_file", arguments, "call-write")
    return 200, _completion(model, {"role": "assistant", "content": "写入完成"}, "stop")


def _resume_after_error_responder(provider: LoopbackProvider, body: dict[str, Any]):
    model = body["model"]
    if len(provider.requests) == 1:
        arguments = {"path": "."}
        provider.emitted_tools.append(("list_files", arguments))
        return 200, _tool_response(model, "list_files", arguments, "call-list")
    if len(provider.requests) == 2:
        return 500, {"error": {
            "message": "synthetic provider failure",
            "type": "server_error",
            "code": "synthetic",
        }}
    return 200, _completion(model, {"role": "assistant", "content": "恢复完成"}, "stop")


def _subprocess_command(arguments: list[str]) -> list[str]:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt" and venv_python.is_file():
        command_line = "call " + subprocess.list2cmdline([str(ROOT / "mini.cmd"), *arguments])
        return ["cmd", "/d", "/c", command_line]
    return [sys.executable, "-m", "mini_agent.launcher", *arguments]


def _subprocess_environment(settings: Settings) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "OPENAI_API_KEY": settings.api_key,
        "OPENAI_BASE_URL": settings.base_url,
        "OPENAI_MODEL": settings.model,
        "MINI_AGENT_STATE_DIR": str(settings.state_dir),
        "AGENT_WORKSPACE": str(settings.workspace),
        "TOOL_APPROVAL_MODE": settings.approval_mode,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
    })
    return environment


def _disable_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("LANGCHAIN_OPENAI_TCP_KEEPALIVE", "0")


def test_agent_application_http_multistep_file_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_proxy(monkeypatch)
    with loopback_provider(_multistep_responder) as provider:
        settings = _settings(tmp_path, provider)
        (settings.workspace / "target.txt").write_text("needle old\n", encoding="utf-8")

        with AgentApplication(settings) as app:
            result = app.run("find needle in target.txt and replace old with new")

        assert result["status"] == "completed", json.dumps(result, ensure_ascii=False)
        assert result["answer"] == "任务已完成"
        assert (settings.workspace / "target.txt").read_text(encoding="utf-8") == "needle new\n"
        assert provider.emitted_tools == [
            ("list_files", {"path": "."}),
            ("search_text", {"query": "needle", "path": ".", "max_results": 20}),
            ("read_file", {"path": "target.txt", "start_line": 1, "max_lines": 10}),
            ("apply_patch", {"path": "target.txt", "old_text": "old", "new_text": "new"}),
            ("read_file", {"path": "target.txt", "start_line": 1, "max_lines": 10}),
        ]
        tool_definitions = {
            item["function"]["name"]
            for item in provider.requests[0]["tools"]
            if item["type"] == "function"
        }
        assert {"list_files", "search_text", "read_file", "apply_patch"} <= tool_definitions
        patch_schema = next(
            item["function"] for item in provider.requests[0]["tools"]
            if item["function"]["name"] == "apply_patch"
        )
        assert set(patch_schema["parameters"]["required"]) == {"path", "old_text", "new_text"}


def test_http_error_leaves_resumable_sqlite_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_proxy(monkeypatch)
    with loopback_provider(_resume_after_error_responder) as provider:
        settings = _settings(tmp_path, provider)
        thread_id = "resume-after-http-error"
        with AgentApplication(settings) as app:
            failed = app.run("list the workspace", thread_id=thread_id)

        assert failed["status"] == "failed", failed
        assert failed["session_id"] == thread_id
        assert failed["pending_nodes"]
        assert "synthetic provider failure" in failed["error"]
        assert (settings.state_dir / ".mini-agent" / "checkpoints" / "v2.sqlite3").is_file()

        with AgentApplication(settings) as app:
            resumed = app.run(None, thread_id=thread_id)

        assert resumed["status"] == "completed", resumed
        assert resumed["answer"] == "恢复完成"
        assert len(provider.requests) == 3


def test_mini_cmd_task_json_subprocess_uses_loopback_provider(tmp_path: Path) -> None:
    with loopback_provider(_cli_responder) as provider:
        settings = _settings(tmp_path, provider)
        process = subprocess.run(
            _subprocess_command([
                "task",
                "--task",
                "create-note",
                "--workspace",
                str(settings.workspace),
            ]),
            cwd=ROOT,
            env=_subprocess_environment(settings),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["status"] == "completed", result
    assert (settings.workspace / "note.txt").read_text(encoding="utf-8") == "loopback"
    assert provider.emitted_tools == [("write_file", {"path": "note.txt", "content": "loopback"})]
    assert provider.requests and provider.requests[0]["model"] == "loopback-model"
    assert "loopback-test-key" not in process.stdout + process.stderr
