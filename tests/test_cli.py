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
import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        environment = patch.dict(os.environ, {
            "AGENT_WORKSPACE": str(self.workspace),
            "MINI_AGENT_STATE_DIR": str(self.root / "state"),
            "OPENAI_API_KEY": "cli-test-private-key",
            "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
            "OPENAI_MODEL": "offline-test",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def invoke_cli(self, result=None, *, error=None, argv=None):
        output, logs = io.StringIO(), io.StringIO()
        runner = Mock(return_value=result, side_effect=error)
        with patch.object(cli, "run_task", runner), patch.object(cli.sys, "stdout", output), patch.object(cli.sys, "stderr", logs):
            code = cli.cli(["--task", "测试"] if argv is None else argv)
        return code, json.loads(output.getvalue()), logs.getvalue(), runner

    def test_run_task_delegates_to_shared_runner_without_reimplementing_runtime(self):
        contract, record = object(), {"session_id": "example"}
        result = {"status": "completed", "answer": "完成", "session_id": "example", "budget": {"model_calls": 1}}
        runner = Mock(return_value=result)
        fake_main = SimpleNamespace(run_task_with_session=runner)
        with patch.dict(sys.modules, {"main": fake_main}):
            self.assertIs(cli.run_task(None, contract, resume_id="example", record=record), result)
        runner.assert_called_once_with(None, contract=contract, resume_id="example", record=record)

    def test_completed_and_incomplete_keep_shared_runner_status(self):
        for status, expected in (("completed", 0), ("incomplete", 1), ("failed", 1), ("limit_reached", 1), ("cancelled", 130)):
            with self.subTest(status=status):
                result = {"status": status, "answer": "不可据此判断成功", "error": None, "budget": {"exhausted": status == "limit_reached"}}
                code, payload, _, _ = self.invoke_cli(result)
                self.assertEqual(code, expected)
                self.assertEqual(payload, result)

    def test_contract_result_contains_independent_acceptance(self):
        acceptance = {"accepted": False, "final_test_exit_code": 1, "reasons": ["测试失败"]}
        result = {"status": "incomplete", "answer": "模型声称完成", "error": "测试失败", "acceptance": acceptance}
        code, payload, _, _ = self.invoke_cli(result)
        self.assertEqual(code, 1)
        self.assertEqual(payload["acceptance"], acceptance)

    def test_valid_contract_and_resume_are_forwarded(self):
        path = self.root / "contract.json"
        path.write_text(json.dumps({
            "task_id": "task", "instruction": "修复", "allowed_paths": ["calculator.py"],
            "test_command": {"command": "python", "args": ["-m", "unittest", "test_calculator", "-q"]},
        }), encoding="utf-8")
        code, _, _, runner = self.invoke_cli({"status": "completed"}, argv=["--contract", str(path), "--resume", "saved-session"])
        self.assertEqual(code, 0)
        self.assertIsNone(runner.call_args.args[0])
        self.assertEqual(runner.call_args.kwargs["contract"].instruction, "修复")
        self.assertEqual(runner.call_args.kwargs["resume_id"], "saved-session")

    def test_expected_runtime_errors_are_json_and_nonzero(self):
        for error in (SystemExit("缺少配置"), PermissionError("审批不可用"), TimeoutError("请求超时"), ValueError("预算配置无效"), EOFError("无终端")):
            with self.subTest(error=type(error).__name__):
                code, payload, _, _ = self.invoke_cli(error=error)
                self.assertEqual(code, 1)
                self.assertEqual(payload["status"], "failed")
                self.assertIsNone(payload["answer"])
                self.assertTrue(payload["error"])

    def test_cancel(self):
        code, payload, _, _ = self.invoke_cli(error=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertEqual(payload["status"], "cancelled")

    def test_invalid_arguments_return_json_without_starting_runtime(self):
        for argv in ([], ["--task", " "], ["--resume", " "], ["--unknown"], ["--task", "x", "--desktop", "--workspace", "x"]):
            with self.subTest(argv=argv):
                code, payload, _, runner = self.invoke_cli(argv=argv)
                self.assertEqual(code, 2)
                self.assertEqual(payload["status"], "failed")
                runner.assert_not_called()

    def test_contract_file_and_json_errors_do_not_escape_boundary(self):
        malformed = self.root / "malformed.json"
        malformed.write_text("{bad", encoding="utf-8")
        invalid = self.root / "invalid.json"
        invalid.write_text("[]", encoding="utf-8")
        invalid_encoding = self.root / "encoding.json"
        invalid_encoding.write_bytes(bytes([255]))
        for path in (self.root / "missing.json", malformed, invalid, invalid_encoding):
            with self.subTest(path=path):
                code, payload, _, runner = self.invoke_cli(argv=["--contract", str(path)])
                self.assertEqual(code, 1)
                self.assertEqual(payload["status"], "failed")
                runner.assert_not_called()

    def test_unknown_bug_is_not_converted_to_success(self):
        with patch.object(cli, "run_task", side_effect=RuntimeError("programming bug")), patch.object(cli.sys, "stdout", io.StringIO()), patch.object(cli.sys, "stderr", io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "programming bug"):
                cli.cli(["--task", "测试"])

    def test_errors_and_nested_results_are_redacted(self):
        secret = os.environ["OPENAI_API_KEY"]
        _, error, _, _ = self.invoke_cli(error=ValueError(f"配置错误 {secret}"))
        self.assertNotIn(secret, json.dumps(error))
        _, payload, _, _ = self.invoke_cli({"status": "completed", "answer": secret, "trace": {"events": [{"message": secret}]}})
        self.assertNotIn(secret, json.dumps(payload))

    def test_runner_logs_go_to_stderr_and_stdout_contains_only_json(self):
        def runner(*args, **kwargs):
            print("运行日志")
            return {"status": "completed", "answer": "完成"}
        fake_main = SimpleNamespace(run_task_with_session=runner)
        output, logs = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"main": fake_main}), patch.object(cli.sys, "stdout", output), patch.object(cli.sys, "stderr", logs):
            self.assertEqual(cli.cli(["--task", "测试"]), 0)
        self.assertEqual(json.loads(output.getvalue())["answer"], "完成")
        self.assertIn("运行日志", logs.getvalue())

    def test_help_remains_standard_argparse_output(self):
        output = io.StringIO()
        with patch.object(cli.sys, "stdout", output):
            with self.assertRaises(SystemExit) as raised:
                cli.cli(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--resume", output.getvalue())
        self.assertNotIn('"status"', output.getvalue())

    def test_unicode_json_and_logs_are_utf8_on_legacy_windows_streams(self):
        stdout_buffer, stderr_buffer = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(stdout_buffer, encoding="cp936")
        stderr = io.TextIOWrapper(stderr_buffer, encoding="cp936")
        answer = "测试全部通过 " + chr(0x2705)
        def runner(*args, **kwargs):
            print("日志 " + chr(0x2705))
            return {"status": "completed", "answer": answer}
        fake_main = SimpleNamespace(run_task_with_session=runner)
        try:
            with patch.dict(sys.modules, {"main": fake_main}), patch.object(cli.sys, "stdout", stdout), patch.object(cli.sys, "stderr", stderr):
                self.assertEqual(cli.cli(["--task", "测试"]), 0)
            stdout.flush()
            stderr.flush()
            self.assertEqual(json.loads(stdout_buffer.getvalue().decode("utf-8"))["answer"], answer)
            self.assertIn(chr(0x2705), stderr_buffer.getvalue().decode("utf-8"))
        finally:
            stdout.detach()
            stderr.detach()


if __name__ == "__main__":
    unittest.main()
