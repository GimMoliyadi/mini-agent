"""Phase 10 tests for tool permission checks and side-effect approval."""

import json
import inspect
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import session  # noqa: E402
import tools  # noqa: E402


def fake_message(call_id: str, tool_name: str, arguments: dict):
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    return SimpleNamespace(content=None, tool_calls=[call])


class ApprovalRecorder:
    def __init__(self, decision: bool):
        self.decision = decision
        self.requests = []

    def __call__(self, tool_name: str, arguments: dict, operation: str) -> bool:
        self.requests.append((tool_name, arguments, operation))
        return self.decision


def run_round(messages, executed, call_id, tool_name, arguments, approval):
    main.run_tool_round(
        messages,
        fake_message(call_id, tool_name, arguments),
        executed,
        approval,
    )
    return messages[-1]["content"]


def model_reply(message):
    return main.ModelReply(message, "stop", 0, 0, 0)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_workspace = tools.WORKSPACE_DIR
        tools.WORKSPACE_DIR = Path(self.temp_dir.name)

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_workspace
        self.temp_dir.cleanup()

    def test_read_only_tools_do_not_request_approval(self):
        (tools.WORKSPACE_DIR / "note.txt").write_text("hello", encoding="utf-8")
        recorder = ApprovalRecorder(False)
        messages = []
        executed = set()

        read_result = run_round(
            messages, executed, "read", "read_file", {"path": "note.txt"}, recorder
        )
        list_result = run_round(
            messages, executed, "list", "list_files", {}, recorder
        )

        self.assertIn("hello", read_result)
        self.assertIn("note.txt", list_result)
        self.assertEqual(recorder.requests, [])

    def test_side_effect_approval_uses_risk_not_tool_name(self):
        operation_target = tools.WORKSPACE_DIR / "mock-operation.txt"
        calls = []

        def mock_side_effect(path: str) -> str:
            operation_target.write_text("mock", encoding="utf-8")
            return f"mock side effect: {path}"

        definition = tools.ToolDefinition(
            name="mock_side_effect",
            schema={
                "type": "function",
                "function": {
                    "name": "mock_side_effect",
                    "description": "test-only side effect",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            },
            handler=mock_side_effect,
            risk_level=tools.RiskLevel.SIDE_EFFECT,
        )
        tools.TOOL_REGISTRY[definition.name] = definition
        try:
            def approval(tool_name, arguments, operation):
                calls.append((tool_name, arguments, operation))
                return True

            messages = []
            result = run_round(
                messages,
                set(),
                "mock",
                "mock_side_effect",
                {"path": "mock-operation.txt"},
                approval,
            )

            self.assertEqual(result, "mock side effect: mock-operation.txt")
            self.assertEqual(calls[0][0], "mock_side_effect")
            self.assertEqual(calls[0][2], "CREATE")
            self.assertTrue(operation_target.is_file())
            self.assertNotIn(
                "mock_side_effect",
                {tool["function"]["name"] for tool in tools.AVAILABLE_TOOLS},
            )
        finally:
            tools.TOOL_REGISTRY.pop(definition.name, None)

    def test_side_effect_ask_mode_uses_injected_callback(self):
        input_func = Mock(return_value="y")
        callback = main.approval_callback_for_mode("ASK", input_func=input_func)
        result = run_round(
            [], set(), "ask", "write_file", {"path": "ask.txt", "content": "OK"}, callback
        )
        self.assertIn("已写入", result)
        input_func.assert_called_once()

    def test_unknown_tool_returns_normal_failure(self):
        result = run_round(
            [], set(), "unknown", "not_registered", {}, main.always_deny
        )
        self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX))

    def test_permission_runtime_does_not_branch_on_write_file_name(self):
        source = inspect.getsource(main.check_tool_permission)
        self.assertNotIn('"write_file"', source)
        self.assertNotIn("'write_file'", source)

    def test_approve_create_and_overwrite_reports_operation(self):
        recorder = ApprovalRecorder(True)
        messages = []
        executed = set()

        run_round(
            messages,
            executed,
            "create",
            "write_file",
            {"path": "notes/approval.md", "content": "FIRST"},
            recorder,
        )
        target = tools.WORKSPACE_DIR / "notes" / "approval.md"
        self.assertEqual(target.read_text(encoding="utf-8"), "FIRST")
        self.assertEqual(recorder.requests[-1][2], "CREATE")

        run_round(
            messages,
            executed,
            "overwrite",
            "write_file",
            {"path": "notes/approval.md", "content": "SECOND"},
            recorder,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "SECOND")
        self.assertEqual(recorder.requests[-1][2], "OVERWRITE")

    def test_deny_create_and_overwrite_does_not_change_files(self):
        recorder = ApprovalRecorder(False)
        messages = []
        executed = set()

        denied_create = run_round(
            messages,
            executed,
            "create",
            "write_file",
            {"path": "new.txt", "content": "NEW"},
            recorder,
        )
        self.assertTrue(denied_create.startswith(main.APPROVAL_DENIED_PREFIX))
        self.assertFalse((tools.WORKSPACE_DIR / "new.txt").exists())

        target = tools.WORKSPACE_DIR / "existing.txt"
        target.write_text("ORIGINAL", encoding="utf-8")
        denied_overwrite = run_round(
            messages,
            executed,
            "overwrite",
            "write_file",
            {"path": "existing.txt", "content": "NEW"},
            recorder,
        )
        self.assertTrue(denied_overwrite.startswith(main.APPROVAL_DENIED_PREFIX))
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")
        self.assertEqual(executed, set())

    def test_denied_action_is_not_recorded_as_successful_duplicate(self):
        messages = []
        executed = set()
        arguments = {"path": "retry.txt", "content": "finally written"}

        denied = run_round(
            messages, executed, "first", "write_file", arguments, main.always_deny
        )
        self.assertTrue(denied.startswith(main.APPROVAL_DENIED_PREFIX))
        self.assertEqual(executed, set())

        allowed = run_round(
            messages, executed, "second", "write_file", arguments, main.always_allow
        )
        self.assertIn("已写入", allowed)
        self.assertIn("retry.txt", allowed)
        self.assertEqual(len(executed), 1)

    def test_sandbox_rejection_happens_before_approval(self):
        recorder = ApprovalRecorder(True)
        messages = []
        executed = set()

        result = run_round(
            messages,
            executed,
            "escape",
            "write_file",
            {"path": "../outside.txt", "content": "NO"},
            recorder,
        )

        self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX))
        self.assertEqual(recorder.requests, [])
        self.assertFalse((Path(self.temp_dir.name).parent / "outside.txt").exists())

    def test_denied_tool_result_keeps_protocol_and_context(self):
        messages = []
        executed = set()
        result = run_round(
            messages,
            executed,
            "denied",
            "write_file",
            {"path": "denied.md", "content": "DO NOT COMPACT"},
            main.always_deny,
        )
        self.assertTrue(result.startswith(main.APPROVAL_DENIED_PREFIX))
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[1]["role"], "tool")
        self.assertEqual(messages[1]["tool_call_id"], messages[0]["tool_calls"][0]["id"])
        context = main.build_model_context(messages, mode="WRITE_ONLY")
        self.assertIn("DO NOT COMPACT", context[0]["tool_calls"][0]["function"]["arguments"])

    def test_denied_result_can_be_saved_and_restored(self):
        messages = []
        executed = set()
        run_round(
            messages,
            executed,
            "denied",
            "write_file",
            {"path": "denied.md", "content": "NO"},
            main.always_deny,
        )
        messages.append({"role": "assistant", "content": "未写入。"})

        original_sessions = session.SESSIONS_DIR
        with tempfile.TemporaryDirectory() as directory:
            session.SESSIONS_DIR = Path(directory)
            record = session.create_session("test-model", messages, "WRITE_ONLY")
            session.save_session(record)
            restored = session.load_session(record["session_id"])
        session.SESSIONS_DIR = original_sessions

        self.assertIn(main.APPROVAL_DENIED_PREFIX, restored["messages"][1]["content"])
        self.assertEqual(restored["messages"][-1]["content"], "未写入。")

    def test_mock_agent_allow_and_deny_reach_final_answer(self):
        client = Mock()
        allow_workspace_file = tools.WORKSPACE_DIR / "allow.txt"
        deny_workspace_file = tools.WORKSPACE_DIR / "deny.txt"

        for path, content, approval, expected_exists in (
            (allow_workspace_file, "ALLOW", main.always_allow, True),
            (deny_workspace_file, "DENY", main.always_deny, False),
        ):
            with self.subTest(path=path.name):
                messages = [{"role": "user", "content": "write"}]
                first = fake_message(
                    "call-1",
                    "write_file",
                    {"path": path.name, "content": content},
                )
                final = SimpleNamespace(
                    content=("已完成写入" if expected_exists else "用户拒绝，未写入"),
                    tool_calls=None,
                )

                def next_reply(*args):
                    self.assertTrue(messages[-1]["role"] == "tool")
                    if not expected_exists:
                        self.assertIn(main.APPROVAL_DENIED_PREFIX, messages[-1]["content"])
                    return model_reply(final)

                with patch.object(main, "ask", side_effect=next_reply):
                    main.run_agent_loop(
                        client,
                        "test-model",
                        messages,
                        model_reply(first),
                        set(),
                        approval,
                    )

                self.assertEqual(messages[-1]["role"], "assistant")
                self.assertTrue(path.exists() is expected_exists)


if __name__ == "__main__":
    unittest.main()
