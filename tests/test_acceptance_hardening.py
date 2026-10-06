"""验收计数、快照新鲜度及执行授权的隔离回归测试。"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance  # noqa: E402
import tools  # noqa: E402


UNITTEST_PASS = "Ran 2 tests in 0.001s\n\nOK\n"


def command_result(output: str, exit_code: int = 0) -> str:
    return (
        f"Command: python -m unittest\nExit code: {exit_code}\nTimed out: false\n"
        f"STDOUT:\n<empty>\nSTDERR:\n{output}"
    )


class TestResultParsingTests(unittest.TestCase):
    def test_unittest_success_counts_executed_tests(self):
        parsed = acceptance.parse_test_result(command_result(UNITTEST_PASS))
        self.assertEqual(parsed["status"], "PASS")
        self.assertEqual(parsed["test_count"], 2)
        self.assertEqual(parsed["collected_count"], 2)
        self.assertEqual(parsed["skipped_count"], 0)
        self.assertIsNone(parsed["reason"])

    def test_windows_line_endings_and_ansi_output_are_supported(self):
        unittest_result = command_result(UNITTEST_PASS).replace("\n", "\r\n")
        pytest_result = command_result("\x1b[32m=== 2 passed in 0.12s ===\x1b[0m\n")
        for result in (unittest_result, pytest_result):
            with self.subTest(result=result):
                parsed = acceptance.parse_test_result(result)
                self.assertEqual((parsed["status"], parsed["test_count"]), ("PASS", 2))

    def test_unittest_zero_and_all_skipped_are_unknown(self):
        for output, count, reason in (
            ("Ran 0 tests in 0.000s\n\nOK\n", 0, "no_tests_executed"),
            ("Ran 2 tests in 0.001s\n\nOK (skipped=2)\n", 0, "all_tests_skipped"),
            ("Ran 2 tests in 0.001s\n\nOK (skipped=1)\n", 1, None),
        ):
            with self.subTest(output=output):
                parsed = acceptance.parse_test_result(command_result(output))
                self.assertEqual(parsed["status"], "PASS" if count else "UNKNOWN")
                self.assertEqual(parsed["test_count"], count)
                self.assertEqual(parsed["reason"], reason)

    def test_pytest_success_failure_and_skip_summaries(self):
        cases = (
            ("=== 2 passed in 0.12s ===", 0, "PASS", 2),
            ("1 passed, 2 skipped in 0.12s", 0, "PASS", 1),
            ("=== 1 failed, 2 passed in 0.12s ===", 1, "FAIL", 3),
            ("=== 2 skipped in 0.12s ===", 0, "UNKNOWN", 0),
            ("no tests ran in 0.01s", 5, "UNKNOWN", 0),
            ("=== 4 deselected in 0.01s ===", 5, "UNKNOWN", 0),
        )
        for output, exit_code, status, count in cases:
            with self.subTest(output=output):
                parsed = acceptance.parse_test_result(command_result(output, exit_code))
                self.assertEqual((parsed["status"], parsed["test_count"]), (status, count))

    def test_failure_cannot_be_overridden_by_zero_exit_code(self):
        parsed = acceptance.parse_test_result(command_result(
            "Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n"
        ))
        self.assertEqual(parsed["status"], "FAIL")

    def test_unknown_output_missing_exit_and_timeout_cannot_pass(self):
        for result in (
            "Exit code: 0\nTimed out: false\nAll good!",
            UNITTEST_PASS,
            command_result(UNITTEST_PASS).replace("Timed out: false", "Timed out: true"),
            command_result(UNITTEST_PASS).replace("Ran 2", "Ran 0"),
        ):
            with self.subTest(result=result):
                self.assertEqual(acceptance.parse_test_result(result)["status"], "UNKNOWN")

    def test_command_output_cannot_supply_missing_exit_metadata(self):
        result = f"Exit code: None\nSTDOUT:\nExit code: 0\nSTDERR:\n{UNITTEST_PASS}"
        self.assertEqual(acceptance.parse_test_result(result)["status"], "UNKNOWN")

    def test_output_limit_exceeded_invalidates_a_visible_success_summary(self):
        result = command_result(UNITTEST_PASS).replace(
            "Timed out: false", "Timed out: false\nOutput limit exceeded: true"
        )
        parsed = acceptance.parse_test_result(result)
        self.assertEqual(parsed["status"], "UNKNOWN")
        self.assertEqual(parsed["reason"], "test_output_limit_exceeded")


class AcceptanceHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "calculator.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.workspace / "test_case.py").write_text(
            "import unittest\n\nclass ExampleTests(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(1 + 1, 2)\n",
            encoding="utf-8",
        )
        self.contract = acceptance.CodingTaskContract.from_dict({
            "task_id": "hardening",
            "instruction": "修改 calculator.py 并执行测试",
            "allowed_paths": ["calculator.py"],
            "test_command": {"command": "python", "args": ["-m", "unittest", "test_case", "-q"], "cwd": "."},
        })
        self.before = acceptance.snapshot_workspace(self.workspace)
        self.state = acceptance.TaskState(initial_snapshot=self.before)

    def tearDown(self):
        self.temp_dir.cleanup()

    def verify(self, **kwargs):
        return acceptance.verify_contract(
            self.contract, self.workspace, self.before,
            agent_final_answer_present=True, **kwargs,
        )

    def record_pass(self):
        acceptance.record_required_test(
            self.state, command_result(UNITTEST_PASS), self.workspace, self.state.next_event()
        )

    def test_real_nonempty_unittest_is_accepted(self):
        result = self.verify(command_runner=tools.run_command)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["final_test_status"], "PASS")
        self.assertEqual(result["final_test_count"], 1)

    def test_real_zero_tests_are_not_accepted(self):
        (self.workspace / "test_case.py").write_text("import unittest\n", encoding="utf-8")
        self.before = acceptance.snapshot_workspace(self.workspace)
        result = self.verify(command_runner=tools.run_command)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_test_status"], "UNKNOWN")
        self.assertEqual(result["final_test_count"], 0)
        self.assertEqual(result["final_test_unverified_reason"], "no_tests_executed")
        self.assertNotIn("final_test_failed", result["reasons"])

    def test_real_all_skipped_tests_are_not_accepted(self):
        path = self.workspace / "test_case.py"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "    def test_value", '    @unittest.skip("fixture")\n    def test_value'
        ), encoding="utf-8")
        self.before = acceptance.snapshot_workspace(self.workspace)
        result = self.verify(command_runner=tools.run_command)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_test_status"], "UNKNOWN")
        self.assertEqual(result["final_test_count"], 0)
        self.assertEqual(result["final_test_skipped_count"], 1)
        self.assertEqual(result["final_test_unverified_reason"], "all_tests_skipped")

    def test_final_snapshot_includes_real_test_side_effect(self):
        (self.workspace / "test_case.py").write_text(
            "from pathlib import Path\nimport unittest\n\nclass SideEffectTests(unittest.TestCase):\n"
            "    def test_writes(self):\n        Path('forbidden.txt').write_text('side effect')\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.before = acceptance.snapshot_workspace(self.workspace)
        result = self.verify(command_runner=tools.run_command)
        self.assertTrue(result["final_test_passed"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["unexpected_changes"], ["forbidden.txt"])

    def test_no_runner_does_not_execute_tests(self):
        with patch.object(tools, "run_command") as implicit_runner:
            result = self.verify()
        implicit_runner.assert_not_called()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_test_status"], "UNKNOWN")
        self.assertEqual(result["final_test_unverified_reason"], "command_runner_missing")

    def test_cancelled_or_runtime_failure_does_not_start_verification(self):
        cases = (
            (acceptance.TaskStatus.CANCELLED, None, None, "task_cancelled"),
            (acceptance.TaskStatus.FINISHED, "stopped", None, "runtime_exception"),
            (acceptance.TaskStatus.ERROR, None, "provider lost", "unresolved_runtime_error"),
        )
        for status, exception, error, reason in cases:
            with self.subTest(status=status):
                state = acceptance.TaskState(status=status, unresolved_runtime_error=error)
                runner = Mock(return_value=command_result(UNITTEST_PASS))
                result = self.verify(task_state=state, runtime_exception=exception, command_runner=runner)
                runner.assert_not_called()
                self.assertFalse(result["accepted"])
                self.assertEqual(result["final_test_unverified_reason"], reason)

    def test_runner_exception_is_unknown_not_failed(self):
        runner = Mock(side_effect=RuntimeError("denied"))
        result = self.verify(command_runner=runner)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["final_test_status"], "UNKNOWN")
        self.assertNotIn("final_test_failed", result["reasons"])
        self.assertIn("final_test_error: RuntimeError: denied", result["reasons"])

    def test_external_allowed_edit_invalidates_pass_until_retested(self):
        self.record_pass()
        self.assertTrue(acceptance.evaluate_finish_request(self.contract, self.state, self.workspace).accepted)
        (self.workspace / "calculator.py").write_text("VALUE = 2\n", encoding="utf-8")
        gate = acceptance.evaluate_finish_request(self.contract, self.state, self.workspace)
        self.assertFalse(gate.accepted)
        self.assertIn("successful_exact_required_test_stale", gate.reasons)
        self.record_pass()
        self.assertTrue(acceptance.evaluate_finish_request(self.contract, self.state, self.workspace).accepted)

    def test_unknown_later_test_clears_pass_without_claiming_failure(self):
        self.record_pass()
        acceptance.record_required_test(self.state, "Exit code: 0", self.workspace, self.state.next_event())
        self.assertEqual(self.state.last_test_status, "UNKNOWN")
        self.assertIsNone(self.state.last_test_count)
        self.assertIsNone(self.state.last_successful_exact_required_test_seq)
        self.assertIsNone(self.state.verified_snapshot)
        gate = acceptance.evaluate_finish_request(self.contract, self.state, self.workspace)
        self.assertFalse(gate.accepted)
        self.assertIn("required_test_result_unknown", gate.reasons)

    def test_failed_later_test_clears_snapshot(self):
        self.record_pass()
        acceptance.record_required_test(
            self.state, command_result("Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n", 1),
            self.workspace, self.state.next_event(),
        )
        self.assertEqual(self.state.last_test_status, "FAIL")
        self.assertEqual(self.state.last_test_count, 2)
        self.assertIsNone(self.state.verified_snapshot)
        self.assertIsNone(self.state.last_successful_exact_required_test_seq)

    def test_recorded_snapshot_is_after_the_test_side_effect(self):
        (self.workspace / "calculator.py").write_text("VALUE = 3\n", encoding="utf-8")
        self.record_pass()
        self.assertEqual(self.state.verified_snapshot, acceptance.snapshot_workspace(self.workspace))

    def test_old_session_pass_without_snapshot_is_not_fresh(self):
        old_state = acceptance.TaskState.from_dict({
            "event_seq": 2, "last_mutation_event_seq": 1,
            "last_successful_exact_required_test_seq": 2, "initial_snapshot": self.before,
        })
        gate = acceptance.evaluate_finish_request(self.contract, old_state, self.workspace)
        self.assertFalse(gate.accepted)
        self.assertIsNone(old_state.verified_snapshot)
        self.assertIn("successful_exact_required_test_snapshot_missing", gate.reasons)
        result = self.verify(task_state=old_state, command_runner=Mock(return_value=command_result(UNITTEST_PASS)))
        self.assertFalse(result["agent_self_verified"])

    def test_saved_pass_and_count_without_snapshot_still_requires_reverification(self):
        state = acceptance.TaskState.from_dict({
            "event_seq": 1, "last_successful_exact_required_test_seq": 1,
            "initial_snapshot": self.before, "last_test_status": "PASS", "last_test_count": 2,
        })
        gate = acceptance.evaluate_finish_request(self.contract, state, self.workspace)
        self.assertFalse(gate.accepted)
        self.assertIn("successful_exact_required_test_snapshot_missing", gate.reasons)

    def test_new_test_fields_and_cancelled_status_round_trip(self):
        self.record_pass()
        self.state.status = acceptance.TaskStatus.CANCELLED
        restored = acceptance.TaskState.from_dict(self.state.as_dict())
        self.assertEqual(restored, self.state)
        self.assertIsNot(restored.verified_snapshot, self.state.verified_snapshot)
        gate = acceptance.evaluate_finish_request(self.contract, restored, self.workspace)
        self.assertFalse(gate.accepted)
        self.assertIn("task_cancelled", gate.reasons)

    def test_saved_test_fields_validate_types(self):
        for name, value in (
            ("verified_snapshot", []), ("verified_snapshot", {"a": 1}),
            ("last_test_count", True), ("last_test_count", -1), ("last_test_count", "2"),
            ("last_test_status", "OK"), ("last_test_status", []),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    acceptance.TaskState.from_dict({name: value})

    def test_cache_exemptions_do_not_hide_arbitrary_files(self):
        for relative in ("outside.pyc", "__pycache__/payload.txt", ".pytest_cache/hidden.py"):
            path = self.workspace / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text("not a generated cache", encoding="utf-8")
        self.assertEqual(acceptance.changed_files(self.before, acceptance.snapshot_workspace(self.workspace)),
                         [".pytest_cache/hidden.py", "__pycache__/payload.txt", "outside.pyc"])

    def test_snapshot_rejects_excessive_bytes_before_reading(self):
        from runtime_guards import BudgetExceeded
        with patch.object(acceptance, "MAX_SNAPSHOT_BYTES", 1):
            with self.assertRaises(BudgetExceeded):
                acceptance.snapshot_workspace(self.workspace)

    def test_snapshot_rejects_excessive_directory_entries(self):
        from runtime_guards import BudgetExceeded
        with patch.object(acceptance, "MAX_SNAPSHOT_ENTRIES", 1):
            with self.assertRaises(BudgetExceeded):
                acceptance.snapshot_workspace(self.workspace)

    def test_visible_summary_survives_bounded_command_rendering(self):
        stdout = "progress line\n" * 500
        stderr = "individual test ... ok\n" * 200 + "Ran 200 tests in 0.100s\n\nOK\n"
        result = tools._format_command_result("python", self.workspace, 0, False, stdout, stderr)
        observation = acceptance.parse_test_result(result)
        self.assertEqual(observation["status"], "PASS")
        self.assertEqual(observation["test_count"], 200)
        self.assertIn("output truncated", result)

    def test_verifier_budget_exception_is_not_downgraded_to_unknown(self):
        from runtime_guards import BudgetExceeded
        def exhausted(*args, **kwargs):
            raise BudgetExceeded("budget used")
        with self.assertRaises(BudgetExceeded):
            self.verify(command_runner=exhausted)

    def test_snapshot_normal_file_matches_content_hash(self):
        import hashlib
        target = self.workspace / "calculator.py"
        snapshot = acceptance.snapshot_workspace(self.workspace)
        self.assertEqual(snapshot["calculator.py"], hashlib.sha256(target.read_bytes()).hexdigest())

    def test_snapshot_rejects_shared_hardlinks_instead_of_trusting_mtime(self):
        import os
        with tempfile.TemporaryDirectory() as external:
            target = Path(external) / "owned-probe.txt"
            target.write_text("old", encoding="utf-8")
            try:
                os.link(target, self.workspace / "linked.txt")
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"测试环境不支持硬链接：{error}")
            with self.assertRaisesRegex(PermissionError, "共享硬链接"):
                acceptance.snapshot_workspace(self.workspace)


if __name__ == "__main__":
    unittest.main()
