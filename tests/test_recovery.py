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


class RecoveryAdmissionTests(unittest.TestCase):
    def test_third_mutation_is_rejected_before_execution(self):
        recovery = Recovery()
        for _ in range(2):
            self.assertTrue(recovery.can_execute("MUTATION"))
            recovery.record_event("MUTATION")
        self.assertFalse(recovery.can_execute("MUTATION"))
        self.assertEqual(recovery.repairs, 2)
        self.assertFalse(recovery.end_round(9))

    def test_extra_test_is_rejected_even_without_a_new_mutation(self):
        recovery = Recovery()
        recovery.record_event("MUTATION")
        recovery.record_event("TEST_PASS")
        self.assertTrue(recovery.can_execute("TEST"))
        recovery.record_event("TEST_PASS")
        self.assertFalse(recovery.can_execute("TEST"))
        self.assertFalse(recovery.can_execute("TEST_FAIL"))
        self.assertEqual((recovery.verifications, recovery.test_attempts), (1, 2))
        self.assertTrue(recovery.can_execute("FINISH"))

    def test_valid_finish_is_accounted_after_two_complete_cycles(self):
        recovery = Recovery()
        self.assertFalse(recovery.observe(["MUTATION", "TEST_FAIL"], 9))
        self.assertFalse(recovery.observe(["MUTATION", "TEST_PASS"], 10))
        self.assertTrue(recovery.can_execute("FINISH"))
        recovery.record_event("FINISH_ACCEPTED")
        self.assertEqual((recovery.repairs, recovery.verifications, recovery.finishes), (2, 2, 1))
        self.assertEqual(recovery.stage, "FINISHED")
        self.assertFalse(recovery.end_round(15))
        self.assertFalse(recovery.can_execute("FINISH"))
        self.assertFalse(recovery.can_execute("MUTATION"))

    def test_finish_cannot_hide_an_earlier_over_quota_event(self):
        recovery = Recovery()
        events = ["MUTATION", "MUTATION", "MUTATION", "TEST_PASS", "FINISH_ACCEPTED"]
        self.assertTrue(recovery.observe(events, 9))
        self.assertEqual((recovery.repairs, recovery.verifications, recovery.finishes), (2, 0, 0))
        self.assertNotEqual(recovery.stage, "FINISHED")

    def test_recording_a_rejected_event_does_not_change_counters(self):
        recovery = Recovery()
        recovery.record_event("MUTATION")
        recovery.record_event("MUTATION")
        with self.assertRaisesRegex(ValueError, "MUTATION"):
            recovery.record_event("MUTATION")
        self.assertEqual(recovery.repairs, 2)

    def test_nonprogress_is_computed_once_per_round(self):
        recovery = Recovery()
        for event in ("READ", "SEARCH", "FINISH_REJECTED"):
            recovery.record_event(event)
        self.assertEqual(recovery.nonprogress, 0)
        self.assertFalse(recovery.end_round(9))
        self.assertEqual(recovery.nonprogress, 1)
        recovery.record_event("MUTATION")
        self.assertFalse(recovery.end_round(10))
        self.assertEqual(recovery.nonprogress, 1)
        self.assertFalse(recovery.observe(["READ", "SEARCH"], 11))
        self.assertTrue(recovery.observe([], 12))
        self.assertFalse(recovery.can_execute("MUTATION"))

    def test_fixed_test_admission_quota_does_not_invent_verification_progress(self):
        recovery = Recovery()
        self.assertFalse(recovery.observe(["TEST_FAIL"], 9))
        self.assertFalse(recovery.observe(["TEST_FAIL"], 10))
        self.assertEqual((recovery.test_attempts, recovery.verifications, recovery.nonprogress), (2, 0, 2))
        self.assertFalse(recovery.can_execute("TEST"))
        self.assertFalse(recovery.can_execute("MUTATION"))
        self.assertTrue(recovery.observe(["TEST_FAIL"], 11))
        self.assertEqual(recovery.test_attempts, 2)

    def test_rejected_valid_finish_blocks_more_tools_in_the_same_reply(self):
        recovery = Recovery()
        recovery.record_event("MUTATION")
        recovery.record_event("TEST_PASS")
        recovery.record_event("FINISH_REJECTED")
        self.assertEqual(recovery.finishes, 1)
        self.assertFalse(recovery.can_execute("FINISH"))
        self.assertFalse(recovery.can_execute("READ"))
        self.assertTrue(recovery.end_round(9))

    def test_hard_ceiling_stops_running_recovery_but_accepts_last_legal_finish(self):
        running = Recovery()
        self.assertTrue(running.observe(["MUTATION"], 15))
        self.assertFalse(running.can_execute("READ"))
        finished = Recovery()
        self.assertFalse(finished.observe(["MUTATION", "TEST_PASS", "FINISH_ACCEPTED"], 15))
        self.assertTrue(Recovery().observe(["MUTATION", "TEST_PASS", "FINISH_ACCEPTED"], 16))

    def test_premature_accepted_finish_is_not_legal(self):
        recovery = Recovery()
        self.assertFalse(recovery.can_execute("FINISH_ACCEPTED"))
        with self.assertRaisesRegex(ValueError, "FINISH_ACCEPTED"):
            recovery.record_event("FINISH_ACCEPTED")
        self.assertEqual((recovery.stage, recovery.finishes), ("REPAIR_NEEDED", 0))

    def test_observe_and_incremental_events_have_identical_state(self):
        observed, incremental = Recovery(), Recovery()
        rounds = (["READ", "SEARCH"], ["MUTATION", "TEST_FAIL"], ["MUTATION", "TEST_PASS"], ["FINISH_ACCEPTED"])
        for turn, events in enumerate(rounds, 9):
            for event in events:
                self.assertTrue(incremental.can_execute(event))
                incremental.record_event(event)
            self.assertEqual(observed.observe(events, turn), incremental.end_round(turn))
            self.assertEqual(observed, incremental)


class RecoveryUnknownTestResults(unittest.TestCase):
    def test_unknown_results_consume_fixed_test_attempts_without_claiming_progress(self):
        recovery = Recovery()
        recovery.record_event("MUTATION")
        recovery.record_event("TEST_UNKNOWN")
        self.assertEqual((recovery.stage, recovery.verifications, recovery.test_attempts), ("VERIFY_NEEDED", 0, 1))
        self.assertFalse(recovery.end_round(9))
        recovery.record_event("TEST_UNKNOWN")
        self.assertTrue(recovery.end_round(10))
        self.assertEqual((recovery.verifications, recovery.test_attempts), (0, 2))
        self.assertFalse(recovery.can_execute("TEST"))
        self.assertFalse(recovery.can_execute("FINISH_ACCEPTED"))

    def test_unknown_result_after_pass_requires_fresh_verification(self):
        recovery = Recovery()
        recovery.observe(["MUTATION", "TEST_PASS"], 9)
        self.assertTrue(recovery.observe(["TEST_UNKNOWN"], 10))
        self.assertEqual((recovery.stage, recovery.verifications), ("VERIFY_NEEDED", 1))
        self.assertFalse(recovery.can_execute("FINISH_ACCEPTED"))


if __name__ == "__main__":
    unittest.main()
