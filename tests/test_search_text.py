"""Phase 17 tests for literal repository text search."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance  # noqa: E402
import main  # noqa: E402
import session  # noqa: E402
import tools  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
REPO_FIXTURE = ROOT / "tests" / "fixtures" / "repo_fixture"


def fake_message(call_id: str, tool_name: str, arguments: dict):
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    return SimpleNamespace(content=None, tool_calls=[call])


def model_tool_reply(call_id: str, tool_name: str, arguments: dict) -> main.ModelReply:
    return main.ModelReply(fake_message(call_id, tool_name, arguments), "tool_calls", 10, 2, 12)


def model_final_reply(content: str) -> main.ModelReply:
    return main.ModelReply(SimpleNamespace(content=content, tool_calls=None), "stop", 10, 2, 12)


class SearchTextTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.original_tools_workspace = tools.WORKSPACE_DIR
        self.original_main_workspace = main.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self.workspace
        main.WORKSPACE_DIR = self.workspace

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_tools_workspace
        main.WORKSPACE_DIR = self.original_main_workspace
        self.temp_dir.cleanup()

    def test_registry_exposes_search_as_read_only(self):
        definition = tools.TOOL_REGISTRY["search_text"]
        self.assertIs(tools.AVAILABLE_TOOLS[2], definition.schema)
        self.assertEqual(definition.risk_level, tools.RiskLevel.READ_ONLY)
        self.assertEqual(
            set(definition.schema["function"]["parameters"]["properties"]),
            {"query", "path", "max_results"},
        )

    def test_single_file_single_match_reports_path_line_and_context(self):
        target = self.workspace / "src" / "pricing.py"
        target.parent.mkdir()
        target.write_text("def calculate_total():\n    return 42\n", encoding="utf-8")

        result = tools.search_text("calculate_total")

        self.assertIn("src/pricing.py:1", result)
        self.assertIn("1 | def calculate_total():", result)
        self.assertIn("2 |     return 42", result)
        self.assertIn("matches_shown: 1", result)
        self.assertIn("matches_total: 1", result)
        self.assertIn("truncated: false", result)

    def test_single_file_multiple_matches_are_counted_by_matching_line(self):
        (self.workspace / "pricing.py").write_text(
            "discount = 0.1\nrate = discount\nreturn discount\n", encoding="utf-8"
        )

        result = tools.search_text("discount")

        self.assertIn("matches_shown: 3", result)
        self.assertIn("matches_total: 3", result)
        self.assertIn("pricing.py:1", result)
        self.assertIn("pricing.py:3", result)

    def test_multiple_files_use_workspace_relative_paths(self):
        for name in ("src/a.py", "src/nested/b.py"):
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("needle\n", encoding="utf-8")

        result = tools.search_text("needle", path="src")

        self.assertIn("src/a.py:1", result)
        self.assertIn("src/nested/b.py:1", result)
        self.assertIn("path: src", result)
        self.assertIn("matches_total: 2", result)

    def test_zero_matches_is_a_normal_result(self):
        (self.workspace / "note.txt").write_text("nothing here\n", encoding="utf-8")

        result = tools.search_text("missing")

        self.assertTrue(result.startswith('No matches found for "missing"'))
        self.assertIn("matches_shown: 0", result)
        self.assertIn("matches_total: 0", result)
        self.assertIn("truncated: false", result)

    def test_max_results_limits_output_and_reports_truncation(self):
        for index in range(5):
            (self.workspace / f"{index}.txt").write_text("needle\n", encoding="utf-8")

        result = tools.search_text("needle", max_results=2)

        self.assertIn("matches_shown: 2", result)
        self.assertIn("matches_total: 5", result)
        self.assertIn("truncated: true", result)
        self.assertNotIn("2.txt:1", result)

    def test_path_limits_search_to_subdirectory(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "target.py").write_text("needle\n", encoding="utf-8")
        (self.workspace / "other.py").write_text("needle\n", encoding="utf-8")

        result = tools.search_text("needle", path="src")

        self.assertIn("src/target.py:1", result)
        self.assertNotIn("other.py:1", result)
        self.assertIn("matches_total: 1", result)

    def test_parent_and_absolute_escape_are_rejected(self):
        with self.assertRaises(PermissionError):
            tools.search_text("needle", path="../")
        with self.assertRaises(PermissionError):
            tools.search_text("needle", path=str(self.workspace.parent))

    def test_symlink_search_root_escape_is_rejected(self):
        outside = Path(self.temp_dir.name).parent / f"search-outside-{self.workspace.name}"
        outside.mkdir()
        try:
            link = self.workspace / "outside-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("当前 Windows 环境不允许创建测试符号链接")
            with self.assertRaises(PermissionError):
                tools.search_text("needle", path="outside-link")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_chinese_text_is_searchable(self):
        (self.workspace / "中文.txt").write_text("折扣计算错误\n", encoding="utf-8")

        result = tools.search_text("折扣计算")

        self.assertIn("中文.txt:1", result)
        self.assertIn("折扣计算错误", result)

    def test_binary_and_invalid_utf8_files_are_skipped(self):
        (self.workspace / "binary.dat").write_bytes(b"needle\x00\xff")
        (self.workspace / "invalid.dat").write_bytes(b"needle\xff")

        result = tools.search_text("needle")

        self.assertIn("matches_total: 0", result)
        self.assertNotIn("binary.dat", result)
        self.assertNotIn("invalid.dat", result)

    def test_default_ignored_directories_are_not_searched(self):
        ignored = [".git", ".venv", "__pycache__", "sessions", "eval/runs"]
        for directory in ignored:
            target = self.workspace / directory / "hidden.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("needle\n", encoding="utf-8")
        (self.workspace / "visible.txt").write_text("needle\n", encoding="utf-8")

        result = tools.search_text("needle")

        self.assertIn("visible.txt:1", result)
        self.assertIn("matches_total: 1", result)
        for directory in ignored:
            self.assertNotIn(directory, result)

    def test_search_result_stays_within_shared_budget(self):
        long_line = "x" * 5000 + "needle" + "y" * 5000
        (self.workspace / "large.txt").write_text(long_line, encoding="utf-8")

        result = tools.search_text("needle")

        self.assertLessEqual(len(result), tools.MAX_TOOL_RESULT_CHARS)
        self.assertIn("matches_total: 1", result)
        self.assertIn("truncated: false", result)

    def test_read_only_search_does_not_request_approval(self):
        (self.workspace / "note.txt").write_text("needle\n", encoding="utf-8")
        approvals = []

        def approval(*arguments):
            approvals.append(arguments)
            return False

        messages = []
        main.run_tool_round(
            messages,
            fake_message("search", "search_text", {"query": "needle"}),
            set(),
            approval,
        )

        self.assertIn("matches_total: 1", messages[-1]["content"])
        self.assertEqual(approvals, [])

    def test_session_and_context_keep_search_tool_result(self):
        (self.workspace / "note.txt").write_text("needle\n", encoding="utf-8")
        messages = []
        main.run_tool_round(
            messages,
            fake_message("search", "search_text", {"query": "needle"}),
            set(),
            main.always_allow,
        )

        original_sessions = session.SESSIONS_DIR
        with tempfile.TemporaryDirectory() as sessions_dir:
            session.SESSIONS_DIR = Path(sessions_dir)
            record = session.create_session("test-model", messages, "WRITE_ONLY")
            session.save_session(record)
            restored = session.load_session(record["session_id"])
        session.SESSIONS_DIR = original_sessions

        self.assertEqual(restored["messages"], messages)
        context = main.build_model_context(restored["messages"], mode="WRITE_ONLY")
        self.assertIn("matches_total: 1", context[-1]["content"])

    def test_repository_navigation_coding_loop_reaches_acceptance(self):
        workspace = self.workspace / "repo_fixture"
        shutil.copytree(REPO_FIXTURE, workspace)
        tools.WORKSPACE_DIR = workspace
        main.WORKSPACE_DIR = workspace
        contract = acceptance.CodingTaskContract.from_dict(
            {
                "task_id": "discount_fix",
                "instruction": "修复 calculate_discount 的错误，让对应测试通过。",
                "allowed_paths": ["src/pricing.py"],
                "test_command": {
                    "command": "python",
                    "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_pricing.py", "-q"],
                    "cwd": ".",
                },
            }
        )
        before = acceptance.snapshot_workspace(workspace)
        old_text = "return price * rate"
        new_text = "return price * (1 - rate)"
        first = model_tool_reply("search", "search_text", {"query": "calculate_discount"})
        following = [
            model_tool_reply("read", "read_file", {"path": "src/pricing.py"}),
            model_tool_reply(
                "patch", "apply_patch", {"path": "src/pricing.py", "old_text": old_text, "new_text": new_text}
            ),
            model_tool_reply(
                "test",
                "run_command",
                {"command": "python", "args": list(contract.test_command.args), "cwd": "."},
            ),
            model_final_reply("已修复 calculate_discount，并通过对应测试。"),
        ]
        messages = [{"role": "user", "content": contract.instruction}]
        trace = main.CodingTaskTrace()
        with patch.object(main, "ask", side_effect=following):
            main.run_agent_loop(
                None,
                "mock-model",
                messages,
                first,
                set(),
                main.always_allow,
                trace=trace,
                required_test=(
                    contract.test_command.command,
                    contract.test_command.args,
                    contract.test_command.cwd,
                ),
            )

        result = acceptance.verify_contract(
            contract,
            workspace,
            before,
            agent_final_answer_present=trace.final_answer is not None,
            agent_ran_required_test=True,
            max_steps_reached=trace.max_steps_reached,
        )
        self.assertTrue(result["accepted"], result)
        self.assertEqual(trace.search_text_calls, 1)
        self.assertEqual(trace.read_file_calls, 1)
        self.assertEqual(trace.apply_patch_calls, 1)
        self.assertEqual(trace.run_command_calls, 1)


if __name__ == "__main__":
    unittest.main()
