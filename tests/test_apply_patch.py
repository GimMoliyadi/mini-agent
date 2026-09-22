"""Phase 16 tests for exact, permission-aware local patch editing."""

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance  # noqa: E402
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


class ApplyPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.original_tools_workspace = tools.WORKSPACE_DIR
        self.original_main_workspace = main.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self.workspace
        main.WORKSPACE_DIR = self.workspace

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_tools_workspace
        main.WORKSPACE_DIR = self.original_main_workspace
        self.temp_dir.cleanup()

    def run_round(self, call_id, arguments, approval=main.always_allow, trace=None):
        messages = []
        executed = set()
        main.run_tool_round(
            messages,
            fake_message(call_id, "apply_patch", arguments),
            executed,
            approval,
            trace=trace,
            turn=1,
        )
        return messages[-1]["content"], executed, messages

    def test_registry_exposes_apply_patch_as_side_effect(self):
        definition = tools.TOOL_REGISTRY["apply_patch"]
        self.assertIn(definition.schema, tools.AVAILABLE_TOOLS)
        self.assertEqual(definition.risk_level, tools.RiskLevel.SIDE_EFFECT)
        self.assertEqual(
            set(definition.schema["function"]["parameters"]["properties"]),
            {"path", "old_text", "new_text"},
        )

    def test_unique_match_replaces_only_one_fragment_and_reports_metadata(self):
        target = self.workspace / "calculator.py"
        target.write_text("return a - b\nreturn a + b\n", encoding="utf-8")

        result = tools.apply_patch("calculator.py", "return a - b", "return a + b")

        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "return a + b\nreturn a + b\n",
        )
        self.assertIn("calculator.py", result)
        self.assertIn("replaced occurrence count = 1", result)
        self.assertIn("old_text length = 12", result)
        self.assertIn("new_text length = 12", result)

    def test_missing_match_is_a_tool_error_and_leaves_file_unchanged(self):
        target = self.workspace / "note.txt"
        target.write_text("current", encoding="utf-8")

        result = main.execute_tool_call(
            fake_message("missing", "apply_patch", {
                "path": "note.txt",
                "old_text": "stale",
                "new_text": "new",
            }).tool_calls[0]
        )

        self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX))
        self.assertIn("目标文本不存在", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "current")

    def test_multiple_matches_are_rejected_without_partial_change(self):
        target = self.workspace / "repeat.txt"
        target.write_text("same\nkeep\nsame\n", encoding="utf-8")

        result = main.execute_tool_call(
            fake_message("multiple", "apply_patch", {
                "path": "repeat.txt",
                "old_text": "same",
                "new_text": "changed",
            }).tool_calls[0]
        )

        self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX))
        self.assertIn("目标文本不唯一", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "same\nkeep\nsame\n")

    def test_whitespace_difference_is_not_fuzzy_matched(self):
        target = self.workspace / "whitespace.txt"
        target.write_text("value = 1", encoding="utf-8")

        result = main.execute_tool_call(
            fake_message("whitespace", "apply_patch", {
                "path": "whitespace.txt",
                "old_text": "value=1",
                "new_text": "value=2",
            }).tool_calls[0]
        )

        self.assertTrue(result.startswith(main.TOOL_FAILURE_PREFIX))
        self.assertIn("目标文本不存在", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "value = 1")

    def test_empty_new_text_deletes_the_unique_fragment(self):
        target = self.workspace / "delete.txt"
        target.write_text("before\nREMOVE\nafter\n", encoding="utf-8")

        result = tools.apply_patch("delete.txt", "REMOVE\n", "")

        self.assertIn("new_text length = 0", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "before\nafter\n")

    def test_chinese_content_is_replaced_as_utf8(self):
        target = self.workspace / "中文.txt"
        target.write_text("旧内容：你好\n", encoding="utf-8")

        tools.apply_patch("中文.txt", "旧内容：你好", "新内容：世界")

        self.assertEqual(target.read_text(encoding="utf-8"), "新内容：世界\n")

    def test_multiline_match_is_literal_and_exact(self):
        target = self.workspace / "multi.py"
        target.write_text("def f():\n    first()\n    second()\n", encoding="utf-8")

        tools.apply_patch(
            "multi.py",
            "def f():\n    first()\n    second()",
            "def f():\n    replacement()",
        )

        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "def f():\n    replacement()\n",
        )

    def test_existing_crlf_style_is_preserved(self):
        target = self.workspace / "crlf.txt"
        target.write_bytes(b"one\r\ntwo\r\n")

        tools.apply_patch("crlf.txt", "one\r\n", "ONE\r\n")

        self.assertEqual(target.read_bytes(), b"ONE\r\ntwo\r\n")

    def test_workspace_escape_is_rejected_before_any_write(self):
        outside = self.workspace.parent / "phase16-outside.txt"
        outside.unlink(missing_ok=True)

        with self.assertRaises(PermissionError):
            tools.apply_patch("../phase16-outside.txt", "old", "new")

        self.assertFalse(outside.exists())

    def test_allow_permission_reaches_patch_handler(self):
        target = self.workspace / "allow.txt"
        target.write_text("old", encoding="utf-8")
        approvals = []

        def approve(tool_name, arguments, operation):
            approvals.append((tool_name, arguments, operation))
            return True

        result, executed, _ = self.run_round(
            "allow", {"path": "allow.txt", "old_text": "old", "new_text": "new"}, approve
        )

        self.assertIn("patch", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(approvals[0][0], "apply_patch")
        self.assertEqual(approvals[0][2], "OVERWRITE")
        self.assertEqual(len(executed), 1)

    def test_deny_permission_leaves_file_unchanged(self):
        target = self.workspace / "deny.txt"
        target.write_text("original", encoding="utf-8")

        result, executed, _ = self.run_round(
            "deny", {"path": "deny.txt", "old_text": "original", "new_text": "changed"},
            main.always_deny,
        )

        self.assertTrue(result.startswith(main.APPROVAL_DENIED_PREFIX))
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(executed, set())

    def test_duplicate_protection_does_not_apply_same_patch_twice(self):
        target = self.workspace / "duplicate.txt"
        target.write_text("old", encoding="utf-8")
        trace = main.CodingTaskTrace()
        args = {"path": "duplicate.txt", "old_text": "old", "new_text": "new"}

        first, executed, _ = self.run_round("first", args, trace=trace)
        messages = []
        main.run_tool_round(
            messages,
            fake_message("second", "apply_patch", args),
            executed,
            main.always_allow,
            trace=trace,
            turn=2,
        )

        self.assertIn("patch", first)
        self.assertTrue(messages[-1]["content"].startswith("[重复调用被拦截]"))
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(trace.apply_patch_calls, 2)
        self.assertEqual(trace.patch_successes, 1)
        self.assertEqual(trace.patch_failures, 0)

    def test_failed_patch_can_be_retried_after_file_is_reread(self):
        target = self.workspace / "retry.txt"
        target.write_text("current", encoding="utf-8")
        args = {"path": "retry.txt", "old_text": "stale", "new_text": "new"}

        failed, executed, _ = self.run_round("failed", args)
        self.assertTrue(failed.startswith(main.TOOL_FAILURE_PREFIX))
        self.assertEqual(executed, set())

        target.write_text("stale", encoding="utf-8")
        succeeded, executed, _ = self.run_round("retry", args)
        self.assertIn("replaced occurrence count = 1", succeeded)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(len(executed), 1)

    def test_patch_history_can_be_saved_and_restored(self):
        target = self.workspace / "session.txt"
        target.write_text("old", encoding="utf-8")
        messages = []
        executed = set()
        main.run_tool_round(
            messages,
            fake_message("patch-session", "apply_patch", {
                "path": "session.txt", "old_text": "old", "new_text": "new"
            }),
            executed,
            main.always_allow,
        )
        messages.append({"role": "assistant", "content": "已完成"})

        original_sessions = session.SESSIONS_DIR
        with tempfile.TemporaryDirectory() as directory:
            session.SESSIONS_DIR = Path(directory)
            record = session.create_session("test-model", messages, "WRITE_ONLY")
            session.save_session(record)
            restored = session.load_session(record["session_id"])
        session.SESSIONS_DIR = original_sessions

        arguments = restored["messages"][0]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(arguments)["old_text"], "old")
        self.assertEqual(restored["messages"][-1]["content"], "已完成")

    def test_patch_arguments_are_not_rewritten_by_context_compaction(self):
        target = self.workspace / "context.txt"
        target.write_text("old", encoding="utf-8")
        messages = []
        main.run_tool_round(
            messages,
            fake_message("context", "apply_patch", {
                "path": "context.txt", "old_text": "old", "new_text": "new"
            }),
            set(),
            main.always_allow,
        )

        context = main.build_model_context(messages, mode="WRITE_ONLY")
        arguments = json.loads(context[0]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(arguments["old_text"], "old")
        self.assertEqual(arguments["new_text"], "new")

    def test_mock_b_ambiguous_patch_then_more_context_succeeds(self):
        target = self.workspace / "ambiguous.txt"
        target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

        failed, executed, _ = self.run_round(
            "ambiguous", {"path": "ambiguous.txt", "old_text": "value = 1", "new_text": "value = 2"}
        )
        self.assertIn("目标文本不唯一", failed)
        self.assertEqual(executed, set())

        success, executed, _ = self.run_round(
            "specific",
            {
                "path": "ambiguous.txt",
                "old_text": "value = 1\nvalue = 1\n",
                "new_text": "value = 2\nvalue = 1\n",
            },
        )
        self.assertIn("replaced occurrence count = 1", success)
        self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\nvalue = 1\n")
        self.assertEqual(len(executed), 1)

    def test_mock_c_changed_target_returns_failure_then_new_patch_succeeds(self):
        target = self.workspace / "changed.txt"
        target.write_text("current", encoding="utf-8")
        args = {"path": "changed.txt", "old_text": "old", "new_text": "new"}

        failed, executed, _ = self.run_round("stale", args)
        self.assertIn("目标文本不存在", failed)
        self.assertEqual(executed, set())

        success, executed, _ = self.run_round(
            "fresh", {"path": "changed.txt", "old_text": "current", "new_text": "new"}
        )
        self.assertIn("replaced occurrence count = 1", success)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(len(executed), 1)

    def test_mock_d_denial_is_a_valid_tool_result_and_no_mutation(self):
        target = self.workspace / "mock-d.txt"
        target.write_text("old", encoding="utf-8")

        result, _, messages = self.run_round(
            "denied", {"path": "mock-d.txt", "old_text": "old", "new_text": "new"},
            main.always_deny,
        )

        self.assertTrue(result.startswith(main.APPROVAL_DENIED_PREFIX))
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], "denied")

    def test_mock_a_patch_test_final_coding_loop(self):
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8"
        )
        (self.workspace / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\n\n"
            "class Tests(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )
        before = acceptance.snapshot_workspace(self.workspace)
        contract = acceptance.CodingTaskContract.from_dict({
            "task_id": "patch_test",
            "instruction": "修复 calculator.py",
            "allowed_paths": ["calculator.py"],
            "test_command": {
                "command": "python",
                "args": ["-m", "unittest", "test_calculator", "-q"],
            },
        })
        first = main.ModelReply(
            fake_message("patch", "apply_patch", {
                "path": "calculator.py", "old_text": "return a - b", "new_text": "return a + b"
            }),
            "tool_calls", 10, 2, 12,
        )
        following = [
            main.ModelReply(
                fake_message("test", "run_command", {
                    "command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]
                }),
                "tool_calls", 10, 2, 12,
            ),
            main.ModelReply(
                fake_message("finish", "finish_task", {"summary": "patch → test → finish"}),
                "tool_calls", 10, 2, 12,
            ),
        ]
        messages = [{"role": "user", "content": "修复 calculator.py"}]
        trace = main.CodingTaskTrace()
        task_state = acceptance.TaskState(initial_snapshot=before)
        with patch.object(main, "ask", side_effect=following):
            main.run_agent_loop(
                None,
                "mock-model",
                messages,
                first,
                set(),
                main.always_allow,
                trace=trace,
                required_test=("python", ("-m", "unittest", "test_calculator", "-q"), "."),
                contract=contract,
                task_state=task_state,
            )

        result = acceptance.verify_contract(
            contract,
            self.workspace,
            before,
            task_state=task_state,
            agent_final_answer_present=trace.final_answer is not None,
            agent_ran_required_test=trace.successful_commands == 1,
            max_steps_reached=trace.max_steps_reached,
        )
        self.assertTrue(result["accepted"], result)
        self.assertEqual(trace.apply_patch_calls, 1)
        self.assertEqual(trace.patch_successes, 1)
        self.assertEqual(trace.run_command_calls, 1)
        self.assertEqual(task_state.finish_message, "patch → test → finish")


if __name__ == "__main__":
    unittest.main()
