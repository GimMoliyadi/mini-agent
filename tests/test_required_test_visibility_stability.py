"""Offline checks for the Phase 19.5 stability aggregation layer."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from eval.required_test_visibility_stability import (  # noqa: E402
    aggregate_stability,
    merge_valid_treatment,
    planned_attempts,
)


def _record(run, *, provider_failure=False, exact=False, hint=False, final=False, accepted=False):
    return {
        "run": run,
        "attempt_index": 1,
        "provider_failure": provider_failure,
        "agent_ran_required_test": exact,
        "completion_hint_triggered": hint,
        "final_answer_present": final,
        "max_steps_reached": False,
        "accepted": accepted,
        "ineffective_test_attempt": False,
        "model_calls": 7,
        "tool_calls": 8,
        "total_tokens": 100,
    }


class RequiredTestVisibilityStabilityTests(unittest.TestCase):
    def test_planned_runs_are_four_through_six(self):
        self.assertEqual(planned_attempts([4, 5, 6]), [(4, 1), (5, 1), (6, 1)])

    def test_provider_failures_are_preserved_but_excluded_from_merge(self):
        phase19 = [_record(1, exact=True, hint=True, final=True, accepted=True), _record(3, provider_failure=True)]
        new = [_record(4, exact=True, hint=True, final=True, accepted=True)]
        merged = merge_valid_treatment(phase19, new)

        self.assertEqual([record["run"] for record in merged], [1, 4])
        self.assertEqual(aggregate_stability(phase19, new)["new_provider_failures"], 0)
        self.assertEqual(aggregate_stability(phase19, new)["merged_valid_runs"], 2)

    def test_replacement_is_one_attempt_for_a_failed_planned_run(self):
        records = planned_attempts([4, 5, 6], failed_runs={5})
        self.assertEqual(records, [(4, 1), (5, 1), (5, 2), (6, 1)])


if __name__ == "__main__":
    unittest.main()
