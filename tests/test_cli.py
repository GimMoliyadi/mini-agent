import io
import json
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import acceptance
import tools


class CliTests(unittest.TestCase):
    def invoke(self, loop):
        client = Mock()
        with patch.object(cli, "load_config", return_value=SimpleNamespace(model="test")), \
             patch.object(cli.main, "build_client", return_value=client), \
             patch.object(cli.main, "ask"), patch.object(cli.main, "log_reply"), \
             patch.object(cli.main, "run_agent_loop", side_effect=loop):
            result = cli.run_task("test")
        client.close.assert_called_once()
        return result

    def test_completed(self):
        def loop(client, model, messages, reply, executed, approval_callback, trace=None, **kwargs):
            messages.append({"role": "assistant", "content": "done"})
        self.assertEqual(self.invoke(loop)["answer"], "done")

    def test_incomplete(self):
        self.assertEqual(self.invoke(lambda *args, **kwargs: None)["status"], "incomplete")

    def test_contract_result_contains_independent_acceptance(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workspace = Path(temp_dir.name)
        (workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        (workspace / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add, subtract\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(2, 3), 5)\n"
            "    def test_subtract(self): self.assertEqual(subtract(5, 3), 2)\n",
            encoding="utf-8",
        )
        contract = acceptance.CodingTaskContract.from_dict({
            "task_id": "calculator_fix",
            "instruction": "修复 calculator.py",
            "allowed_paths": ["calculator.py"],
            "test_command": {
                "command": "python",
                "args": ["-m", "unittest", "test_calculator", "-q"],
            },
        })

        def loop(client, model, messages, reply, executed, approval_callback, trace=None, **kwargs):
            self.assertEqual(
                kwargs["required_test"],
                ("python", ("-m", "unittest", "test_calculator", "-q"), "."),
            )
            self.assertIn("Coding Task 收口规则", messages[0]["content"])
            task_state = kwargs["task_state"]
            task_state.event_seq = 2
            task_state.last_mutation_event_seq = 1
            task_state.last_successful_exact_required_test_seq = 2
            task_state.record_finish_attempt("done", True, [])
            trace.set_task_state(task_state)

        original_workspace = cli.main.WORKSPACE_DIR
        original_tool_workspace = tools.WORKSPACE_DIR
        cli.main.WORKSPACE_DIR = workspace
        tools.WORKSPACE_DIR = workspace
        self.addCleanup(setattr, cli.main, "WORKSPACE_DIR", original_workspace)
        self.addCleanup(setattr, tools, "WORKSPACE_DIR", original_tool_workspace)
        client = Mock()
        with patch.object(cli, "load_config", return_value=SimpleNamespace(model="test")), \
             patch.object(cli.main, "build_client", return_value=client), \
             patch.object(cli.main, "ask"), patch.object(cli.main, "log_reply"), \
             patch.object(cli.main, "run_agent_loop", side_effect=loop):
            result = cli.run_task("ignored", contract=contract)
        self.assertTrue(result["acceptance"]["accepted"])
        self.assertEqual(result["acceptance"]["final_test_exit_code"], 0)
        self.assertEqual(result["answer"], "done")

    def test_provider_error(self):
        self.assertEqual(self.invoke(TimeoutError("timeout"))["status"], "failed")

    def test_cancel(self):
        self.assertEqual(self.invoke(KeyboardInterrupt())["status"], "cancelled")

    def capture_cli_result(self, answer):
        buffer = io.BytesIO()
        stdout = io.TextIOWrapper(buffer, encoding="cp936")
        try:
            with patch.object(cli, "run_task", return_value={
                "status": "completed",
                "answer": answer,
                "error": None,
            }), patch.object(cli.sys, "stdout", stdout), patch.object(
                cli.sys, "argv", ["cli.py", "--task", "test"]
            ):
                exit_code = cli.cli()
            stdout.flush()
            payload = json.loads(buffer.getvalue().decode("utf-8"))
        finally:
            stdout.detach()
        return exit_code, payload

    def test_unicode_json_output_preserves_content(self):
        cases = [
            "OK",
            "测试全部通过",
            "测试全部通过 ✅",
            '{"message": "测试全部通过 ✅", "ok": true}',
        ]
        for answer in cases:
            with self.subTest(answer=answer):
                exit_code, payload = self.capture_cli_result(answer)
                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["answer"], answer)


if __name__ == "__main__":
    unittest.main()
