"""Offline checks for the Phase 22.5 recovery fixture."""

import unittest

from eval.phase22_5 import deterministic_recovery_check


class Phase225FixtureTests(unittest.TestCase):
    def test_surface_fix_still_fails_boundary_then_complete_fix_passes(self):
        result = deterministic_recovery_check()
        self.assertNotEqual(result["baseline_exit"], 0)
        self.assertNotEqual(result["surface_exit"], 0)
        self.assertIn("test_one_percent_boundary", result["surface_output"])
        self.assertEqual(result["complete_exit"], 0)
