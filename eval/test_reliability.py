"""Regression checks for interrupted/isolated evaluation and strict grading."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run_eval
from run_task import judge_success


class ReliabilityTests(unittest.TestCase):
    def test_timeout_is_recorded(self):
        with patch.object(run_eval.subprocess, "run", side_effect=subprocess.TimeoutExpired("agent", 900)):
            result = run_eval.run_one("task", Path("tasks.json"))
        self.assertEqual(result["exit_code"], 124)
        self.assertFalse(result["result"]["success"])

    def test_child_uses_isolated_workspace(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"success": True}).encode(), b"")
        with patch.object(run_eval.subprocess, "run", return_value=completed) as run:
            run_eval.run_one("task", Path("tasks.json"), Path("isolated"))
        self.assertEqual(run.call_args.kwargs["env"]["AGENT_WORKSPACE"], "isolated")

    def test_exact_content_and_missing_file(self):
        task = {"expected_output_file": "result.txt", "success": {
            "output_content_checks": [{"scope": "output_file", "equals": "expected"}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(judge_success(task, {}, root))
            (root / "result.txt").write_text("expected\n", encoding="utf-8")
            self.assertTrue(judge_success(task, {}, root))
            (root / "result.txt").write_text("expected", encoding="utf-8")
            self.assertEqual(judge_success(task, {}, root), [])

    def test_directory_omission_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "missing.md").touch()
            self.assertTrue(judge_success({"success": {"require_complete_file_list": True}},
                                         {"final_answer": "nothing"}, root))

    def test_configuration_is_not_provider_failure(self):
        category, _ = run_eval.classify_failure({"runtime_errors": ["Configuration: missing socksio"]})
        self.assertEqual(category, "Configuration")


if __name__ == "__main__":
    unittest.main()
