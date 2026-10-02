import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import main
import session
import tools
from acceptance import CodingTaskContract, TaskState
from config import LLMConfig
from openai.types.chat import ChatCompletionMessage
from recovery import Recovery
from runtime_guards import RunBudget, budget_context


TEST_SUCCESS = "Exit code: 0\nTimed out: false\nOutput limit exceeded: false\nSTDOUT:\n\nSTDERR:\nRan 1 test in 0.001s\n\nOK\n"


def tool_call(identifier, name, arguments):
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def response(content=None, calls=None, finish_reason="stop", usage=True):
    message = ChatCompletionMessage(role="assistant", content=content, tool_calls=calls)
    counts = SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12) if usage else None
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=counts,
    )


class RuntimeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "state" / "sessions"
        self.stack.enter_context(patch.dict(os.environ, {
            "AGENT_WORKSPACE": str(self.workspace),
            "MINI_AGENT_STATE_DIR": str(self.sessions.parent),
            "OPENAI_API_KEY": "offline-runtime-test-credential",
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "OPENAI_MODEL": "offline-mock",
            "TOOL_APPROVAL_MODE": "ALLOW",
            "CONTEXT_MODE": "WRITE_ONLY",
        }))
        self.stack.enter_context(patch.object(config, "WORKSPACE_DIR", self.workspace))
        self.stack.enter_context(patch.object(main, "WORKSPACE_DIR", self.workspace))
        self.stack.enter_context(patch.object(tools, "WORKSPACE_DIR", self.workspace))
        self.stack.enter_context(patch.object(session, "SESSIONS_DIR", self.sessions))
        configuration = LLMConfig("offline-runtime-test-credential", "http://127.0.0.1:9/v1", "offline-mock")
        self.stack.enter_context(patch.object(main, "load_config", return_value=configuration))
        self.client = Mock()
        self.stack.enter_context(patch.object(main, "build_client", return_value=self.client))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def contract(self):
        return CodingTaskContract.from_dict({
            "task_id": "offline-repair",
            "instruction": "修改 a.txt 后执行固定测试。",
            "allowed_paths": ["a.txt"],
            "test_command": {"command": "python", "args": ["-m", "unittest", "-q"], "cwd": "."},
        })

    def test_invalid_tool_json_is_saved_and_can_be_loaded(self):
        self.client.chat.completions.create.side_effect = [
            response(calls=[tool_call("broken", "write_file", "{")], finish_reason="tool_calls"),
            response("参数不完整，未写入文件。"),
        ]
        result = main.run_task_with_session("检查坏工具参数的恢复流程。")
        self.assertEqual(result["status"], "completed", result)
        saved = session.load_session(result["session_id"], workspace=self.workspace)
        calls = [entry for entry in saved["messages"] if entry.get("tool_calls")]
        self.assertEqual(calls[0]["tool_calls"][0]["function"]["arguments"], "{")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_contract_cancellation_never_starts_verifier(self):
        self.client.chat.completions.create.side_effect = KeyboardInterrupt
        with patch.object(main, "verify_contract") as verify:
            result = main.run_task_with_session(None, contract=self.contract())
        self.assertEqual(result["status"], "cancelled", result)
        self.assertEqual(result["task_state"]["status"], "CANCELLED")
        self.assertTrue(result["budget"]["cancelled"])
        verify.assert_not_called()
        self.client.close.assert_called_once()
        saved = session.load_session(result["session_id"], workspace=self.workspace)
        self.assertEqual(saved["run_status"], "cancelled")

    def test_denied_verifier_does_not_execute_command(self):
        execute = main.verification_runner(main.always_deny)
        with patch.object(main, "execute_tool_call") as handler:
            with self.assertRaises(PermissionError):
                execute("python", ["-m", "unittest", "-q"], ".", workspace=self.workspace)
        handler.assert_not_called()

    def test_truncated_response_is_not_completed(self):
        self.client.chat.completions.create.return_value = response("半截回答", finish_reason="length")
        result = main.run_task_with_session("回答一个问题。")
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["answer"], "半截回答")
        self.assertEqual(self.client.chat.completions.create.call_count, 1)

    def test_missing_usage_is_unknown_in_trace(self):
        self.client.chat.completions.create.return_value = response("回答。", usage=False)
        result = main.run_task_with_session("只回答。")
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["trace"]["total_tokens"])
        self.assertEqual(result["trace"]["unknown_usage_calls"], 1)
        self.assertFalse(result["trace"]["usage_complete"])

    def test_empty_choices_produces_structured_failure(self):
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[], usage=None)
        result = main.run_task_with_session("测试协议错误。")
        self.assertEqual(result["status"], "failed")
        self.assertIn("ProviderProtocolError", result["error"])
        self.assertEqual(result["trace"]["executed_tool_calls"], 0)

    def test_outbound_redaction_does_not_change_canonical_history(self):
        original = [{"role": "user", "content": "key = offline-runtime-test-credential"}]
        self.client.chat.completions.create.return_value = response("回答。")
        with budget_context(RunBudget()):
            main.ask(self.client, "offline-mock", original)
        submitted = self.client.chat.completions.create.call_args.kwargs["messages"]
        self.assertNotIn("offline-runtime-test-credential", json.dumps(submitted))
        self.assertIn("offline-runtime-test-credential", original[0]["content"])

    def test_interrupted_tool_batch_remains_paired_and_persistable(self):
        (self.workspace / "a.txt").write_text("old", encoding="utf-8")
        calls = [tool_call("read", "read_file", {"path": "a.txt"}), tool_call("write", "write_file", {"path": "a.txt", "content": "new"})]
        message = response(calls=calls, finish_reason="tool_calls").choices[0].message
        messages = [{"role": "system", "content": "offline"}, {"role": "user", "content": "read and write"}]
        def unavailable(*args):
            raise main.ApprovalUnavailableError("no terminal")
        with self.assertRaises(main.ApprovalUnavailableError):
            main.run_tool_round(messages, message, set(), unavailable)
        results = [item for item in messages if item["role"] == "tool"]
        self.assertEqual([item["tool_call_id"] for item in results], ["read", "write"])
        record = session.create_session("offline", messages, "WRITE_ONLY", workspace=self.workspace)
        session.save_session(record)
        session.load_session(record["session_id"], workspace=self.workspace)
        self.assertEqual((self.workspace / "a.txt").read_text(), "old")

    def test_full_mode_can_read_again_with_identical_arguments(self):
        target = self.workspace / "a.txt"
        target.write_text("before", encoding="utf-8")
        messages = [{"role": "user", "content": "read"}]
        executed = set()
        with patch.dict(os.environ, {"CONTEXT_MODE": "FULL"}):
            for identifier, content in (("first", "before"), ("second", "after")):
                target.write_text(content, encoding="utf-8")
                message = response(calls=[tool_call(identifier, "read_file", {"path": "a.txt"})], finish_reason="tool_calls").choices[0].message
                main.run_tool_round(messages, message, executed, main.always_allow)
                self.assertIn(content, messages[-1]["content"])

    def test_recovery_rejects_third_mutation_before_execution(self):
        target = self.workspace / "a.txt"
        target.write_text("original", encoding="utf-8")
        contract = self.contract()
        state = TaskState(initial_snapshot=main.snapshot_workspace(self.workspace))
        recovery = Recovery()
        calls = [tool_call(str(index), "write_file", {"path": "a.txt", "content": str(index)}) for index in range(1, 4)]
        calls += [tool_call("test", "run_command", {"command": "python", "args": ["-m", "unittest", "-q"]}), tool_call("finish", "finish_task", {"summary": "完成固定任务"})]
        message = response(calls=calls, finish_reason="tool_calls").choices[0].message
        original = tools.TOOL_REGISTRY["run_command"]
        fake = replace(original, handler=lambda **kwargs: TEST_SUCCESS)
        with patch.dict(tools.TOOL_REGISTRY, {"run_command": fake}):
            main.run_tool_round(
                [], message, set(), main.always_allow,
                required_test=("python", ("-m", "unittest", "-q"), "."),
                contract=contract, task_state=state, recovery=recovery,
            )
        self.assertEqual(target.read_text(), "2")
        self.assertEqual(recovery.repairs, 2)
        self.assertEqual(recovery.verifications, 1)
        self.assertEqual(recovery.finishes, 1)
        self.assertEqual(state.status.value, "FINISHED")

    def test_failed_request_usage_is_not_reported_as_zero(self):
        self.client.chat.completions.create.side_effect = ConnectionError("offline failure")
        result = main.run_task_with_session("请求一次。")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["trace"]["model_request_attempts"], 1)
        self.assertIsNone(result["trace"]["total_tokens"])
        self.assertFalse(result["trace"]["usage_complete"])

    def test_model_refusal_is_not_a_completed_task(self):
        reply = response()
        reply.choices[0].message.refusal = "模型明确拒绝该请求。"
        self.client.chat.completions.create.return_value = reply
        result = main.run_task_with_session("请求模型拒绝。")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["answer"], "模型明确拒绝该请求。")
        self.assertEqual(result["trace"]["executed_tool_calls"], 0)

    def test_plain_conversation_does_not_scan_workspace(self):
        self.client.chat.completions.create.return_value = response("你好。")
        with patch.object(main, "snapshot_workspace", side_effect=AssertionError("not needed")):
            result = main.run_task_with_session("你好。")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["verification_status"], "not_configured")

    def test_tool_call_finish_reason_without_calls_is_not_success(self):
        self.client.chat.completions.create.return_value = response("pending", finish_reason="tool_calls")
        result = main.run_task_with_session("验证协议组合。")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.client.chat.completions.create.call_count, 1)
        self.assertEqual(result["trace"]["executed_tool_calls"], 0)

    def test_interactive_session_binds_effective_workspace_before_first_task(self):
        other = self.root / "other"
        other.mkdir()
        with patch.object(main, "WORKSPACE_DIR", other), patch("builtins.input", return_value="exit"):
            self.assertEqual(main.main([]), 0)
        identifiers = session.list_sessions()
        self.assertEqual(len(identifiers), 1)
        saved = session.load_session(identifiers[0])
        self.assertEqual(saved["workspace"], str(self.workspace.resolve()))
        self.client.chat.completions.create.assert_not_called()

    def test_interactive_entry_rejects_state_inside_workspace_before_saving(self):
        nested = self.workspace / "sessions"
        with patch.object(session, "SESSIONS_DIR", nested), patch("builtins.input") as input_prompt:
            self.assertEqual(main.main([]), 2)
        self.assertFalse(nested.exists())
        input_prompt.assert_not_called()

    def test_environment_file_accepts_utf8_bom_without_echoing_values(self):
        target = self.root / "example.env"
        target.write_text("BOM_TEST_OPTION=value\ninvalid-private-line", encoding="utf-8-sig")
        with patch.object(config, "ENV_FILE", target), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOM_TEST_OPTION", None)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                config.load_env_file()
            self.assertEqual(os.environ["BOM_TEST_OPTION"], "value")
            self.assertNotIn("invalid-private-line", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
