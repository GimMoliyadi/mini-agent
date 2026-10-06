"""Phase 12 tests for controlled local command execution."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import tools  # noqa: E402
from config import MAX_COMMAND_OUTPUT_CHARS  # noqa: E402
from mini_agent.result import ToolResult  # noqa: E402
from process_runner import ProcessResult  # noqa: E402


def fake_message(call_id: str, arguments: dict):
    call = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name="run_command",
            arguments=json.dumps(arguments),
        ),
    )
    return SimpleNamespace(content=None, tool_calls=[call])


def run_round(arguments: dict, approval) -> list[dict]:
    messages = []
    main.run_tool_round(messages, fake_message("command-1", arguments), set(), approval)
    return messages


class ControlledCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_tools_workspace = tools.WORKSPACE_DIR
        self.original_main_workspace = main.WORKSPACE_DIR
        tools.WORKSPACE_DIR = Path(self.temp_dir.name)
        main.WORKSPACE_DIR = Path(self.temp_dir.name)

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_tools_workspace
        main.WORKSPACE_DIR = self.original_main_workspace
        self.temp_dir.cleanup()

    def test_registry_declares_execution_risk(self):
        definition = tools.TOOL_REGISTRY["run_command"]
        self.assertIs(definition.risk_level, tools.RiskLevel.EXECUTION)
        self.assertIn("run_command", {item["function"]["name"] for item in tools.AVAILABLE_TOOLS})

    def test_allow_and_shell_false_execute_successfully(self):
        (tools.WORKSPACE_DIR / "test_smoke.py").write_text(
            "import unittest\nclass Smoke(unittest.TestCase):\n"
            "    def test_smoke(self):\n        self.assertEqual(1 + 1, 2)\n",
            encoding="utf-8",
        )
        with patch("process_runner.subprocess.Popen", wraps=subprocess.Popen) as start:
            result = tools.run_command("python", ["-m", "unittest", "-q"])
        self.assertIn("Exit code: 0", result)
        self.assertIn("Timed out: false", result)
        self.assertIn("Ran 1 test", result)
        self.assertIsInstance(result, ToolResult)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.code, "command_succeeded")
        self.assertFalse(start.call_args.kwargs["shell"])
        self.assertEqual(start.call_args.args[0][0], sys.executable)
        self.assertEqual(start.call_args.args[0][1:3], ["-m", "unittest"])
        self.assertIn("env", start.call_args.kwargs)

    def test_allow_permission_reaches_subprocess(self):
        with patch("tools.run_process", return_value=ProcessResult(0, b"clean\n", b"")) as run:
            messages = run_round({"command": "git", "args": ["status"]}, main.always_allow)
        run.assert_called_once()
        self.assertIn("Exit code: 0", messages[-1]["content"])

    def test_deny_permission_never_starts_subprocess(self):
        with patch("process_runner.subprocess.Popen") as start:
            messages = run_round({"command": "python", "args": ["-m", "pytest", "-q"]}, main.always_deny)
        start.assert_not_called()
        self.assertIn("[用户拒绝执行]", messages[-1]["content"])

    def test_ask_permission_callback_is_used(self):
        approval = main.approval_callback_for_mode("ASK", input_func=lambda _: "y")
        with patch("tools.run_process", return_value=ProcessResult(0, b"commit\n", b"")):
            messages = run_round({"command": "git", "args": ["log"]}, approval)
        self.assertIn("Exit code: 0", messages[-1]["content"])

    def test_shell_syntax_and_unknown_commands_are_rejected(self):
        rejected = [
            ("python", ["-c", "print(1)"]), ("python", ["-m", "pip", "install", "x"]),
            ("git", ["push"]), ("powershell", []),
            ("python", ["-m", "pytest", "x.py", "&&", "del", "x"]),
            ("python", ["-m", "pytest", "../outside.py"]),
            ("git", ["diff", ".env"]), ("git", ["diff", "--ext-diff"]),
            ("git", ["status", "--git-dir=.git"]),
            ("git", ["status", "--work-tree=."]),
            ("git", ["status", "--upload-pack=git-upload-pack"]),
            ("git", ["status", "-C", "."]),
        ]
        with patch("process_runner.subprocess.Popen") as start:
            for command, args in rejected:
                with self.subTest(command=command, args=args):
                    with self.assertRaises(tools.CommandPolicyError):
                        tools.run_command(command, args)
        start.assert_not_called()

    def test_python_unittest_and_git_read_only_commands_are_allowed(self):
        with patch("tools.run_process", return_value=ProcessResult(0, b"ok\n", b"")) as run:
            tools.run_command("python", ["-m", "unittest", "-q"])
            tools.run_command("git", ["diff"])
            tools.run_command("git", ["log", "-1"])
        self.assertEqual(run.call_count, 3)

    def test_cwd_escape_is_rejected_before_approval(self):
        approval = Mock(return_value=True)
        with patch("process_runner.subprocess.Popen") as start:
            messages = run_round({"command": "git", "args": ["status"], "cwd": "../"}, approval)
        start.assert_not_called()
        approval.assert_not_called()
        self.assertIn("PermissionError", messages[-1]["content"])

    def test_timeout_returns_a_tool_result(self):
        with patch("tools.run_process", return_value=ProcessResult(None, b"partial", b"slow", timed_out=True)):
            result = tools.run_command("python", ["-m", "pytest", "-q"])
        self.assertIn("Exit code: None", result)
        self.assertIn("Timed out: true", result)
        self.assertIn("[命令执行超时]", result)
        self.assertIn("partial", result)

    def test_nonzero_exit_code_is_returned_without_crashing(self):
        with patch("tools.run_process", return_value=ProcessResult(3, b"", b"test failed\n")):
            result = tools.run_command("python", ["-m", "unittest"])
        self.assertIn("Exit code: 3", result)
        self.assertIn("test failed", result)
        self.assertIn("Timed out: false", result)
        self.assertEqual(result.status, "command_failed")
        self.assertEqual(result.code, "exit_nonzero")

    def test_long_stdout_is_truncated(self):
        with patch("tools.run_process", return_value=ProcessResult(0, b"x" * (MAX_COMMAND_OUTPUT_CHARS + 100), b"")):
            result = tools.run_command("git", ["status"])
        self.assertIn("[output truncated:", result)
        stdout = result.split("STDOUT:\n", 1)[1].split("\nSTDERR:", 1)[0]
        self.assertLessEqual(stdout.count("x"), MAX_COMMAND_OUTPUT_CHARS)

    def test_mock_nonzero_result_flows_through_role_tool(self):
        with patch("tools.run_process", return_value=ProcessResult(2, b"", b"bad diff")):
            messages = run_round({"command": "git", "args": ["diff"]}, main.always_allow)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn("Exit code: 2", messages[-1]["content"])
        self.assertIn("bad diff", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
