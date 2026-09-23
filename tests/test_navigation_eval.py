"""Offline Phase 18 fixture, contract, and metric checks."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

from eval.navigation_fixtures import SPECS, build_fixture, coding_contract, validate_fixture  # noqa: E402
from eval.navigation_metrics import calculate_navigation_metrics, navigation_accepted  # noqa: E402
import tools  # noqa: E402


class NavigationEvalTests(unittest.TestCase):
    def test_all_fixture_sizes_have_declared_counts_and_unique_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, spec in SPECS.items():
                root = Path(directory) / name
                build_fixture(root, name)
                validate_fixture(root, spec)
                self.assertEqual(len(list(root.rglob("*.py"))) + len(list(root.rglob("*.md"))), spec.file_count)
                self.assertEqual(spec.target_file.count("/"), {"SMALL": 1, "MEDIUM": 3, "LARGE-SYNTHETIC": 4}[name])

    def test_coding_contract_allows_only_ground_truth_target(self):
        for spec in SPECS.values():
            contract = coding_contract(spec)
            self.assertEqual(contract["allowed_paths"], [spec.target_file])
            self.assertNotIn(spec.target_file, contract["instruction"])

    def test_generated_coding_fixture_fails_before_patch_and_passes_after_patch(self):
        spec = SPECS["MEDIUM"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            build_fixture(root, spec.name)
            command = [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_discount.py",
                "-q",
            ]
            before = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(before.returncode, 0)
            target = root / spec.target_file
            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    "return price * rate", "return price * (1 - rate)"
                ),
                encoding="utf-8",
            )
            after = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(after.returncode, 0, after.stderr)

    def test_search_text_finds_symbol_and_error_string_in_fixture(self):
        spec = SPECS["MEDIUM"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            build_fixture(root, spec.name)
            original = tools.WORKSPACE_DIR
            tools.WORKSPACE_DIR = root
            try:
                symbol_result = tools.search_text(spec.symbol)
                error_result = tools.search_text(spec.error_string)
            finally:
                tools.WORKSPACE_DIR = original
        self.assertIn(spec.target_file, symbol_result)
        self.assertIn(spec.error_file, error_result)

    def test_metric_extraction_finds_search_result_as_first_correct_file(self):
        def call(call_id, name, arguments):
            return {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }

        replies = [SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12)] * 2
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "find it"},
            {"role": "assistant", "content": "", "tool_calls": [call("1", "list_files", {})]},
            {"role": "tool", "tool_call_id": "1", "content": "工作目录 . 的内容：\n[d] src"},
            {"role": "assistant", "content": "", "tool_calls": [call("2", "search_text", {"query": "calculate_discount"})]},
            {"role": "tool", "tool_call_id": "2", "content": "query: calculate_discount\n\nsrc/deep/discounts.py:3\n3 | def calculate_discount():"},
            {"role": "assistant", "content": "在 src/deep/discounts.py。"},
        ]
        metrics = calculate_navigation_metrics(replies, messages, "src/deep/discounts.py")
        self.assertEqual(metrics["list_files_calls"], 1)
        self.assertEqual(metrics["search_text_calls"], 1)
        self.assertEqual(metrics["first_correct_file_turn"], 2)
        self.assertEqual(metrics["navigation_tool_calls_before_correct_file"], 1)
        self.assertTrue(navigation_accepted(metrics, "src/deep/discounts.py"))

    def test_metric_extraction_counts_read_target_and_candidates(self):
        def call(call_id, name, arguments):
            return {
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }

        replies = [SimpleNamespace(prompt_tokens=5, completion_tokens=1, total_tokens=6)]
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [call("1", "read_file", {"path": "src/discounts.py"})]},
            {"role": "tool", "tool_call_id": "1", "content": "文件：src/discounts.py\n--- 内容开始 ---"},
            {"role": "assistant", "content": "src/discounts.py"},
        ]
        metrics = calculate_navigation_metrics(replies, messages, "src/discounts.py")
        self.assertEqual(metrics["read_file_calls"], 1)
        self.assertEqual(metrics["first_correct_file_method"], "read_file")
        self.assertEqual(metrics["candidate_files_inspected"], 1)


if __name__ == "__main__":
    unittest.main()
