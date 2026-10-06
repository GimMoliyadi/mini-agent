"""Offline checks for the Phase 22.5 recovery fixture."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import acceptance
from eval import phase22_5


class Phase225FixtureTests(unittest.TestCase):
    def test_recovery_fixture_matches_saved_snapshot_bytes_cross_platform(self):
        with tempfile.TemporaryDirectory(prefix="phase22-5-snapshot-") as directory:
            root = Path(directory) / "workspace"
            phase22_5.setup("E", root)
            actual = acceptance.snapshot_workspace(root)
            generated_test_hash = hashlib.sha256(
                (root / "tests/test_coupons.py").read_bytes()
            ).hexdigest()

        saved = json.loads(
            (Path(__file__).parents[1] / "docs/experiments/phase23_budget_results.json").read_text(
                encoding="utf-8"
            )
        )["runs"]["8"]["result"]["task_state"]["initial_snapshot"]
        self.assertEqual(actual, saved)
        self.assertEqual(
            generated_test_hash,
            saved["tests/test_coupons.py"],
        )

    def test_surface_fix_still_fails_boundary_then_complete_fix_passes(self):
        result = phase22_5.deterministic_recovery_check()
        self.assertNotEqual(result["baseline_exit"], 0)
        self.assertNotEqual(result["surface_exit"], 0)
        self.assertIn("test_one_percent_boundary", result["surface_output"])
        self.assertEqual(result["complete_exit"], 0)
