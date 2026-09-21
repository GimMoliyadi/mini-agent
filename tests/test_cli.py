import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli


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
        def loop(client, model, messages, reply, executed, approval_callback):
            messages.append({"role": "assistant", "content": "done"})
        self.assertEqual(self.invoke(loop)["answer"], "done")

    def test_incomplete(self):
        self.assertEqual(self.invoke(lambda *args: None)["status"], "incomplete")

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
