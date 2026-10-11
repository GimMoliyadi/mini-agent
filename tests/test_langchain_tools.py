from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from langchain_core.messages import ToolMessage

from mini_agent.tools import build_tools


class LangChainToolsTests(unittest.TestCase):
    def test_registry_is_workspace_bound_and_excludes_control_flow(self) -> None:
        with TemporaryDirectory() as directory:
            tools = build_tools(Path(directory))

        self.assertEqual(
            [tool.name for tool in tools],
            [
                "list_files",
                "read_file",
                "search_text",
                "write_file",
                "apply_patch",
                "rename_file",
                "run_command",
                "inspect_project",
                "inspect_capabilities",
            ],
        )
        self.assertNotIn("finish_task", {tool.name for tool in tools})
        self.assertNotIn("workspace", tools[1].args)
        self.assertEqual(tools[3].metadata["risk"], "write")
        self.assertTrue(tools[3].metadata["mutation"])
        self.assertEqual(tools[3].metadata["workspace_arguments"], ("path",))
        self.assertEqual(tools[3].metadata["mutation_paths"], ("path",))
        self.assertEqual(tools[6].metadata["risk"], "execute")
        self.assertFalse(tools[6].metadata["mutation"])

    def test_same_tool_factory_uses_the_bound_workspace(self) -> None:
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            first_root = Path(first)
            second_root = Path(second)
            (first_root / "note.txt").write_text("first", encoding="utf-8")
            (second_root / "note.txt").write_text("second", encoding="utf-8")
            first_read = next(tool for tool in build_tools(first_root) if tool.name == "read_file")
            second_read = next(tool for tool in build_tools(second_root) if tool.name == "read_file")

            self.assertIn("first", first_read.invoke({"path": "note.txt"}))
            self.assertIn("second", second_read.invoke({"path": "note.txt"}))

    def test_list_files_validates_entries_against_bound_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("bound", encoding="utf-8")
            tool = next(tool for tool in build_tools(root) if tool.name == "list_files")

            result = tool.invoke({})

        self.assertIn("note.txt", result)

    def test_tool_call_preserves_structured_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            tool = next(tool for tool in build_tools(Path(directory)) if tool.name == "run_command")
            message = tool.invoke(
                {
                    "type": "tool_call",
                    "name": "run_command",
                    "args": {"command": "python", "args": ["-m", "unittest", "--help"]},
                    "id": "call-1",
                }
            )

        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(set(message.artifact), {"status", "code", "metadata"})
        self.assertEqual(message.artifact["status"], "success")
        self.assertEqual(message.artifact["code"], "command_succeeded")
        self.assertEqual(message.artifact["metadata"]["returncode"], 0)

    def test_capabilities_are_derived_from_tool_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            tool = next(
                tool
                for tool in build_tools(
                    Path(directory), runtime_context={"runtime_features": {"session": "active"}}
                )
                if tool.name == "inspect_capabilities"
            )
            payload = tool.invoke({})

        self.assertIn('"name":"write_file"', payload)
        self.assertIn('"risk":"write"', payload)
        self.assertIn('"session":"active"', payload)

    def test_project_introspection_lists_current_architecture_files(self) -> None:
        with TemporaryDirectory() as directory:
            tool = next(tool for tool in build_tools(Path(directory)) if tool.name == "inspect_project")
            listing = tool.invoke({})

            self.assertIn("mini_agent/policies.py", listing)
            self.assertIn("mini_agent/persistence.py", listing)
            self.assertNotIn("mini_agent/agent.py", listing)
            with self.assertRaises(PermissionError):
                tool.invoke({"path": "mini_agent/agent.py"})

    def test_bound_file_tools_keep_path_and_patch_guards(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("before", encoding="utf-8")
            tools = {tool.name: tool for tool in build_tools(root)}

            tools["apply_patch"].invoke(
                {"path": "note.txt", "old_text": "before", "new_text": "after"}
            )
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "after")
            with self.assertRaises(PermissionError):
                tools["read_file"].invoke({"path": "../outside.txt"})

    def test_command_tool_keeps_no_shell_allowlist(self) -> None:
        with TemporaryDirectory() as directory:
            tool = next(tool for tool in build_tools(Path(directory)) if tool.name == "run_command")
            with self.assertRaises(ValueError):
                tool.invoke(
                    {
                        "command": "python",
                        "args": ["-c", "print('no')"],
                    }
                )


if __name__ == "__main__":
    unittest.main()
