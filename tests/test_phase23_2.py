"""Offline tests for Phase 23.2R recovery capture and serialization."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from eval import phase23_2


def _reply(call_id: str, tool: str, arguments: dict, content: str = "") -> main.ModelReply:
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=tool,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(content=content, tool_calls=[call])
    return main.ModelReply(message, "tool_calls", 10, 2, 12)


def _scripted_recovery():
    return [
        _reply(
            "recovery_patch",
            "apply_patch",
            {
                "path": "src/pricing/coupons.py",
                "old_text": "factor = percent / 100.0 if percent > 1 else percent",
                "new_text": (
                    "factor = percent / 100.0 "
                    "if (isinstance(percent, int) or percent > 1) else percent"
                ),
            },
            "The test_one_percent_boundary failure shows that integer 1 must mean 1%, not a decimal fraction.",
        ),
        _reply(
            "recovery_test",
            "run_command",
            {
                "command": phase23_2.phase23_budget.REQUIRED_TEST[0],
                "args": list(phase23_2.phase23_budget.REQUIRED_TEST[1]),
                "cwd": phase23_2.phase23_budget.REQUIRED_TEST[2],
            },
            "",
        ),
        _reply(
            "recovery_finish",
            "finish_task",
            {"summary": "Applied the 1% boundary correction, reran the required test, and finished."},
            "",
        ),
    ]


class Phase23_2RecoveryHarnessTests(unittest.TestCase):
    def _run_scripted(self, replies: list) -> dict:
        model_name = phase23_2._prior_payload()["manifest"]["model"]
        model_config = SimpleNamespace(model=model_name)
        original_verify = phase23_2.harness._verify

        def verify_coding_trace(*args, **kwargs):
            trace = args[-1] if args else kwargs["trace"]
            self.assertIsInstance(trace, main.CodingTaskTrace)
            return original_verify(*args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="phase23-2-scripted-") as directory:
            workspace = Path(directory) / "workspace"
            with (
                patch.dict(os.environ, {"AGENT_WORKSPACE": str(workspace)}),
                patch.object(phase23_2.config, "load_env_file"),
                patch.object(phase23_2.config, "load_config", return_value=model_config),
                patch.object(phase23_2.config, "get_context_mode", return_value="WRITE_ONLY"),
                patch.object(phase23_2.config, "get_approval_mode", return_value="ALLOW"),
                patch.object(phase23_2.harness, "_verify", side_effect=verify_coding_trace),
            ):
                result = phase23_2._run_recovery(4, scripted_replies=replies)
            return result

    def test_fixed_prefix_contains_the_saved_turn_eight_failure(self):
        baseline, messages, prefix_id = phase23_2._fixed_prefix()

        self.assertEqual(len(messages), 28)
        self.assertEqual(prefix_id, "caecc4acb2ce6820")
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn("test_one_percent_boundary", messages[-1]["content"])
        self.assertIn("0.0 != 99.0", messages[-1]["content"])
        self.assertEqual(baseline["task_state"]["status"], "LIMIT_REACHED")
        self.assertEqual(baseline["task_state"]["event_seq"], 18)
        self.assertEqual(baseline["task_state"]["last_mutation_event_seq"], 17)

    def test_preflight_runs_scripted_json_round_trip_and_infrastructure_classification(self):
        validation = phase23_2._offline_harness_validation()

        result = validation["scripted_finished_result"]
        self.assertEqual(validation["status"], "passed")
        self.assertFalse(validation["provider_used"])
        self.assertTrue(validation["coding_task_trace_passed_to_verify"])
        self.assertTrue(validation["finished_result_json_round_trip"])
        self.assertTrue(phase23_2._complete_valid_run(result))
        self.assertEqual(result["status"], "FINISHED")
        self.assertEqual(
            validation["scripted_provider_failure_classification"]["infrastructure_kind"],
            "provider",
        )

    def test_scripted_finished_recovery_verifies_and_serializes_complete_result(self):
        result = self._run_scripted(_scripted_recovery())
        serialized = phase23_2._json_text(result)
        with tempfile.TemporaryDirectory(prefix="phase23-2-result-json-") as directory:
            result_path = Path(directory) / "scripted_result.json"
            result_path.write_text(serialized, encoding="utf-8")
            loaded = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertTrue(phase23_2._complete_valid_run(loaded))
        self.assertEqual(loaded["status"], "FINISHED")
        self.assertTrue(loaded["accepted"])
        self.assertEqual(loaded["recovery_model_calls"], 3)
        self.assertEqual(loaded["recovery_tool_calls"], 3)
        self.assertEqual(loaded["recovery_token_usage"]["observed_total_tokens"], 36)
        self.assertTrue(loaded["recovery_token_usage"]["complete"])
        self.assertEqual(loaded["task_state_before_recovery"]["status"], "LIMIT_REACHED")
        self.assertEqual(loaded["task_state_at_recovery_start"]["status"], "RUNNING")
        self.assertEqual(loaded["task_state_at_recovery_start"]["event_seq"], 18)
        self.assertEqual(loaded["task_state_at_recovery_start"]["last_mutation_event_seq"], 17)
        self.assertEqual(loaded["executed_call_state"]["before_recovery"]["count"], 17)
        baseline_state = phase23_2._fixed_prefix()[0]["task_state"]
        mutation_snapshot = loaded["fixture_snapshot_after_first_mutation"]
        changed_paths = sorted(
            path
            for path in set(baseline_state["initial_snapshot"]) | set(mutation_snapshot)
            if baseline_state["initial_snapshot"].get(path) != mutation_snapshot.get(path)
        )
        self.assertEqual(changed_paths, ["src/pricing/coupons.py"])
        self.assertEqual(loaded["task_state_after_recovery"]["status"], "FINISHED")
        self.assertEqual(len(loaded["canonical_history"]), 34)
        self.assertEqual(len(loaded["recovery_history"]), 6)
        self.assertTrue(loaded["analysis"]["next_model_turn_visibly_references_failure"])
        self.assertEqual(loaded["analysis"]["second_mutation_target"], "src/pricing/coupons.py")
        self.assertEqual(loaded["analysis"]["required_test_pass_turns"], [10])
        self.assertEqual(loaded["analysis"]["finish_task_turn"], 11)
        self.assertEqual(loaded["verifier_result"], loaded["acceptance"])

    def test_scripted_provider_error_is_kept_as_infrastructure_failure(self):
        replies = [
            _scripted_recovery()[0],
            ConnectionError("mock provider connection failure"),
        ]
        result = self._run_scripted(replies)
        loaded = json.loads(phase23_2._json_text(result))

        self.assertFalse(phase23_2._complete_valid_run(loaded))
        self.assertTrue(loaded["infrastructure_failure"])
        self.assertEqual(loaded["infrastructure_kind"], "provider")
        self.assertFalse(loaded["valid_real_run"])
        self.assertIn("canonical_history", loaded)
        self.assertIn("task_state_after_recovery", loaded)


if __name__ == "__main__":
    unittest.main()
