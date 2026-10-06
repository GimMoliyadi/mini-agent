"""真实入口的离线回环与隐私测试，所有配置和状态均隔离。"""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import awareness_cli
import main as runtime
from test_awareness import ORIGINAL, client_for, completion, dialogue, organization, safety_result

DUMMY_KEY = "awareness-offline-fixture-key"
PRIVATE_MARKER = "SYNTHETIC_PRIVATE_PROVIDER_BODY"
CHILD_TIMEOUT_SECONDS = 30
SERVER_JOIN_SECONDS = 5
SYSTEM_ENV_NAMES = {"SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "TEMP", "TMP", "PATHEXT"}


class TerminalInput(io.TextIOWrapper):
    def __init__(self, text):
        super().__init__(io.BytesIO(text.encode("utf-8")), encoding="utf-8", errors="strict")

    def isatty(self):
        return True


def isolated_environment(root):
    environment = {name: value for name, value in os.environ.items() if name.upper() in SYSTEM_ENV_NAMES}
    environment.update({
        "OPENAI_API_KEY": DUMMY_KEY, "OPENAI_MODEL": "offline-model", "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "MINI_AGENT_STATE_DIR": str(root / "state"), "AGENT_WORKSPACE": str(root / "workspace"),
        "HOME": str(root), "USERPROFILE": str(root), "APPDATA": str(root / "appdata"),
        "LOCALAPPDATA": str(root / "local"), "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
        "MINI_AGENT_HTTP_PROXY": "", "NO_PROXY": "*", "TOOL_APPROVAL_MODE": "ALLOW", "CONTEXT_MODE": "WRITE_ONLY",
        "GIT_CEILING_DIRECTORIES": str(root),
    })
    return environment


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = patch.dict(os.environ, isolated_environment(self.root), clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_cli(self, client, *, text=ORIGINAL, args=None, config_error=None, interactive=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        config = SimpleNamespace(model="offline-model", api_key=DUMMY_KEY, base_url="http://127.0.0.1:9/v1")
        stdin = TerminalInput(text) if interactive else io.StringIO(text)
        with redirect_stdout(stdout), redirect_stderr(stderr), patch("sys.stdin", stdin):
            with patch("config.load_config", return_value=config, side_effect=config_error), patch.object(runtime, "build_client", return_value=client):
                with patch.object(runtime, "run_agent_loop", side_effect=AssertionError("execution forbidden")) as loop:
                    with patch.object(runtime, "run_task_with_session", side_effect=AssertionError("session forbidden")) as session_runner:
                        code = awareness_cli.main(["--json"] if args is None else args)
        loop.assert_not_called()
        session_runner.assert_not_called()
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "workspace").exists())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_pipe_success_is_single_use_and_private(self):
        client = client_for(completion(organization()))
        code, stdout, stderr = self.run_cli(client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["result"], organization())
        self.assertNotIn(ORIGINAL, stderr)
        client.close.assert_called_once_with()
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_terminal_text_stream_keeps_reading_after_natural_reply(self):
        invitation = dialogue("invite", question="愿意一起看看此刻感受吗？可以跳过。")
        guidance = dialogue("guide", kind="consent", quote="愿意", question="此刻有什么感受？可以跳过。")
        client = client_for(completion(invitation), completion(guidance))
        code, stdout, stderr = self.run_cli(
            client, text=ORIGINAL + "\n愿意\nexit\n", args=[], interactive=True,
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn(invitation["question"], stdout)
        self.assertIn(guidance["question"], stdout)
        self.assertEqual(stderr.count("请输入文字"), 3)
        self.assertNotIn(str(awareness_cli.AwarenessError("runtime")), stdout + stderr)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        second_request = client.chat.completions.create.call_args_list[1].kwargs
        self.assertIn("当前 guidance_state=pending", second_request["messages"][0]["content"])

    def test_terminal_correction_uses_only_current_run_memory(self):
        corrected = organization()
        corrected["feelings"] = [{"quote": "生气", "text": "生气。", "source": "user_explicit"}]
        client = client_for(completion(organization()), completion(corrected))
        correction = "不是失落，是生气。"
        code, stdout, stderr = self.run_cli(client, text=ORIGINAL + "\n" + correction + "\n退出\n", interactive=True)
        self.assertEqual(code, 0, stderr)
        payloads = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([payload["result"] for payload in payloads], [organization(), corrected])
        requests = client.chat.completions.create.call_args_list
        self.assertEqual(len(requests), 2)
        messages = requests[1].kwargs["messages"]
        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1]["content"].strip(), ORIGINAL)
        self.assertEqual(json.loads(messages[2]["content"]), organization())
        self.assertEqual(messages[3]["content"].strip(), correction)
        for request in requests:
            self.assertNotIn("tools", request.kwargs)
        self.assertNotIn(ORIGINAL, stderr)
        self.assertEqual(client.close.call_count, 2)

        fresh_result = organization()
        fresh_result["experiences"] = fresh_result["interpretations"] = []
        fresh_result["feelings"] = corrected["feelings"]
        fresh_client = client_for(completion(fresh_result))
        code, stdout, _ = self.run_cli(fresh_client, text=correction + "\nexit\n", interactive=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["result"], fresh_result)
        # 新运行不恢复上一运行的用户输入或整理结果。
        for request in fresh_client.chat.completions.create.call_args_list:
            self.assertNotIn(ORIGINAL, json.dumps(request.kwargs, ensure_ascii=False))

    def test_terminal_consent_topic_change_skip_and_end(self):
        turns = [
            (ORIGINAL, dialogue("invite", question="愿意一起看看此刻感受吗？可以跳过。")),
            ("愿意", dialogue("guide", kind="consent", quote="愿意", question="此刻有什么感受？可以跳过。")),
            ("有些紧张", dialogue()),
            ("跳过这个问题", dialogue(kind="skip", quote="跳过这个问题")),
            ("换个话题，我在担心考试", dialogue("invite", kind="topic_change", quote="换个话题", question="愿意看看此刻的感受吗？")),
            ("不愿意", dialogue(kind="decline", quote="不愿意")),
            ("今天就聊到这里吧", dialogue("end", kind="end", quote="今天就聊到这里吧")),
        ]
        client = client_for(*(completion(result) for _, result in turns))
        code, stdout, stderr = self.run_cli(client, text="\n".join(text for text, _ in turns) + "\n不要再读这行\n", interactive=True)
        self.assertEqual(code, 0, stderr)
        payloads = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([item["result"] for item in payloads], [result for _, result in turns])
        requests = client.chat.completions.create.call_args_list
        self.assertEqual(len(requests), len(turns))
        for request, state in zip(requests, ("off", "pending", "allowed", "declined", "off", "declined", "declined")):
            self.assertIn(f"当前 guidance_state={state}", request.kwargs["messages"][0]["content"])
            self.assertNotIn("tools", request.kwargs)
        self.assertNotIn(ORIGINAL, stderr)

    def test_terminal_ambiguous_answer_stays_pending_and_restart_resets(self):
        responses = [dialogue("invite", question="愿意吗？"), dialogue(), dialogue()]
        client = client_for(*(completion(result) for result in responses))
        code, _, _ = self.run_cli(client, text=ORIGINAL + "\n也许吧\n我还没想好\nexit\n", interactive=True)
        self.assertEqual(code, 0)
        for call in client.chat.completions.create.call_args_list[1:]:
            self.assertIn("当前 guidance_state=pending", call.kwargs["messages"][0]["content"])
        fresh = client_for(completion(dialogue()))
        self.run_cli(fresh, text="重新说一件事\nexit\n", interactive=True)
        self.assertIn("当前 guidance_state=off", fresh.chat.completions.create.call_args.kwargs["messages"][0]["content"])

    def test_revocation_and_failed_request_cannot_restore_permission(self):
        replies = [
            dialogue("invite", question="愿意吗？"),
            dialogue("guide", kind="consent", quote="愿意", question="此刻有什么感受？"),
            RuntimeError(PRIVATE_MARKER),
        ]
        client = client_for(*(completion(reply) if isinstance(reply, dict) else reply for reply in replies))
        code, stdout, stderr = self.run_cli(client, text=ORIGINAL + "\n愿意\n不要继续引导\n", interactive=True)
        self.assertEqual(code, 1)
        self.assertIn("当前 guidance_state=declined", client.chat.completions.create.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(json.loads(stdout.splitlines()[-1])["error"]["code"], "runtime")
        self.assertNotIn(PRIVATE_MARKER, stdout + stderr)

    def test_terminal_stop_and_eof_do_not_request_model(self):
        for text in ("exit\n", "退出\n", "结束对话\n", ""):
            client = client_for()
            with self.subTest(text=text):
                code, stdout, _ = self.run_cli(client, text=text, interactive=True)
                self.assertEqual(code, 0)
                self.assertEqual(stdout, "")
                client.chat.completions.create.assert_not_called()
                client.close.assert_not_called()

    def test_stopping_guidance_can_continue_ordinary_conversation(self):
        turns = [
            (ORIGINAL, dialogue("invite", question="愿意吗？")),
            ("愿意", dialogue("guide", kind="consent", quote="愿意", question="此刻有什么感受？可以跳过。")),
            ("我不想继续了", dialogue(kind="revoke", quote="我不想继续了")),
            ("我还是想说说这件事", dialogue()),
        ]
        client = client_for(*(completion(result) for _, result in turns))
        code, stdout, stderr = self.run_cli(client, text="\n".join(text for text, _ in turns) + "\nexit\n", interactive=True)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(stdout.splitlines()), 4)
        self.assertIn("当前 guidance_state=declined", client.chat.completions.create.call_args.kwargs["messages"][0]["content"])

    def test_failed_semantic_revocation_repair_stays_declined(self):
        text = "这一部分先到这里吧"
        invalid = dialogue("guide", kind="revoke", quote=text, question="FORBIDDEN_QUESTION？")
        client = client_for(
            completion(dialogue("invite", question="愿意吗？")),
            completion(dialogue("guide", kind="consent", quote="愿意", question="此刻有什么感受？")),
            completion(invalid), completion(dialogue()), completion(dialogue()),
        )
        code, stdout, stderr = self.run_cli(client, text=ORIGINAL + "\n愿意\n" + text + "\n继续普通对话\nexit\n", interactive=True)
        self.assertEqual(code, 0, stderr)
        payloads = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([payload["status"] for payload in payloads], ["completed", "completed", "failed", "completed"])
        self.assertEqual(payloads[2]["error"]["code"], "validation")
        self.assertNotIn(invalid["question"], stdout + stderr)
        self.assertIn("当前 guidance_state=declined", client.chat.completions.create.call_args.kwargs["messages"][0]["content"])

    def test_growing_conversation_budget_stops_before_next_request(self):
        client = client_for(completion(organization()), completion(organization()))
        import awareness
        initial = [{"role": "system", "content": awareness.SYSTEM_PROMPT + "\n当前 guidance_state=off。"},
                   {"role": "user", "content": ORIGINAL + "\n"}]
        limit = len(json.dumps(initial, ensure_ascii=False, separators=(",", ":")))
        with patch.dict(os.environ, {"MINI_AGENT_MAX_CONTEXT_CHARS": str(limit)}):
            code, stdout, stderr = self.run_cli(client, text=(ORIGINAL + "\n") * 2 + "\nexit\n", interactive=True)
        self.assertEqual(code, 1, stderr)
        payloads = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([payload["status"] for payload in payloads], ["completed", "failed"])
        self.assertEqual(payloads[1]["error"]["code"], "budget")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_terminal_failure_does_not_add_invalid_reply_to_history(self):
        call = SimpleNamespace(id="blocked", function=SimpleNamespace(name="write_file", arguments="{}"))
        client = client_for(completion(organization()), completion(None, tool_calls=[call]), completion(organization()))
        code, stdout, stderr = self.run_cli(client, text=(ORIGINAL + "\n") * 3 + "exit\n", interactive=True)
        self.assertEqual(code, 0, stderr)
        payloads = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([payload["status"] for payload in payloads], ["completed", "failed", "completed"])
        self.assertEqual(payloads[1]["error"]["code"], "tools")
        messages = client.chat.completions.create.call_args_list[2].kwargs["messages"]
        self.assertEqual(len(messages), 4)
        self.assertNotIn("write_file", json.dumps(messages))

    def test_terminal_oversized_line_is_not_reused_as_next_message(self):
        client = client_for()
        text = "字" * (awareness_cli.MAX_INPUT_CHARS * 2) + "\nexit\n"
        code, stdout, _ = self.run_cli(client, text=text, interactive=True)
        self.assertEqual(code, 0)
        payloads = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["error"]["code"], "input")
        client.chat.completions.create.assert_not_called()

    def test_provider_eof_is_failure_not_normal_input_end(self):
        client = client_for(EOFError(PRIVATE_MARKER))
        code, stdout, stderr = self.run_cli(client, text=ORIGINAL + "\nexit\n", interactive=True)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["error"]["code"], "runtime")
        self.assertNotIn(PRIVATE_MARKER, stdout + stderr)
        client.close.assert_called_once_with()

    def test_cleanup_interrupt_reports_cancellation(self):
        client = client_for(completion(organization()))
        client.close.side_effect = KeyboardInterrupt()
        code, stdout, stderr = self.run_cli(client)
        self.assertEqual(code, 130, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["result"])
        client.close.assert_called_once_with()

    def test_request_interrupt_closes_client(self):
        client = client_for(KeyboardInterrupt())
        code, stdout, _ = self.run_cli(client)
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(stdout)["status"], "cancelled")
        client.close.assert_called_once_with()

    def test_cleanup_error_does_not_leak_or_override_primary_error(self):
        for replies, expected_code in (((completion(organization()),), 0), ((RuntimeError(PRIVATE_MARKER),), 1)):
            client = client_for(*replies)
            client.close.side_effect = RuntimeError(PRIVATE_MARKER)
            with self.subTest(expected_code=expected_code):
                code, stdout, stderr = self.run_cli(client)
                self.assertEqual(code, expected_code)
                self.assertNotIn(PRIVATE_MARKER, stdout + stderr)
                client.close.assert_called_once_with()

    def test_configuration_and_argument_errors_are_static(self):
        client = client_for(completion(organization()))
        code, stdout, stderr = self.run_cli(client, config_error=SystemExit(PRIVATE_MARKER))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["error"]["code"], "configuration")
        self.assertNotIn(PRIVATE_MARKER, stdout + stderr)
        client.chat.completions.create.assert_not_called()
        code, stdout, stderr = self.run_cli(client, args=["--json", "--task", PRIVATE_MARKER])
        self.assertEqual(code, 2)
        self.assertNotIn(PRIVATE_MARKER, stdout + stderr)
        self.assertEqual(json.loads(stdout)["error"]["code"], "arguments")

    def test_help_never_reads_input_or_initializes_runtime(self):
        with patch("sys.stdin") as stdin, patch.object(awareness_cli, "execute") as execute:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                awareness_cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        stdin.read.assert_not_called()
        stdin.readline.assert_not_called()
        execute.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_logging_and_environment_are_restored_after_failure(self):
        previous = logging.root.manager.disable
        output = io.StringIO()
        logger = logging.getLogger("awareness-test-private")
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        os.environ["OPENAI_LOG"] = "debug"
        try:
            with self.assertRaises(RuntimeError):
                with awareness_cli.private_provider_logging():
                    logger.critical(PRIVATE_MARKER)
                    self.assertNotIn("OPENAI_LOG", os.environ)
                    raise RuntimeError("synthetic failure")
            self.assertEqual(logging.root.manager.disable, previous)
            self.assertEqual(os.environ["OPENAI_LOG"], "debug")
            self.assertEqual(output.getvalue(), "")
        finally:
            logging.disable(previous)


