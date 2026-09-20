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
        self.assertIn("run_command", {
            item["function"]["name"] for item in tools.AVAILABLE_TOOLS
        })

    def test_allow_and_shell_false_execute_successfully(self):
        completed = subprocess.CompletedProcess(
            ["python", "-m", "pytest", "tests/test_long_file.py", "-q"],
            0,
            "8 passed\n",
            "",
        )
        with patch("tools.subprocess.run", return_value=completed) as run:
            result = tools.run_command(
                "python", ["-m", "pytest", "tests/test_long_file.py", "-q"]
            )

        self.assertIn("Exit code: 0", result)
        self.assertIn("Timed out: false", result)
        self.assertIn("8 passed", result)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.args[0][0], sys.executable)
        self.assertEqual(run.call_args.args[0][1:3], ["-m", "pytest"])

    def test_allow_permission_reaches_subprocess(self):
        completed = subprocess.CompletedProcess(["git", "status"], 0, "clean\n", "")
        with patch("tools.subprocess.run", return_value=completed) as run:
            messages = run_round(
                {"command": "git", "args": ["status"]}, main.always_allow
            )
        run.assert_called_once()
        self.assertIn("Exit code: 0", messages[-1]["content"])

    def test_deny_permission_never_starts_subprocess(self):
        with patch("tools.subprocess.run") as run:
            messages = run_round(
                {"command": "python", "args": ["-m", "pytest", "-q"]},
                main.always_deny,
            )
        run.assert_not_called()
        self.assertIn("[用户拒绝执行]", messages[-1]["content"])

    def test_ask_permission_callback_is_used(self):
        approval = main.approval_callback_for_mode("ASK", input_func=lambda _: "y")
        completed = subprocess.CompletedProcess(["git", "log"], 0, "commit\n", "")
        with patch("tools.subprocess.run", return_value=completed):
            messages = run_round({"command": "git", "args": ["log"]}, approval)
        self.assertIn("Exit code: 0", messages[-1]["content"])

    def test_shell_syntax_and_unknown_commands_are_rejected(self):
        rejected = [
            ("python", ["-c", "print(1)"]),
            ("python", ["-m", "pip", "install", "x"]),
            ("git", ["push"]),
            ("powershell", []),
            ("python", ["-m", "pytest", "x.py", "&&", "del", "x"]),
            ("python", ["-m", "pytest", "../outside.py"]),
        ]
        with patch("tools.subprocess.run") as run:
            for command, args in rejected:
                with self.subTest(command=command, args=args):
                    with self.assertRaises(tools.CommandPolicyError):
                        tools.run_command(command, args)
        run.assert_not_called()

    def test_python_unittest_and_git_read_only_commands_are_allowed(self):
        completed = subprocess.CompletedProcess([], 0, "ok\n", "")
        with patch("tools.subprocess.run", return_value=completed) as run:
            tools.run_command("python", ["-m", "unittest", "-q"])
            tools.run_command("git", ["diff"])
            tools.run_command("git", ["log", "-1"])
        self.assertEqual(run.call_count, 3)

    def test_cwd_escape_is_rejected_before_approval(self):
        approval = Mock(return_value=True)
        with patch("tools.subprocess.run") as run:
            messages = run_round(
                {"command": "git", "args": ["status"], "cwd": "../"}, approval
            )
        run.assert_not_called()
        approval.assert_not_called()
        self.assertIn("PermissionError", messages[-1]["content"])

    def test_timeout_returns_a_tool_result(self):
        timeout = subprocess.TimeoutExpired(
            ["python", "-m", "pytest"], 0.01, output="partial", stderr="slow"
        )
        with patch("tools.subprocess.run", side_effect=timeout):
            result = tools.run_command("python", ["-m", "pytest", "-q"])
        self.assertIn("Exit code: None", result)
        self.assertIn("Timed out: true", result)
        self.assertIn("[命令执行超时]", result)
        self.assertIn("partial", result)

    def test_nonzero_exit_code_is_returned_without_crashing(self):
        completed = subprocess.CompletedProcess(
            ["python", "-m", "unittest"], 3, "", "test failed\n"
        )
        with patch("tools.subprocess.run", return_value=completed):
            result = tools.run_command("python", ["-m", "unittest"])
        self.assertIn("Exit code: 3", result)
        self.assertIn("test failed", result)
        self.assertIn("Timed out: false", result)

    def test_long_stdout_is_truncated(self):
        completed = subprocess.CompletedProcess(
            ["git", "status"], 0, "x" * (MAX_COMMAND_OUTPUT_CHARS + 100), ""
        )
        with patch("tools.subprocess.run", return_value=completed):
            result = tools.run_command("git", ["status"])
        self.assertIn("[output truncated:", result)
        stdout = result.split("STDOUT:\n", 1)[1].split("\nSTDERR:", 1)[0]
        self.assertLessEqual(stdout.count("x"), MAX_COMMAND_OUTPUT_CHARS)

    def test_mock_nonzero_result_flows_through_role_tool(self):
        completed = subprocess.CompletedProcess(["git", "diff"], 2, "", "bad diff")
        with patch("tools.subprocess.run", return_value=completed):
            messages = run_round({"command": "git", "args": ["diff"]}, main.always_allow)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn("Exit code: 2", messages[-1]["content"])
        self.assertIn("bad diff", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
