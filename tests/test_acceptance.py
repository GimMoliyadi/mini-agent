"""Phase 14 deterministic Coding Task Contract and verifier tests."""

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance  # noqa: E402
import tools  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "coding_workspace"


def make_contract() -> acceptance.CodingTaskContract:
    return acceptance.CodingTaskContract.from_dict(
        {
            "task_id": "calculator_fix",
            "instruction": "修复 calculator.py，让测试通过",
            "allowed_paths": ["calculator.py"],
            "test_command": {
                "command": "python",
                "args": ["-m", "unittest", "test_calculator", "-q"],
                "cwd": ".",
            },
            "require_test_pass": True,
        }
    )


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "coding_workspace"
        shutil.copytree(FIXTURE, self.workspace)
        self.contract = make_contract()
        self.before = acceptance.snapshot_workspace(self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def verify(self, **kwargs):
        task_state = kwargs.pop(
            "task_state",
            acceptance.TaskState(
                status=acceptance.TaskStatus.FINISHED,
                event_seq=2,
                last_mutation_event_seq=1,
                last_successful_exact_required_test_seq=2,
                initial_snapshot=self.before,
            ),
        )
        return acceptance.verify_contract(
            self.contract,
            self.workspace,
            self.before,
            task_state=task_state,
            agent_final_answer_present=kwargs.pop("final", True),
            agent_ran_required_test=kwargs.pop("agent_test", True),
            **kwargs,
        )

    def fix_calculator(self):
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
            encoding="utf-8",
        )

    def test_contract_loads_and_round_trips(self):
        restored = acceptance.CodingTaskContract.from_dict(self.contract.as_dict())
        self.assertEqual(restored, self.contract)
        self.assertEqual(restored.allowed_paths, ("calculator.py",))

    def test_contract_loads_from_json_file(self):
        path = self.workspace / "contract.json"
        path.write_text(json.dumps(self.contract.as_dict()), encoding="utf-8")
        self.assertEqual(acceptance.load_contract(path), self.contract)

    def test_contract_rejects_unsafe_required_command(self):
        value = self.contract.as_dict()
        value["test_command"] = {"command": "python", "args": ["-c", "print(1)"]}
        with self.assertRaises(tools.CommandPolicyError):
            acceptance.CodingTaskContract.from_dict(value)

    def test_mock_a_allowed_change_and_final_test_pass_are_accepted(self):
        self.fix_calculator()
        result = self.verify()
        self.assertTrue(result["accepted"])
        self.assertTrue(result["artifact_passed"])
        self.assertTrue(result["interaction_completed"])
        self.assertEqual(result["changed_files"], ["calculator.py"])
        self.assertEqual(result["unexpected_changes"], [])
        self.assertEqual(result["final_test_exit_code"], 0)
        self.assertTrue(result["final_test_passed"])

    def test_mock_b_test_passes_but_test_file_change_is_rejected(self):
        (self.workspace / "test_calculator.py").write_text(
            "import unittest\n\nfrom calculator import add, subtract\n\n\nclass CalculatorTests(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), -1)\n\n"
            "    def test_subtract(self):\n        self.assertEqual(subtract(5, 3), 8)\n",
            encoding="utf-8",
        )
        result = self.verify()
        self.assertFalse(result["accepted"])
        self.assertFalse(result["artifact_passed"])
        self.assertTrue(result["interaction_completed"])
        self.assertEqual(result["final_test_exit_code"], 0)
        self.assertEqual(result["unexpected_changes"], ["test_calculator.py"])
        self.assertIn("unexpected file changed: test_calculator.py", result["reasons"])

    def test_mock_c_final_answer_does_not_override_failing_final_test(self):
        result = self.verify()
        self.assertFalse(result["accepted"])
        self.assertFalse(result["artifact_passed"])
        self.assertTrue(result["agent_final_answer_present"])
        self.assertNotEqual(result["final_test_exit_code"], 0)
        self.assertIn("final_test_failed", result["reasons"])

    def test_mock_d_artifact_passes_but_missing_final_is_not_accepted(self):
        self.fix_calculator()
        result = self.verify(
            task_state=acceptance.TaskState(initial_snapshot=self.before),
            max_steps_reached=True,
        )
        self.assertTrue(result["artifact_passed"])
        self.assertFalse(result["interaction_completed"])
        self.assertFalse(result["accepted"])

    def test_mock_d_final_test_catches_regression_after_agent_test(self):
        self.fix_calculator()
        (self.workspace / "calculator.py").write_text(
            "def add(a, b):\n    return a - b\n\n\ndef subtract(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        result = self.verify()
        self.assertFalse(result["accepted"])
        self.assertIn("final_test_failed", result["reasons"])

    def test_snapshot_detects_new_and_deleted_files(self):
        (self.workspace / "new.txt").write_text("new", encoding="utf-8")
        (self.workspace / "test_calculator.py").unlink()
        result = self.verify()
        self.assertEqual(result["changed_files"], ["new.txt", "test_calculator.py"])
        self.assertEqual(result["unexpected_changes"], result["changed_files"])

    def test_generated_python_cache_is_not_reported_as_agent_change(self):
        cache = self.workspace / "__pycache__"
        cache.mkdir()
        (cache / "test_calculator.cpython-311.pyc").write_bytes(b"cache")
        self.assertEqual(acceptance.snapshot_workspace(self.workspace), self.before)

    def make_pytest_cache(self):
        cache = self.workspace / ".pytest_cache"
        node_ids = cache / "v" / "cache"
        node_ids.mkdir(parents=True)
        (cache / "README.md").write_text("pytest cache guide", encoding="utf-8")
        (cache / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172", encoding="utf-8")
        (node_ids / "nodeids").write_text('["test_calculator.py"]', encoding="utf-8")

    def test_pytest_cache_is_not_reported_as_agent_change(self):
        self.make_pytest_cache()
        self.assertEqual(acceptance.snapshot_workspace(self.workspace), self.before)

    def test_pytest_cache_does_not_mask_a_real_unexpected_change(self):
        self.make_pytest_cache()
        (self.workspace / "unexpected.txt").write_text("outside contract", encoding="utf-8")
        result = self.verify()
        self.assertEqual(result["unexpected_changes"], ["unexpected.txt"])
        self.assertNotIn(".pytest_cache/README.md", result["changed_files"])
        self.assertNotIn(".pytest_cache/v/cache/nodeids", result["changed_files"])

    def test_allowed_source_plus_pytest_cache_reports_only_the_source(self):
        self.fix_calculator()
        self.make_pytest_cache()
        result = self.verify()
        self.assertEqual(result["changed_files"], ["calculator.py"])
        self.assertEqual(result["unexpected_changes"], [])
        self.assertTrue(result["artifact_passed"])
        self.assertTrue(result["accepted"])

    def test_unlisted_generated_files_still_count_as_changes(self):
        (self.workspace / ".coverage").write_bytes(b"coverage")
        mypy_cache = self.workspace / ".mypy_cache"
        mypy_cache.mkdir()
        (mypy_cache / "cache.json").write_text("{}", encoding="utf-8")
        result = self.verify()
        self.assertEqual(
            result["changed_files"],
            [".coverage", ".mypy_cache/cache.json"],
        )

    def test_root_level_file_keeps_its_name_outside_a_cache_directory(self):
        (self.workspace / "README.md").write_text("project readme", encoding="utf-8")
        self.assertEqual(
            acceptance.changed_files(self.before, acceptance.snapshot_workspace(self.workspace)),
            ["README.md"],
        )

    def test_legacy_caller_without_task_state_keeps_final_answer_rule(self):
        """Phase 18-20 eval harnesses never drive the finish protocol."""
        self.fix_calculator()
        result = acceptance.verify_contract(
            self.contract,
            self.workspace,
            self.before,
            agent_final_answer_present=True,
            agent_ran_required_test=True,
        )
        self.assertTrue(result["interaction_completed"])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["agent_self_verified"])
        self.assertEqual(result["task_status"], None)
        self.assertNotIn("finish_task_not_accepted", result["reasons"])

        missing_final = acceptance.verify_contract(
            self.contract,
            self.workspace,
            self.before,
            agent_final_answer_present=False,
        )
        self.assertFalse(missing_final["interaction_completed"])
        self.assertIn("agent_final_answer_missing", missing_final["reasons"])
        self.assertNotIn("finish_task_not_accepted", missing_final["reasons"])

    def test_finish_at_the_step_limit_is_not_counted_as_a_limit_failure(self):
        self.fix_calculator()
        result = self.verify(max_steps_reached=True)
        self.assertTrue(result["interaction_completed"])
        self.assertTrue(result["accepted"])
        self.assertNotIn("max_agent_steps_reached", result["reasons"])

    def test_verifier_uses_fixed_command_and_does_not_call_an_llm(self):
        self.fix_calculator()
        with patch.object(tools, "run_command", wraps=tools.run_command) as run:
            result = self.verify()
        run.assert_called_once_with(
            "python",
            ["-m", "unittest", "test_calculator", "-q"],
            ".",
            workspace=self.workspace.resolve(),
        )
        self.assertTrue(result["accepted"])


if __name__ == "__main__":
    unittest.main()
