"""Benchmark oracles cannot turn failures or missing evidence into success."""

import unittest
from unittest.mock import patch

from benchmark.metrics import aggregate
from benchmark.run_benchmark import (
    RuntimeRun, _navigation_accepted, _scenario_evidence, load_manifest, main_cli,
)


class BenchmarkV1Tests(unittest.TestCase):
    def test_runtime_errors_stay_in_acceptance_denominator(self):
        rows = [
            {"accepted": True, "expected_acceptance": True, "runtime_error": False,
             "model_calls": 4, "tool_calls": 3, "total_tokens": None},
            {"accepted": False, "expected_acceptance": True, "runtime_error": True,
             "model_calls": 0, "tool_calls": 0, "total_tokens": None},
        ]
        metrics = aggregate(rows)
        self.assertEqual(metrics["acceptance_rate"], 0.5)
        self.assertEqual(metrics["positive_acceptance_rate"], 0.5)
        self.assertEqual(metrics["runtime_error_rate"], 0.5)
        self.assertEqual(metrics["median_model_calls"], 4)
        self.assertIsNone(metrics["median_tokens"])
        self.assertIsNone(metrics["p95_tokens"])

    def test_final_answer_without_navigation_evidence_cannot_pass(self):
        task = {"scenario": "navigation", "target": "src/target.py"}
        self.assertFalse(_navigation_accepted(task, {"final_answer": "src/target.py", "events": []}))

    def test_rejected_finish_does_not_prove_path_policy_worked(self):
        run = RuntimeRun({"events": [
            {"action": "tool_call", "result_status": "finish_rejected"},
        ]}, None, None, "", "", [])
        self.assertFalse(_scenario_evidence({"scenario": "wrong_path"}, run))

    def test_retry_does_not_prove_recovery_state_machine_ran(self):
        run = RuntimeRun({"successful_commands": 1, "events": [
            {"action": "tool_call", "result_status": "command_failed"},
        ]}, None, None, "", "", [])
        self.assertFalse(_scenario_evidence({"scenario": "recovery_grace"}, run))

    def test_manifest_covers_real_recovery_and_rejected_cases(self):
        tasks = load_manifest()
        self.assertGreaterEqual(len(tasks), 30)
        self.assertLessEqual(len(tasks), 50)
        self.assertIn("recovery_grace", {task["scenario"] for task in tasks})
        self.assertTrue(any(not task["expected_acceptance"] for task in tasks))

    def test_ci_exit_code_fails_when_oracle_does_not_match(self):
        bad = {"runtime_error": False, "deterministic_match": False}
        with patch("benchmark.run_benchmark.run_one", return_value=bad), patch("benchmark.run_benchmark.render_report", return_value="fixture"):
            self.assertEqual(main_cli(["--task-id", "nav-01"]), 1)


if __name__ == "__main__":
    unittest.main()
