"""Offline coverage for the Phase 22 fixtures and local harness."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


import acceptance  # noqa: E402
from eval import phase22_fixtures as fixtures  # noqa: E402
from eval import phase22_harness as harness  # noqa: E402


def _run_required_test(root: Path, spec) -> subprocess.CompletedProcess[str]:
    """Run one fixture test with deterministic UTF-8 decoding and no shell."""
    contract = acceptance.CodingTaskContract.from_dict(fixtures.coding_contract(spec))
    environment = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, *contract.test_command.args],
        cwd=root / contract.test_command.cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


class Phase22FixtureTests(unittest.TestCase):
    def test_registry_has_exactly_six_tasks_and_valid_contracts(self):
        specs = list(fixtures.TASKS.values())

        self.assertEqual(len(specs), 6)
        self.assertTrue(all(isinstance(spec, fixtures.TaskSpec) for spec in specs))
        self.assertEqual({spec.code for spec in specs}, set("ABCDEF"))
        self.assertEqual(
            {fixtures.get_task(spec.code).task_id for spec in specs},
            {spec.task_id for spec in specs},
        )

        for spec in specs:
            contract_dict = fixtures.coding_contract(spec)
            contract = acceptance.CodingTaskContract.from_dict(contract_dict)
            self.assertEqual(contract.task_id, spec.task_id)
            self.assertEqual(
                contract.allowed_paths,
                spec.ground_truth.intended_changed_files,
            )
            self.assertEqual(contract.test_command.command, "python")
            self.assertEqual(contract.test_command.args[:4], ("-m", "unittest", "discover", "-s"))
            self.assertEqual(contract.test_command.args[4], "tests")
            self.assertEqual(contract.test_command.args[5], "-p")
            self.assertEqual(contract.test_command.args[6], spec.expected_test)
            self.assertEqual(contract.test_command.args[7:], ("-q",))
            self.assertTrue(contract.require_test_pass)
            self.assertNotIn("ground_truth", contract_dict)
            self.assertIn("bug_explanation", spec.ground_truth.as_dict())
            self.assertNotIn("bug_explanation", contract_dict)

    def test_fixture_resets_to_33_files_and_each_focused_test_needs_its_fix(self):
        with tempfile.TemporaryDirectory(prefix="phase22-fixtures-") as directory:
            root = Path(directory) / "workspace"
            for spec in fixtures.TASKS.values():
                fixtures.build_fixture(root)
                (root / "stale.txt").write_text("must be removed", encoding="utf-8")
                fixtures.build_fixture(root)
                fixtures.validate_fixture(root)

                snapshot = acceptance.snapshot_workspace(root)
                self.assertEqual(len(snapshot), 33)
                self.assertNotIn("stale.txt", snapshot)

                before = _run_required_test(root, spec)
                self.assertNotEqual(
                    before.returncode,
                    0,
                    f"{spec.code} baseline unexpectedly passed:\n{before.stdout}\n{before.stderr}",
                )

                changed = fixtures.apply_ground_truth_fix(root, spec)
                self.assertEqual(changed, spec.intended_changed_files)
                after = _run_required_test(root, spec)
                self.assertEqual(after.returncode, 0, after.stdout + after.stderr)

                changed_files = acceptance.changed_files(
                    snapshot, acceptance.snapshot_workspace(root)
                )
                self.assertEqual(changed_files, sorted(spec.intended_changed_files))

    def test_fixture_workspaces_are_isolated(self):
        with tempfile.TemporaryDirectory(prefix="phase22-isolation-") as directory:
            first = fixtures.build_fixture(Path(directory) / "first")
            second = fixtures.build_fixture(Path(directory) / "second")
            second_before = acceptance.snapshot_workspace(second)
            second_source_before = (second / "src/orders/shipping.py").read_text(
                encoding="utf-8"
            )

            fixtures.apply_ground_truth_fix(first, "A")

            self.assertEqual(acceptance.snapshot_workspace(second), second_before)
            self.assertEqual(
                (second / "src/orders/shipping.py").read_text(encoding="utf-8"),
                second_source_before,
            )
            self.assertNotEqual(
                (first / "src/orders/shipping.py").read_text(encoding="utf-8"),
                second_source_before,
            )


class Phase22HarnessTests(unittest.TestCase):
    def test_scripted_a_path_accepts_only_after_fresh_exact_test(self):
        with tempfile.TemporaryDirectory(prefix="phase22-scripted-") as directory:
            root = fixtures.build_fixture(Path(directory) / "workspace")
            fixed_source = fixtures.ground_truth_patch("A")[
                "src/orders/shipping.py"
            ]
            replies = [
                harness._scripted_tool_reply(
                    "read", "read_file", {"path": "src/orders/shipping.py"}
                ),
                harness._scripted_tool_reply(
                    "write",
                    "write_file",
                    {"path": "src/orders/shipping.py", "content": fixed_source},
                ),
                harness._scripted_tool_reply(
                    "test",
                    "run_command",
                    {
                        "command": "python",
                        "args": [
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-p",
                            "test_shipping.py",
                            "-q",
                        ],
                    },
                ),
                harness._scripted_tool_reply(
                    "finish",
                    "finish_task",
                    {"summary": "Fixed shipping and ran the required test."},
                ),
            ]

            result = harness.run_scripted_case(root, "A", replies)

        metrics = result["metrics"]
        self.assertTrue(result["acceptance"]["accepted"])
        self.assertTrue(result["acceptance"]["artifact_passed"])
        self.assertTrue(result["acceptance"]["interaction_completed"])
        self.assertTrue(result["acceptance"]["agent_self_verified"])
        self.assertEqual(metrics["first_correct_file_turn"], 1)
        self.assertEqual(metrics["first_correct_file_method"], "read_file")
        self.assertEqual(metrics["required_test_attempts"], 1)
        self.assertEqual(metrics["required_test_final_exit_code"], 0)
        self.assertTrue(metrics["post_mutation_exact_test_pass"])
        self.assertTrue(metrics["finish_after_fresh_verification"])
        self.assertGreater(
            result["task_state"]["last_successful_exact_required_test_seq"],
            result["task_state"]["last_mutation_event_seq"],
        )
        self.assertEqual(metrics["changed_files"], ["src/orders/shipping.py"])
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)

    def test_local_harness_validation_is_offline(self):
        with patch.object(
            harness,
            "_provider_preflight",
            side_effect=AssertionError("provider preflight must not run"),
        ):
            validation = harness.run_local_harness_validation()

        self.assertTrue(validation["passed"])
        self.assertEqual(
            validation["checks"],
            {
                "fixture_reset": True,
                "task_isolation": True,
                "contracts": True,
                "ground_truth_and_verifier": True,
                "scripted_finish_protocol": True,
                "metrics_and_serialization": True,
            },
        )

    def test_failure_taxonomy_is_deterministic_for_infrastructure_completion_and_verification(self):
        infrastructure = harness._annotate_result(
            {"infrastructure_failure": True, "metrics": {}}
        )
        completion = harness._annotate_result(
            {
                "infrastructure_failure": False,
                "metrics": {
                    "accepted": False,
                    "artifact_passed": True,
                    "agent_self_verified": True,
                    "interaction_completed": False,
                    "policy_rejected": 0,
                    "relevant_files_seen": ["src/orders/shipping.py"],
                    "task_flags": {},
                    "modified_tests": False,
                    "wrong_file_mutations": 0,
                    "run_command_calls": 0,
                    "required_test_attempts": 0,
                },
            }
        )
        verification = harness._annotate_result(
            {
                "infrastructure_failure": False,
                "metrics": {
                    "accepted": False,
                    "artifact_passed": False,
                    "agent_self_verified": False,
                    "interaction_completed": False,
                    "policy_rejected": 0,
                    "relevant_files_seen": ["src/orders/shipping.py"],
                    "task_flags": {},
                    "modified_tests": False,
                    "wrong_file_mutations": 0,
                    "run_command_calls": 1,
                    "required_test_attempts": 1,
                    "post_mutation_exact_test_pass": False,
                },
            }
        )
        context = harness._annotate_result(
            {
                "infrastructure_failure": False,
                "metrics": {
                    "accepted": False,
                    "artifact_passed": False,
                    "agent_self_verified": False,
                    "interaction_completed": False,
                    "policy_rejected": 0,
                    "mutation_before_source_context": True,
                    "relevant_files_seen": ["src/orders/shipping.py"],
                    "task_flags": {},
                    "modified_tests": False,
                    "wrong_file_mutations": 0,
                    "run_command_calls": 0,
                    "required_test_attempts": 0,
                },
            }
        )

        self.assertEqual(infrastructure["primary_failure"], "INFRASTRUCTURE")
        self.assertEqual(infrastructure["secondary_failure"], [])
        self.assertEqual(completion["primary_failure"], "COMPLETION")
        self.assertEqual(completion["secondary_failure"], [])
        self.assertEqual(verification["primary_failure"], "VERIFICATION")
        self.assertEqual(verification["secondary_failure"], [])
        self.assertEqual(context["primary_failure"], "CONTEXT")
        self.assertEqual(context["secondary_failure"], [])
        self.assertEqual(
            harness._failure_taxonomy([infrastructure, completion, verification, context]),
            {
                "COMPLETION": 1,
                "CONTEXT": 1,
                "INFRASTRUCTURE": 1,
                "VERIFICATION": 1,
            },
        )

    def test_metric_helpers_handle_malformed_calls_listings_and_retry_order(self):
        required = ("python", ("-m", "unittest"), ".")
        malformed = {
            "tool": "run_command",
            "arguments": {"command": "python", "args": None},
        }
        self.assertFalse(harness._matches_required_test(malformed, required))
        self.assertEqual(
            harness._list_file_candidates(
                "工作目录 src/orders 的内容：\n[f] shipping.py  (195 字节)\n[d] ignored",
                "src/orders",
            ),
            {"src/orders/shipping.py"},
        )

        failed_test = {
            "tool": "run_command",
            "arguments": {"command": "python", "args": ["-m", "unittest"]},
            "exit_code": 1,
            "result": "Exit code: 1",
        }
        mutation = {
            "tool": "apply_patch",
            "arguments": {},
            "exit_code": None,
            "result": "patched",
        }
        passed_test = {
            "tool": "run_command",
            "arguments": {"command": "python", "args": ["-m", "unittest"]},
            "exit_code": 0,
            "result": "Exit code: 0",
        }
        self.assertTrue(
            harness._has_required_test_retry_cycle(
                [failed_test, mutation, passed_test], required
            )
        )
        self.assertFalse(
            harness._has_required_test_retry_cycle(
                [mutation, failed_test, passed_test], required
            )
        )

    def test_provider_server_errors_retry_and_nonzero_child_is_infrastructure(self):
        self.assertEqual(
            harness._runtime_error_kind("InternalServerError: Error code: 500"),
            "provider",
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=9, stdout=b"{}", stderr=b"fatal child error"
        )
        with patch.object(harness.subprocess, "run", return_value=completed):
            result = harness._run_child_subprocess("hidden_single_file_bug", Path.cwd())

        self.assertTrue(result["infrastructure_failure"])
        self.assertEqual(result["infrastructure_kind"], "harness")
        self.assertEqual(result["primary_failure"], "INFRASTRUCTURE")
        self.assertEqual(result["process_exit_code"], 9)
        self.assertEqual(result["metrics"]["prompt_tokens"], 0)
        self.assertEqual(result["metrics"]["changed_files"], [])
        self.assertIn("task_flags", result["metrics"])


if __name__ == "__main__":
    unittest.main()
