"""Offline checks for Phase 18.5 aggregation and Provider classification."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from navigation_stability import aggregate_runs, is_provider_failure  # noqa: E402


def run_record(**overrides):
    record = {
        "scenario": "small_symbol_navigation",
        "repo_size": "SMALL",
        "task_type": "navigation-only",
        "run_index": 1,
        "attempt_index": 1,
        "provider_failure": False,
        "provider_errors": [],
        "model_calls": 3,
        "tool_calls": 2,
        "list_files_calls": 0,
        "search_text_calls": 1,
        "read_file_calls": 1,
        "first_correct_file_turn": 1,
        "total_tokens": 100,
        "accepted": True,
        "tool_chain": ["search_text", "read_file", "Final"],
        "agent_ran_required_test": False,
    }
    record.update(overrides)
    return record


class NavigationStabilityTests(unittest.TestCase):
    def test_provider_errors_are_separate_from_model_behavior(self):
        self.assertTrue(is_provider_failure({"metrics": {"runtime_errors": ["APIConnectionError: Connection error."]}}))
        self.assertTrue(is_provider_failure({"metrics": {"runtime_errors": ["HTTP 429"]}}))
        self.assertFalse(is_provider_failure({"metrics": {"runtime_errors": []}}))

    def test_aggregate_reports_sample_rate_and_ranges(self):
        records = [
            run_record(run_index=1, search_text_calls=1, total_tokens=100),
            run_record(run_index=2, search_text_calls=0, total_tokens=200),
            run_record(run_index=3, search_text_calls=1, total_tokens=300),
        ]
        aggregate = aggregate_runs(records)
        self.assertEqual(aggregate["search_usage"], {"used": 2, "denominator": 3, "rate": "2/3"})
        self.assertEqual(aggregate["total_tokens"], {"min": 100, "mean": 200.0, "max": 300})
        self.assertEqual(aggregate["first_correct_file_turns"], [1, 1, 1])

    def test_provider_failure_is_excluded_from_numeric_ranges_but_counted(self):
        aggregate = aggregate_runs(
            [
                run_record(total_tokens=100),
                run_record(provider_failure=True, total_tokens=0, search_text_calls=0),
                run_record(total_tokens=300),
            ]
        )
        self.assertEqual(aggregate["provider_failures"], 1)
        self.assertEqual(aggregate["valid_runs"], 2)
        self.assertEqual(aggregate["total_tokens"], {"min": 100, "mean": 200.0, "max": 300})
        self.assertEqual(aggregate["search_usage"]["rate"], "2/2")
        self.assertEqual(aggregate["first_correct_file_turns"], [1, 1])
        self.assertEqual(len(aggregate["tool_chains"]), 2)


if __name__ == "__main__":
    unittest.main()
