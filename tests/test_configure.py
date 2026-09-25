import io
from contextlib import redirect_stderr, redirect_stdout
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import configure


class ConfigureTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows launcher")
    def test_mini_config_reaches_wizard_before_venv_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1]
            for name in ("mini.cmd", "configure.py", "config.py"):
                shutil.copy2(root / name, Path(directory) / name)
            result = subprocess.run(
                ["cmd", "/c", str(Path(directory) / "mini.cmd"), "config"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            self.assertEqual(result.returncode, 130, result.stderr)
            self.assertIn(".env", result.stdout)
            self.assertFalse((Path(directory) / ".env").exists())

    def test_creates_config_without_echoing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            answers = iter(("https://example.com/v1", "example-model"))
            output = io.StringIO()
            with redirect_stdout(output):
                result = configure.configure(
                    path,
                    input_func=lambda prompt: next(answers),
                    secret_func=lambda prompt: "test-private-key",
                )
            self.assertEqual(result, 0)
            self.assertNotIn("test-private-key", output.getvalue())
            self.assertEqual(configure.read_existing(path)[1], {
                "OPENAI_BASE_URL": "https://example.com/v1",
                "OPENAI_MODEL": "example-model",
                "OPENAI_API_KEY": "test-private-key",
            })

    def test_updates_selected_values_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# local settings\nOPENAI_BASE_URL=https://old.example/v1\n"
                "OPENAI_MODEL=old-model\nOPENAI_API_KEY=old-key\n"
                "TOOL_APPROVAL_MODE=ASK\n",
                encoding="utf-8",
            )
            answers = iter(("https://new.example/v1", ""))
            with redirect_stdout(io.StringIO()):
                result = configure.configure(
                    path,
                    input_func=lambda prompt: next(answers),
                    secret_func=lambda prompt: "",
                )
            self.assertEqual(result, 0)
            text = path.read_text(encoding="utf-8")
            self.assertIn("# local settings\n", text)
            self.assertIn("TOOL_APPROVAL_MODE=ASK\n", text)
            self.assertIn("OPENAI_BASE_URL=https://new.example/v1\n", text)
            self.assertIn("OPENAI_MODEL=old-model\n", text)
            self.assertIn("OPENAI_API_KEY=old-key\n", text)

    def test_invalid_or_incomplete_input_does_not_change_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("TOOL_APPROVAL_MODE=ASK\n", encoding="utf-8")
            for url, key in (
                ("not-a-url", "test-key"),
                ("http://[bad-host", "test-key"),
                ("https://example.com/v1", ""),
            ):
                answers = iter((url, "example-model"))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(configure.configure(
                        path,
                        input_func=lambda prompt: next(answers),
                        secret_func=lambda prompt: key,
                    ), 2)
                self.assertEqual(path.read_text(encoding="utf-8"), "TOOL_APPROVAL_MODE=ASK\n")

    def test_example_placeholder_does_not_count_as_a_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            original = (
                "OPENAI_BASE_URL=https://example.com/v1\n"
                "OPENAI_MODEL=example-model\n"
                "OPENAI_API_KEY=sk-your-key-here\n"
            )
            path.write_text(original, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = configure.configure(
                    path,
                    input_func=lambda prompt: "",
                    secret_func=lambda prompt: "",
                )
            self.assertEqual(result, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
