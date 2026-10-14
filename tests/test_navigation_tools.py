"""Behavioral regression for project navigation tools and LangChain bindings."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mini_agent.tools import build_tools
from mini_agent.tools.navigation import find_files, inspect_python_symbols


class NavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "class Greeter:\n"
            "    def hello(self, name):\n"
            "        return name\n"
            "\n"
            "async def serve():\n"
            "    return Greeter()\n", encoding="utf-8",
        )
        (self.root / "src" / "module.py").write_text(
            "def setup():\n    pass\n", encoding="utf-8"
        )
        (self.root / "test_app.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "private.py").write_text("secret = 1\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET=none\n", encoding="utf-8")

    def test_find_files_recurses_with_globs_and_ignores_private_paths(self) -> None:
        output = find_files("*.py", workspace=self.root)
        self.assertIn("src/app.py", output)
        self.assertIn("src/module.py", output)
        self.assertIn("test_app.py", output)
        self.assertNotIn(".venv/private.py", output)
        self.assertNotIn(".env", output)
        self.assertIn("matches_total: 3", output)
        scoped = find_files("*app.py", path="src", workspace=self.root)
        self.assertIn("src/app.py", scoped)
        self.assertNotIn("test_app.py", scoped)

    def test_find_files_validates_limits_and_prevents_escape(self) -> None:
        for count in (0, 101, True):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    find_files("*.py", max_results=count, workspace=self.root)
        with self.assertRaises(PermissionError):
            find_files("*.py", path="../", workspace=self.root)

    def test_symbol_navigation_reports_line_ranges_and_filters(self) -> None:
        output = inspect_python_symbols("src/app.py", workspace=self.root)
        self.assertIn("src/app.py:1-3 class Greeter", output)
        self.assertIn("src/app.py:2-3 method Greeter.hello", output)
        self.assertIn("src/app.py:5-6 async function serve", output)
        filtered = inspect_python_symbols("src/app.py", query="hello", workspace=self.root)
        self.assertIn("symbols_total: 1", filtered)
        self.assertNotIn("async function serve", filtered)

    def test_nested_function_scopes_are_not_class_methods(self) -> None:
        (self.root / "src" / "nested.py").write_text(
            "def outer():\n"
            "    def inner():\n"
            "        pass\n"
            "    class Local:\n"
            "        def member(self):\n"
            "            def helper():\n"
            "                pass\n"
            "            return helper()\n"
            "    return Local()\n",
            encoding="utf-8",
        )
        output = inspect_python_symbols("src/nested.py", workspace=self.root)
        self.assertIn("function outer.inner", output)
        self.assertIn("method outer.Local.member", output)
        self.assertIn("function outer.Local.member.helper", output)
        self.assertNotIn("method outer.inner", output)
        self.assertNotIn("method outer.Local.member.helper", output)

    def test_symbol_parser_does_not_execute_code(self) -> None:
        (self.root / "src" / "bad.py").write_text(
            "raise RuntimeError('should never run')\n\nclass Safe:\n    pass\n", encoding="utf-8"
        )
        result = inspect_python_symbols("src/bad.py", workspace=self.root)
        self.assertIn("class Safe", result)
        (self.root / "src" / "broken.py").write_text("def syntax(:\n", encoding="utf-8")
        with self.assertRaises(SyntaxError):
            inspect_python_symbols("src/broken.py", workspace=self.root)

    def test_symbol_navigation_enforces_workspace_and_private_files(self) -> None:
        for path in ("../external.py", ".venv/private.py", ".env"):
            with self.subTest(path=path):
                with self.assertRaises((PermissionError, ValueError)):
                    inspect_python_symbols(path, workspace=self.root)

    def test_langchain_tools_are_bound_and_read_only(self) -> None:
        registry = {tool.name: tool for tool in build_tools(self.root)}
        listing = registry["find_files"].invoke({"pattern": "*.py"})
        self.assertIn("src/app.py", listing)
        symbols = registry["inspect_python_symbols"].invoke({"path": "src/app.py"})
        self.assertIn("Greeter.hello", symbols)
        self.assertEqual(registry["find_files"].metadata["risk"], "read")
        self.assertEqual(registry["inspect_python_symbols"].metadata["risk"], "read")
        self.assertNotIn("workspace", registry["find_files"].args)
        self.assertNotIn("workspace", registry["inspect_python_symbols"].args)


if __name__ == "__main__":
    unittest.main()
