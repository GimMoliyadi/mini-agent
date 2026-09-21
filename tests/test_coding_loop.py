"""Phase 13 tests for a bounded, model-driven coding loop."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
import tools  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "coding_workspace"


def model_tool_reply(call_id: str, tool_name: str, arguments: dict) -> main.ModelReply:
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return main.ModelReply(message, "tool_calls", 10, 2, 12)


def model_final_reply(content: str = "完成") -> main.ModelReply:
    return main.ModelReply(SimpleNamespace(content=content, tool_calls=None), "stop", 10, 2, 12)


class CodingLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "coding_workspace"
        shutil.copytree(FIXTURE, self.workspace)
        self.original_tools_workspace = tools.WORKSPACE_DIR
        self.original_main_workspace = main.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self.workspace
        main.WORKSPACE_DIR = self.workspace

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_tools_workspace
        main.WORKSPACE_DIR = self.original_main_workspace
        self.temp_dir.cleanup()

    def run_mock_loop(self, first_reply: main.ModelReply, following: list[main.ModelReply]):
        messages = [{"role": "user", "content": "修复 calculator.py，让测试通过。"}]
        trace = main.CodingTaskTrace()
        with patch.object(main, "ask", side_effect=following):
            main.run_agent_loop(
                None,
                "mock-model",
                messages,
                first_reply,
                set(),
                main.always_allow,
                trace=trace,
            )
        return messages, trace

    def run_fixture_tests(self) -> str:
        return tools.run_command(
            "python", ["-m", "unittest", "test_calculator", "-q"]
        )

    def test_fixture_starts_with_a_failing_test(self):
        result = self.run_fixture_tests()
        self.assertIn("Exit code: 1", result)
        self.assertIn("FAILED", result)

    def test_mock_a_read_write_test_final(self):
        fixed = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
        first = model_tool_reply("a-read", "read_file", {"path": "calculator.py"})
        messages, trace = self.run_mock_loop(
            first,
            [
                model_tool_reply(
                    "a-write", "write_file", {"path": "calculator.py", "content": fixed}
                ),
                model_tool_reply(
                    "a-test",
                    "run_command",
                    {"command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]},
                ),
                model_final_reply("已修复并通过测试。"),
            ],
        )

        self.assertEqual((self.workspace / "calculator.py").read_text(encoding="utf-8"), fixed)
        self.assertIn("Exit code: 0", "\n".join(m["content"] for m in messages if m["role"] == "tool"))
        self.assertEqual(trace.write_file_calls, 1)
        self.assertEqual(trace.run_command_calls, 1)
        self.assertEqual(trace.model_calls, 4)
        self.assertFalse(trace.max_steps_reached)
        self.assertEqual(trace.final_answer, "已修复并通过测试。")
        write_event = next(event for event in trace.events if event.get("tool") == "write_file")
        command_event = next(event for event in trace.events if event.get("tool") == "run_command")
        self.assertEqual(write_event["approval"], "ALLOW")
        self.assertEqual(write_event["write_target"], "calculator.py")
        self.assertEqual(command_event["exit_code"], "0")

    def test_mock_b_failure_result_allows_second_repair_and_retest(self):
        first_fix = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a + b\n"
        second_fix = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n\n# corrected\n"
        first = model_tool_reply("b-read", "read_file", {"path": "calculator.py"})
        messages, trace = self.run_mock_loop(
            first,
            [
                model_tool_reply(
                    "b-write-1", "write_file", {"path": "calculator.py", "content": first_fix}
                ),
                model_tool_reply(
                    "b-test-1",
                    "run_command",
                    {"command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]},
                ),
                model_tool_reply(
                    "b-write-2", "write_file", {"path": "calculator.py", "content": second_fix}
                ),
                model_tool_reply(
                    "b-test-2",
                    "run_command",
                    {"command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]},
                ),
                model_final_reply("第一次修复后仍失败，第二次修复已通过测试。"),
            ],
        )

        tool_results = [m["content"] for m in messages if m["role"] == "tool"]
        self.assertTrue(any("Exit code: 1" in result for result in tool_results))
        self.assertTrue(any("Exit code: 0" in result for result in tool_results))
        self.assertEqual((self.workspace / "calculator.py").read_text(encoding="utf-8"), second_fix)
        self.assertEqual(trace.write_file_calls, 2)
        self.assertEqual(trace.run_command_calls, 2)
        self.assertEqual(trace.model_calls, 6)
        self.assertFalse(trace.max_steps_reached)
        self.assertEqual(
            [event["exit_code"] for event in trace.events if event.get("tool") == "run_command"],
            ["1", "0"],
        )

    def test_mock_c_stops_at_max_steps_without_final_answer(self):
        first = model_tool_reply("c-1", "read_file", {"path": "calculator.py"})
        repeated_replies = [
            model_tool_reply(f"c-{index}", "read_file", {"path": "calculator.py"})
            for index in range(2, main.MAX_AGENT_STEPS + 1)
        ]
        messages, trace = self.run_mock_loop(first, repeated_replies)

        self.assertTrue(trace.max_steps_reached)
        self.assertIsNone(trace.final_answer)
        self.assertEqual(trace.model_calls, main.MAX_AGENT_STEPS)
        self.assertEqual(trace.tool_calls, main.MAX_AGENT_STEPS - 1)
        self.assertFalse(any(message.get("role") == "assistant" and not message.get("tool_calls") for message in messages))

    def test_denied_write_and_execution_do_not_change_or_start_process(self):
        write_messages = []
        main.run_tool_round(
            write_messages,
            model_tool_reply("deny-write", "write_file", {"path": "calculator.py", "content": "NO"}).message,
            set(),
            main.always_deny,
        )
        self.assertNotEqual((self.workspace / "calculator.py").read_text(encoding="utf-8"), "NO")
        self.assertIn(main.APPROVAL_DENIED_PREFIX, write_messages[-1]["content"])

        command_messages = []
        with patch("tools.subprocess.run") as run:
            main.run_tool_round(
                command_messages,
                model_tool_reply(
                    "deny-command",
                    "run_command",
                    {"command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]},
                ).message,
                set(),
                main.always_deny,
            )
        run.assert_not_called()
        self.assertIn(main.APPROVAL_DENIED_PREFIX, command_messages[-1]["content"])

    def test_nonzero_command_is_not_marked_as_duplicate(self):
        executed = set()
        messages = []
        call = model_tool_reply(
            "retry-command",
            "run_command",
            {"command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]},
        ).message
        with patch.object(main, "execute_tool_call", side_effect=[
            "Command: python -m unittest test_calculator -q\nExit code: 1",
            "Command: python -m unittest test_calculator -q\nExit code: 0",
        ]) as run:
            main.run_tool_round(messages, call, executed, main.always_allow)
            main.run_tool_round(messages, call, executed, main.always_allow)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(executed), 1)


if __name__ == "__main__":
    unittest.main()
