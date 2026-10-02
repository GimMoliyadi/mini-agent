import io
from contextlib import redirect_stderr, redirect_stdout
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import configure


class ConfigureTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows launcher")
    def test_mini_config_reaches_wizard_before_venv_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1]
            for name in ("mini.cmd", "launcher.py", "cli.py", "configure.py", "file_safety.py"):
                shutil.copy2(root / name, Path(directory) / name)
            result = subprocess.run(
                ["cmd", "/c", str(Path(directory) / "mini.cmd"), "config"],
                stdin=subprocess.DEVNULL,
                env={**os.environ, "MINI_AGENT_STATE_DIR": directory, "AGENT_WORKSPACE": directory, "TMP": directory, "TEMP": directory},
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

    def test_invalid_url_port_does_not_save_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            for url in ("https://example.com:bad/v1", "https://example.com:99999/v1", "https://example.com:0/v1"):
                with self.subTest(url=url):
                    answers = iter((url, "model"))
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        result = configure.configure(path, input_func=lambda prompt: next(answers), secret_func=lambda prompt: "key")
                    self.assertEqual(result, 2)
                    self.assertFalse(path.exists())

    def test_atomic_replace_failure_preserves_original_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            original = "OPENAI_MODEL=old\n"
            path.write_text(original, encoding="utf-8")
            values = {key: "new" for key in configure.FIELDS}
            with patch.object(configure.os, "replace", side_effect=PermissionError("不可写")):
                with self.assertRaises(PermissionError):
                    configure.save_config(path, original.splitlines(), values)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(sorted(item.name for item in path.parent.iterdir()), [".env"])

    def test_configuration_symlink_is_rejected_before_reading(self):
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(PermissionError):
                configure.read_existing(Path("unread-config"))

    def test_cancelled_wizard_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            original = "OPENAI_MODEL=old\n"
            path.write_text(original, encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(EOFError):
                    configure.configure(path, input_func=Mock(side_effect=EOFError()), secret_func=Mock())
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
