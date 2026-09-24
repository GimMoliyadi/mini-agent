import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
import tools


class RenameFileTests(unittest.TestCase):
    def test_rename_keeps_content_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, "WORKSPACE_DIR", Path(directory)):
            source = Path(directory) / "old.md"
            destination = Path(directory) / "你好.md"
            source.write_text("你好，我是mini agent", encoding="utf-8")
            self.assertIn("已重命名", tools.rename_file("old.md", "你好.md"))
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "你好，我是mini agent")

            source.write_text("保留", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                tools.rename_file("old.md", "你好.md")
            self.assertEqual(source.read_text(encoding="utf-8"), "保留")
            self.assertEqual(destination.read_text(encoding="utf-8"), "你好，我是mini agent")
            with self.assertRaises(PermissionError):
                tools.rename_file("old.md", "../outside.md")

    def test_rename_requires_approval_and_checks_both_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tools, "WORKSPACE_DIR", Path(directory)):
            (Path(directory) / "old.md").write_text("原文", encoding="utf-8")
            call = SimpleNamespace(function=SimpleNamespace(
                name="rename_file", arguments=json.dumps({"source": "old.md", "destination": "new.md"})))
            allowed, result = main.check_tool_permission(call, main.always_deny)
            self.assertFalse(allowed)
            self.assertIn(main.APPROVAL_DENIED_PREFIX, result)
            self.assertTrue((Path(directory) / "old.md").exists())

            call.function.arguments = json.dumps({"source": "old.md", "destination": "../outside.md"})
            allowed, result = main.check_tool_permission(call, main.always_allow)
            self.assertFalse(allowed)
            self.assertIn(main.TOOL_FAILURE_PREFIX, result)

    def test_agent_round_can_rename(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(tools, "WORKSPACE_DIR", Path(directory)), \
             patch.object(main, "WORKSPACE_DIR", Path(directory)):
            (Path(directory) / "old.md").write_text("原文", encoding="utf-8")
            call = SimpleNamespace(id="rename-1", function=SimpleNamespace(
                name="rename_file", arguments=json.dumps({"source": "old.md", "destination": "new.md"})))
            messages = []
            with redirect_stdout(io.StringIO()):
                main.run_tool_round(messages, SimpleNamespace(tool_calls=[call], content=None),
                                    set(), main.always_allow)
            self.assertEqual([message["role"] for message in messages], ["assistant", "tool"])
            self.assertIn("已重命名", messages[-1]["content"])
            self.assertEqual((Path(directory) / "new.md").read_text(encoding="utf-8"), "原文")


if __name__ == "__main__":
    unittest.main()
