"""Offline checks for the Phase 19.5R recovery aggregation layer."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from eval.required_test_visibility_recovery import (  # noqa: E402
    _is_harness_failure,
    merge_recovery_records,
    recovery_plan,
)


def _record(run, *, provider_failure=False, exact=False, hint=False, final=False, accepted=False):
    return {
        "run": run,
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


class RequiredTestVisibilityRecoveryTests(unittest.TestCase):
    def test_recovery_plans_runs_seven_through_nine(self):
        self.assertEqual(recovery_plan(), [7, 8, 9])

    def test_recovery_merge_excludes_provider_failures(self):
        phase19 = [_record(1, exact=True, hint=True, final=True, accepted=True)]
        recovery = [
            _record(7, exact=True, hint=True, final=True, accepted=True),
            _record(8, provider_failure=True),
        ]
        merged = merge_recovery_records(phase19, recovery)
        self.assertEqual([record["run"] for record in merged], [1, 7])

    def test_harness_failure_is_not_a_valid_treatment(self):
        record = {
            "provider_failure": False,
            "raw_result": {
                "metrics": {"runtime_errors": ["child output was not valid JSON"]}
            },
        }
        self.assertTrue(_is_harness_failure(record))

    def test_repo_root_module_startup_smoke(self):
        environment = os.environ.copy()
        eval_path = str(ROOT / "eval")
        environment["PYTHONPATH"] = os.pathsep.join(
            path for path in (eval_path, environment.get("PYTHONPATH")) if path
        )
        completed = subprocess.run(
            [sys.executable, "-m", "eval.required_test_visibility_recovery", "--help"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("ModuleNotFoundError", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
