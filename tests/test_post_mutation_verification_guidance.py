"""Offline checks for the Phase 20 guidance-only Treatment harness."""

import unittest

from eval.post_mutation_verification_guidance import (
    GUIDANCE_TEXT,
    aggregate_guidance_records,
    build_guided_treatment_system_prompt,
)


class PostMutationVerificationGuidanceTests(unittest.TestCase):
    def test_guidance_prompt_adds_only_the_final_verification_requirement(self):
        prompt = build_guided_treatment_system_prompt()
        self.assertIn("Required test command: python -m unittest discover -s tests -p test_discount.py -q", prompt)
        self.assertIn(GUIDANCE_TEXT, prompt)
        self.assertNotIn("优先执行", prompt)
        self.assertNotIn("必须 Final", prompt)
        self.assertNotIn("关闭 Completion Hint", prompt)

    def test_aggregate_excludes_provider_failures_from_behavior_denominator(self):
        records = [
            {
                "provider_failure": False,
                "forensics": {
                    "post_mutation_required_test_passed": True,
                    "agent_self_verified": True,
                    "completion_hint_triggered": True,
                    "final_answer_present": True,
                    "accepted": True,
                    "artifact_passed": True,
                    "interaction_completed": True,
                    "max_steps_reached": False,
                    "exact_test_turns": [5],
                    "exact_test_exit_codes": ["0"],
                },
            },
            {
                "provider_failure": True,
                "forensics": {
                    "post_mutation_required_test_passed": False,
                    "agent_self_verified": False,
                    "completion_hint_triggered": False,
                    "final_answer_present": False,
                    "accepted": False,
                    "artifact_passed": False,
                    "interaction_completed": False,
                    "max_steps_reached": False,
                    "exact_test_turns": [],
                    "exact_test_exit_codes": [],
                },
            },
        ]
        aggregate = aggregate_guidance_records(records)
        self.assertEqual(aggregate["valid_runs"], 1)
        self.assertEqual(aggregate["provider_failures"], 1)
        self.assertEqual(aggregate["agent_self_verified"]["rate"], "1/1")


if __name__ == "__main__":
    unittest.main()