def wire_reply(content, *, finish="stop", usage=True, **fields):
    message = {"role": "assistant", "content": json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content}
    message.update(fields)
    result = {"id": "offline-awareness", "object": "chat.completion", "created": 0, "model": "offline-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    return 200, result


@contextmanager
def local_provider(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")))
            index = len(requests) - 1
            code, body = responses[index] if index < len(responses) else (500, {"error": {"message": "unexpected retry"}})
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
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


class PublicAwarenessTests(unittest.TestCase):
    def run_case(self, responses, *, text=ORIGINAL, args=None, debug=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = isolated_environment(root)
            if debug:
                environment["OPENAI_LOG"] = "debug"
            with local_provider(responses) as (endpoint, requests):
                environment["OPENAI_BASE_URL"] = endpoint
                process = subprocess.run(
                    [sys.executable, str(ROOT / "launcher.py"), "awareness", *(["--json"] if args is None else args)],
                    input=text, cwd=root, env=environment, capture_output=True, text=True,
                    encoding="utf-8", timeout=CHILD_TIMEOUT_SECONDS, check=False,
                )
            self.assertFalse((root / "state").exists())
            self.assertFalse((root / "workspace").exists())
            self.assertNotIn(DUMMY_KEY, process.stdout + process.stderr)
            for request in requests:
                self.assertTrue({"tools", "tool_choice", "functions", "function_call"}.isdisjoint(request))
            return process, requests

    def test_real_sdk_json_and_human_output_without_utf8_environment(self):
        for args in (["--json"], []):
            with self.subTest(args=args):
                process, requests = self.run_case([wire_reply(organization())], args=args, debug=True)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["messages"][1]["content"], ORIGINAL)
                self.assertNotIn(ORIGINAL, process.stderr)
                self.assertNotIn("我很失落", process.stderr)
                if args:
                    self.assertEqual(json.loads(process.stdout)["result"], organization())
                else:
                    self.assertIn(organization()["reflection"], process.stdout)
                    self.assertNotIn("发生了什么", process.stdout)
                    self.assertNotIn("原文：", process.stdout)

    def test_real_sdk_format_repair(self):
        process, requests = self.run_case([wire_reply(PRIVATE_MARKER), wire_reply(organization())], debug=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(requests), 2)
        self.assertNotIn(PRIVATE_MARKER, process.stdout + process.stderr)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(requests[1], ensure_ascii=False))
        self.assertEqual(json.loads(process.stdout)["result"], organization())

    def test_real_sdk_tools_refusal_truncation_and_server_error_stop(self):
        tool = {"id": "forbidden-write", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}
        cases = [
            (wire_reply(None, finish="tool_calls", tool_calls=[tool]), "tools"),
            (wire_reply(None, finish="function_call", function_call={"name": "write_file", "arguments": "{}"}), "tools"),
            (wire_reply(None, refusal=PRIVATE_MARKER), "refusal"),
            (wire_reply("partial", finish="length"), "finish"),
            ((500, {"error": {"message": PRIVATE_MARKER, "type": "server_error"}}), "network"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                process, requests = self.run_case([response], debug=True)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(len(requests), 1)
                self.assertEqual(json.loads(process.stdout)["error"]["code"], code)
                self.assertNotIn(PRIVATE_MARKER, process.stdout + process.stderr)

    def test_safety_mode_includes_fixed_support_and_never_downgrades(self):
        process, requests = self.run_case([wire_reply(safety_result())], args=[])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("当地紧急服务", process.stdout)
        self.assertEqual(len(requests), 1)
        raw = json.dumps(safety_result(), ensure_ascii=False).replace('"source": "user_report"', '"source": "user_report", "source": "user_report"')
        process, requests = self.run_case([wire_reply(raw), wire_reply(organization())])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(process.stdout)["error"]["code"], "safety_validation")

    def test_malformed_safety_reply_stops_at_public_entry(self):
        raw = '{"mode":"safety_support","risk":'
        process, requests = self.run_case([wire_reply(raw), wire_reply(organization())], debug=True)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(process.stdout)["error"]["code"], "safety_validation")
        self.assertNotIn(raw, process.stdout + process.stderr)

    def test_help_and_invalid_input_never_contact_provider(self):
        process, requests = self.run_case([], args=["--help"])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(requests, [])
        for text in ("", "password=synthetic-value"):
            with self.subTest(text=text):
                process, requests = self.run_case([], text=text)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(requests, [])
                self.assertNotIn("synthetic-value", process.stdout + process.stderr)

    def test_missing_usage_blocks_repair(self):
        process, requests = self.run_case([wire_reply("invalid", usage=False), wire_reply(organization())])
        self.assertEqual(process.returncode, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(json.loads(process.stdout)["error"]["code"], "budget")


if __name__ == "__main__":
    unittest.main()
