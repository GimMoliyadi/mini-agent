"""Deterministic recovery controller boundaries."""

import unittest

from recovery import Recovery


class RecoveryTests(unittest.TestCase):
    def test_mutation_after_pass_requires_fresh_verification(self):
        recovery = Recovery()
        self.assertFalse(recovery.observe(["MUTATION"], 9))
        self.assertFalse(recovery.observe(["TEST_PASS"], 10))
        self.assertEqual(recovery.stage, "FINISH_NEEDED")
        self.assertFalse(recovery.observe(["MUTATION"], 11))
        self.assertEqual(recovery.stage, "VERIFY_NEEDED")

    def test_later_test_failure_returns_to_repair(self):
        recovery = Recovery()
        self.assertFalse(recovery.observe(["MUTATION", "TEST_PASS"], 9))
        self.assertFalse(recovery.observe(["TEST_FAIL"], 10))
        self.assertEqual(recovery.stage, "REPAIR_NEEDED")
        self.assertEqual((recovery.verifications, recovery.nonprogress), (1, 0))

    def test_premature_finish_and_multiple_detours_in_one_reply(self):
        recovery = Recovery()
        self.assertFalse(recovery.observe(["READ", "SEARCH"], 9))
        self.assertEqual(recovery.nonprogress, 1)
        self.assertFalse(recovery.observe(["FINISH_REJECTED"], 10))
        self.assertEqual((recovery.finishes, recovery.nonprogress), (0, 2))
        self.assertTrue(recovery.observe(["READ"], 11))

    def test_rejected_valid_finish_spends_opportunity(self):
        recovery = Recovery()
        recovery.observe(["MUTATION"], 9)
        recovery.observe(["TEST_PASS"], 10)
        self.assertTrue(recovery.observe(["FINISH_REJECTED"], 11))
        self.assertEqual(recovery.finishes, 1)

    def test_two_failed_cycles_stop_after_second_verification(self):
        recovery = Recovery()
        for turn, event in enumerate(("MUTATION", "TEST_FAIL", "MUTATION"), 9):
            self.assertFalse(recovery.observe([event], turn))
        self.assertTrue(recovery.observe(["TEST_FAIL"], 12))
        self.assertEqual((recovery.repairs, recovery.verifications), (2, 2))


if __name__ == "__main__":
    unittest.main()
