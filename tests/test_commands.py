"""Regression tests for the bounded command tool and its LangChain metadata."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mini_agent.tools import command as tools
from mini_agent.process_runner import ProcessResult
from mini_agent.result import ToolResult
from mini_agent.tools import build_tools


class ControlledCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.original_workspace = tools.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self.workspace

    def tearDown(self) -> None:
        tools.WORKSPACE_DIR = self.original_workspace
        self.temp_dir.cleanup()

    def test_registry_declares_execution_risk(self) -> None:
        tool = next(tool for tool in build_tools(self.workspace) if tool.name == "run_command")
        self.assertEqual(tool.metadata["risk"], "execute")
        self.assertFalse(tool.metadata["mutation"])
        self.assertEqual(tool.metadata["workspace_arguments"], ("cwd",))
        self.assertNotIn("workspace", tool.args)

    def test_allow_and_shell_false_execute_successfully(self) -> None:
        (self.workspace / "test_smoke.py").write_text(
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

    def test_mock_process_result_is_adapted_without_starting_process(self) -> None:
        with patch.object(tools, "run_process", return_value=ProcessResult(0, b"clean\n", b"")) as run:
            result = tools.run_command("git", ["status"])
        run.assert_called_once()
        self.assertIn("Exit code: 0", result)
        self.assertEqual(result.code, "command_succeeded")

    def test_shell_syntax_and_unknown_commands_are_rejected(self) -> None:
        rejected = [
            ("python", ["-c", "print(1)"]),
            ("python", ["-m", "pip", "install", "x"]),
            ("git", ["push"]),
            ("powershell", []),
            ("python", ["-m", "pytest", "x.py", "&&", "del", "x"]),
            ("python", ["-m", "pytest", "../outside.py"]),
            ("git", ["diff", ".env"]),
            ("git", ["diff", "--ext-diff"]),
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

    def test_python_unittest_and_git_read_only_commands_are_allowed(self) -> None:
        with patch.object(tools, "run_process", return_value=ProcessResult(0, b"ok\n", b"")) as run:
            tools.run_command("python", ["-m", "unittest", "-q"])
            tools.run_command("git", ["diff"])
            tools.run_command("git", ["log", "-1"])
        self.assertEqual(run.call_count, 3)

    def test_cwd_escape_is_rejected_before_starting_process(self) -> None:
        with patch("process_runner.subprocess.Popen") as start:
            with self.assertRaises(PermissionError):
                tools.run_command("git", ["status"], cwd="../")
        start.assert_not_called()

    def test_timeout_returns_a_tool_result(self) -> None:
        with patch.object(
            tools, "run_process",
            return_value=ProcessResult(None, b"partial", b"slow", timed_out=True),
        ):
            result = tools.run_command("python", ["-m", "pytest", "-q"])
        self.assertIn("Exit code: None", result)
        self.assertIn("Timed out: true", result)
        self.assertIn("[命令执行超时]", result)
        self.assertIn("partial", result)
        self.assertEqual(result.code, "timeout")

    def test_nonzero_exit_code_is_returned_without_crashing(self) -> None:
        with patch.object(
            tools, "run_process", return_value=ProcessResult(3, b"", b"test failed\n")
        ):
            result = tools.run_command("python", ["-m", "unittest"])
        self.assertIn("Exit code: 3", result)
        self.assertIn("test failed", result)
        self.assertIn("Timed out: false", result)
        self.assertEqual(result.status, "command_failed")
        self.assertEqual(result.code, "exit_nonzero")

    def test_long_stdout_is_truncated(self) -> None:
        with patch.object(
            tools, "run_process",
            return_value=ProcessResult(0, b"x" * (tools.MAX_COMMAND_OUTPUT_CHARS + 100), b""),
        ):
            result = tools.run_command("git", ["status"])
        self.assertIn("[output truncated:", result)
        stdout = result.split("STDOUT:\n", 1)[1].split("\nSTDERR:", 1)[0]
        self.assertLessEqual(stdout.count("x"), tools.MAX_COMMAND_OUTPUT_CHARS)


if __name__ == "__main__":
    unittest.main()
