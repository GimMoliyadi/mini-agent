"""Offline checks for the Phase 19 required-test visibility eval."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from required_test_visibility import (  # noqa: E402
    EXACT_REQUIRED_TEST,
    aggregate_condition,
    build_treatment_system_prompt,
    normalise_run,
    _fallback_run_commands,
)


class RequiredTestVisibilityTests(unittest.TestCase):
    def test_treatment_prompt_exposes_only_the_required_command_fact(self):
        prompt = build_treatment_system_prompt()
        command = "python -m unittest discover -s tests -p test_discount.py -q"
        self.assertIn(f"Required test command: {command}", prompt)
        self.assertNotIn("必须 Final", prompt)
        self.assertNotIn("优先执行", prompt)

    def test_normalise_run_detects_exact_test_hint_and_zero_test(self):
        raw_result = {
            "scenario": "medium_symbol_coding",
            "repo_size": "MEDIUM",
            "task_type": "coding",
            "metrics": {
                "model_calls": 7,
                "tool_calls": 5,
                "list_files_calls": 1,
                "search_text_calls": 1,
                "read_file_calls": 1,
                "apply_patch_calls": 1,
                "write_file_calls": 0,
                "run_command_calls": 2,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "final_answer_present": True,
                "max_steps_reached": False,
                "tool_chain": [
                    {"turn": 3, "tool": "apply_patch", "arguments": "{}", "exit_code": None},
                    {
                        "turn": 4,
                        "tool": "run_command",
                        "arguments": '{"command":"python","args":["-m","unittest","discover"],"cwd":"."}',
                        "exit_code": "0",
                    },
                    {
                        "turn": 5,
                        "tool": "run_command",
                        "arguments": '{"command":"python","args":["-m","unittest","discover","-s","tests","-p","test_discount.py","-q"],"cwd":"."}',
                        "exit_code": "0",
                    },
                ],
            },
            "trace": {"events": [{"turn": 6, "action": "final_answer"}]},
            "observed_run_commands": [
                {
                    "turn": 4,
                    "command": "python",
                    "args": ["-m", "unittest", "discover"],
                    "cwd": ".",
                    "exit_code": "0",
                    "result": "Ran 0 tests in 0.000s\nOK",
                    "completion_hint": False,
                },
                {
                    "turn": 5,
                    "command": "python",
                    "args": list(EXACT_REQUIRED_TEST["args"]),
                    "cwd": ".",
                    "exit_code": "0",
                    "result": "Ran 1 test in 0.000s\nOK\n[Completion status]",
                    "completion_hint": True,
                },
            ],
        }

        record = normalise_run(raw_result, run_index=1, condition="treatment")

        self.assertTrue(record["required_test_visible"])
        self.assertTrue(record["agent_ran_required_test"])
        self.assertEqual(record["required_test_exact_match_turn"], 5)
        self.assertEqual(record["required_test_exit_code"], "0")
        self.assertTrue(record["completion_hint_triggered"])
        self.assertEqual(record["completion_hint_turn"], 5)
        self.assertTrue(record["ineffective_test_attempt"])

    def test_aggregate_keeps_provider_failures_out_of_behavior_rates(self):
        records = [
            {"provider_failure": False, "agent_ran_required_test": True, "completion_hint_triggered": True, "final_answer_present": True, "max_steps_reached": False, "accepted": True, "model_calls": 5, "tool_calls": 4, "total_tokens": 100},
            {"provider_failure": True, "agent_ran_required_test": False, "completion_hint_triggered": False, "final_answer_present": False, "max_steps_reached": False, "accepted": False, "model_calls": 0, "tool_calls": 0, "total_tokens": 0},
        ]
        aggregate = aggregate_condition(records)
        self.assertEqual(aggregate["valid_runs"], 1)
        self.assertEqual(aggregate["provider_failures"], 1)
        self.assertEqual(aggregate["exact_required_test"], {"used": 1, "denominator": 1, "rate": "1/1"})
        self.assertEqual(aggregate["accepted"], {"used": 1, "denominator": 1, "rate": "1/1"})

    def test_fallback_maps_zero_test_to_the_correct_command_only(self):
        raw_result = {
            "process_stderr": "Ran 0 tests in 0.000s\nRan 1 test in 0.000s",
            "metrics": {
                "tool_chain": [
                    {
                        "turn": 6,
                        "tool": "run_command",
                        "arguments": '{"command":"python","args":["-m","unittest","discover"]}',
                        "exit_code": "0",
                        "result": "Command: python -m unittest discover",
                    },
                    {
                        "turn": 7,
                        "tool": "run_command",
                        "arguments": '{"command":"python","args":["-m","unittest","tests.test_discount"]}',
                        "exit_code": "0",
                        "result": "Command: python -m unittest tests.test_discount",
                    },
                ]
            },
        }
        commands = _fallback_run_commands(raw_result)
        self.assertEqual([command["test_count"] for command in commands], [0, 1])
        self.assertEqual(
            [command["ineffective_test_attempt"] for command in commands],
            [True, False],
        )


if __name__ == "__main__":
    unittest.main()
