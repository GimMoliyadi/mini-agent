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
        def loop(client, model, messages, reply, executed):
            messages.append({"role": "assistant", "content": "done"})
        self.assertEqual(self.invoke(loop)["answer"], "done")

    def test_incomplete(self):
        self.assertEqual(self.invoke(lambda *args: None)["status"], "incomplete")

    def test_provider_error(self):
        self.assertEqual(self.invoke(TimeoutError("timeout"))["status"], "failed")

    def test_cancel(self):
        self.assertEqual(self.invoke(KeyboardInterrupt())["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
