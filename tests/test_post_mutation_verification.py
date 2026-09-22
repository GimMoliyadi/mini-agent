"""Offline checks for Phase 19.6 post-mutation verification analysis."""

import unittest

from eval.post_mutation_verification import analyse_record, aggregate_records


def _record(events, commands, **overrides):
    record = {
        "condition": "treatment",
        "run": 9,
        "provider_failure": False,
        "run_commands": commands,
        "raw_result": {"trace": {"events": events}},
        "final_answer_present": True,
        "final_answer_turn": 6,
        "completion_hint_triggered": False,
        "accepted": True,
        "artifact_passed": True,
        "interaction_completed": True,
    }
    record.update(overrides)
    return record


class PostMutationVerificationTests(unittest.TestCase):
    def test_successful_exact_test_after_last_patch_is_self_verification(self):
        record = _record(
            [
                {"action": "tool_call", "tool": "apply_patch", "turn": 5, "executed": True, "classification": "PRODUCTIVE"}
            ],
            [{"turn": 6, "command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"], "cwd": ".", "exit_code": "0"}],
        )
        result = analyse_record(record)
        self.assertEqual(result["last_mutation_turn"], 5)
        self.assertEqual(result["last_required_test_turn"], 6)
        self.assertTrue(result["post_mutation_required_test_passed"])
        self.assertTrue(result["agent_self_verified"])

    def test_pre_mutation_failed_test_is_not_self_verification(self):
        record = _record(
            [
                {"action": "tool_call", "tool": "apply_patch", "turn": 5, "executed": True, "classification": "PRODUCTIVE"}
            ],
            [{"turn": 4, "command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"], "cwd": ".", "exit_code": "1"}],
        )
        result = analyse_record(record)
        self.assertEqual(result["exact_test_turns"], [4])
        self.assertEqual(result["last_successful_exact_test_turn"], None)
        self.assertTrue(result["exact_test_before_last_mutation"])
        self.assertFalse(result["post_mutation_required_test_passed"])
        self.assertFalse(result["agent_self_verified"])
        self.assertTrue(result["artifact_passed"])
        self.assertTrue(result["accepted"])

    def test_successful_test_followed_by_patch_is_stale(self):
        record = _record(
            [
                {"action": "tool_call", "tool": "apply_patch", "turn": 5, "executed": True, "classification": "PRODUCTIVE"}
            ],
            [{"turn": 4, "command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"], "cwd": ".", "exit_code": "0"}],
        )
        result = analyse_record(record)
        self.assertTrue(result["test_evidence_expired"])
        self.assertFalse(result["post_mutation_required_test_passed"])

    def test_aggregate_separates_execution_from_pass_and_freshness(self):
        records = [
            _record([], [{"turn": 2, "command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"], "cwd": ".", "exit_code": "0"}], run=1),
            _record([], [{"turn": 2, "command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"], "cwd": ".", "exit_code": "1"}], run=2),
        ]
        aggregate = aggregate_records(records)
        self.assertEqual(aggregate["final_answer"]["rate"], "2/2")
        self.assertEqual(aggregate["exact_test_executed"]["rate"], "2/2")
        self.assertEqual(aggregate["exact_test_passed"]["rate"], "1/2")


if __name__ == "__main__":
    unittest.main()
