"""Phase 21 deterministic finish protocol tests."""

from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance  # noqa: E402
import main  # noqa: E402
import tools  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "coding_workspace"
FIXED_CALCULATOR = (
    "def add(a, b):\n    return a + b\n\n\n"
    "def subtract(a, b):\n    return a - b\n"
)
REQUIRED_ARGS = ["-m", "unittest", "test_calculator", "-q"]


def tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def tool_reply(call_id: str, name: str, arguments: dict) -> main.ModelReply:
    return main.ModelReply(
        SimpleNamespace(content=None, tool_calls=[tool_call(call_id, name, arguments)]),
        "tool_calls",
        10,
        2,
        12,
    )


def multi_tool_reply(*calls) -> main.ModelReply:
    return main.ModelReply(
        SimpleNamespace(content=None, tool_calls=list(calls)), "tool_calls", 10, 2, 12
    )


def final_reply(content: str) -> main.ModelReply:
    return main.ModelReply(
        SimpleNamespace(content=content, tool_calls=None), "stop", 10, 2, 12
    )


class FinishProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "coding_workspace"
        shutil.copytree(FIXTURE, self.workspace)
        self.original_tools_workspace = tools.WORKSPACE_DIR
        self.original_main_workspace = main.WORKSPACE_DIR
        tools.WORKSPACE_DIR = self.workspace
        main.WORKSPACE_DIR = self.workspace
        self.initial_snapshot = acceptance.snapshot_workspace(self.workspace)
        self.contract = acceptance.CodingTaskContract.from_dict(
            {
                "task_id": "calculator_fix",
                "instruction": "Fix calculator.py so its test passes.",
                "allowed_paths": ["calculator.py"],
                "test_command": {
                    "command": "python",
                    "args": REQUIRED_ARGS,
                    "cwd": ".",
                },
                "require_test_pass": True,
            }
        )

    def tearDown(self):
        tools.WORKSPACE_DIR = self.original_tools_workspace
        main.WORKSPACE_DIR = self.original_main_workspace
        self.temp_dir.cleanup()

    def state(self) -> acceptance.TaskState:
        return acceptance.TaskState(initial_snapshot=dict(self.initial_snapshot))

    def required_test(self) -> tuple[str, tuple[str, ...], str]:
        return (
            self.contract.test_command.command,
            self.contract.test_command.args,
            self.contract.test_command.cwd,
        )

    def write_fixed_calculator(self) -> None:
        (self.workspace / "calculator.py").write_text(
            FIXED_CALCULATOR, encoding="utf-8"
        )

    def run_loop(
        self,
        first: main.ModelReply,
        following: list[main.ModelReply],
        state: acceptance.TaskState,
        *,
        max_steps: int | None = None,
    ) -> tuple[list[dict], main.CodingTaskTrace]:
        messages = [{"role": "user", "content": self.contract.instruction}]
        trace = main.CodingTaskTrace()
        limit = (
            patch.object(main, "MAX_AGENT_STEPS", max_steps)
            if max_steps is not None
            else nullcontext()
        )
        with patch.object(main, "ask", side_effect=following), limit:
            main.run_agent_loop(
                None,
                "mock-model",
                messages,
                first,
                set(),
                main.always_allow,
                trace=trace,
                required_test=self.required_test(),
                contract=self.contract,
                task_state=state,
            )
        return messages, trace

    def assert_history_is_paired(self, messages: list[dict]) -> None:
        pending: list[str] = []
        for message in messages:
            if message.get("role") == "assistant":
                pending.extend(call["id"] for call in message.get("tool_calls") or [])
            elif message.get("role") == "tool":
                self.assertTrue(pending)
                self.assertEqual(message["tool_call_id"], pending.pop(0))
        self.assertEqual(pending, [])

    def test_mock_a_patch_test_finish_is_accepted(self):
        state = self.state()
        messages, trace = self.run_loop(
            tool_reply("read", "read_file", {"path": "calculator.py"}),
            [
                tool_reply(
                    "patch",
                    "write_file",
                    {"path": "calculator.py", "content": FIXED_CALCULATOR},
                ),
                tool_reply(
                    "test",
                    "run_command",
                    {"command": "python", "args": REQUIRED_ARGS},
                ),
                tool_reply(
                    "finish",
                    "finish_task",
                    {"summary": "Fixed calculator and verified the required test."},
                ),
            ],
            state,
        )

        self.assertIs(state.status, acceptance.TaskStatus.FINISHED)
        self.assertEqual(state.last_mutation_event_seq, 2)
        self.assertEqual(state.last_successful_exact_required_test_seq, 3)
        self.assertEqual(trace.finish_successes, 1)
        self.assertEqual(json.loads(messages[-1]["content"])["status"], "FINISHED")
        verdict = acceptance.verify_contract(
            self.contract,
            self.workspace,
            self.initial_snapshot,
            task_state=state,
            agent_ran_required_test=True,
        )
        self.assertTrue(verdict["artifact_passed"])
        self.assertTrue(verdict["interaction_completed"])
        self.assertTrue(verdict["agent_self_verified"])
        self.assertTrue(verdict["accepted"])

    def test_mock_b_failed_test_patch_finish_reject_then_pass_finish(self):
        state = self.state()
        messages, trace = self.run_loop(
            tool_reply(
                "failed-test",
                "run_command",
                {"command": "python", "args": REQUIRED_ARGS},
            ),
            [
                tool_reply(
                    "patch",
                    "write_file",
                    {"path": "calculator.py", "content": FIXED_CALCULATOR},
                ),
                tool_reply("finish-1", "finish_task", {"summary": "done"}),
                tool_reply(
                    "passing-test",
                    "run_command",
                    {"command": "python", "args": REQUIRED_ARGS},
                ),
                tool_reply("finish-2", "finish_task", {"summary": "done"}),
            ],
            state,
        )

        self.assertIs(state.status, acceptance.TaskStatus.FINISHED)
        self.assertEqual([item["status"] for item in state.finish_attempts], ["REJECTED", "FINISHED"])
        self.assertIn(
            "successful_exact_required_test_stale",
            state.finish_attempts[0]["reasons"],
        )
        self.assertEqual(state.finish_attempts[0]["summary"], state.finish_message)
        self.assertEqual(trace.finish_rejections, 1)
        self.assertEqual(trace.finish_successes, 1)
        self.assert_history_is_paired(messages)

    def test_mock_c_unexpected_change_rejects_finish(self):
        state = self.state()
        current_test = (self.workspace / "test_calculator.py").read_text(encoding="utf-8")
        messages, trace = self.run_loop(
            multi_tool_reply(
                tool_call(
                    "source",
                    "write_file",
                    {"path": "calculator.py", "content": FIXED_CALCULATOR},
                ),
                tool_call(
                    "forbidden",
                    "write_file",
                    {"path": "test_calculator.py", "content": current_test + "\n# changed\n"},
                ),
            ),
            [
                tool_reply(
                    "test",
                    "run_command",
                    {"command": "python", "args": REQUIRED_ARGS},
                ),
                tool_reply("finish", "finish_task", {"summary": "done"}),
            ],
            state,
            max_steps=3,
        )

        self.assertIs(state.status, acceptance.TaskStatus.LIMIT_REACHED)
        self.assertIn(
            "unexpected_change:test_calculator.py",
            state.last_finish_rejection["reasons"],
        )
        self.assertTrue(trace.max_steps_reached)
        self.assert_history_is_paired(messages)

    def test_mock_d_finish_discards_following_tool_calls(self):
        self.write_fixed_calculator()
        state = self.state()
        state.event_seq = 2
        state.last_mutation_event_seq = 1
        state.last_successful_exact_required_test_seq = 2
        message = multi_tool_reply(
            tool_call("finish", "finish_task", {"summary": "done"}),
            tool_call("read-after", "read_file", {"path": "calculator.py"}),
            tool_call(
                "test-after",
                "run_command",
                {"command": "python", "args": REQUIRED_ARGS},
            ),
        ).message
        messages: list[dict] = []
        with patch.object(main, "execute_tool_call") as execute:
            main.run_tool_round(
                messages,
                message,
                set(),
                main.always_allow,
                contract=self.contract,
                task_state=state,
            )

        execute.assert_not_called()
        self.assertIs(state.status, acceptance.TaskStatus.FINISHED)
        self.assertEqual(len(messages[0]["tool_calls"]), 1)
        self.assertEqual(messages[0]["tool_calls"][0]["id"], "finish")
        self.assertEqual(len(messages), 2)
        self.assert_history_is_paired(messages)

    def test_mock_e_rejected_finish_also_discards_following_calls(self):
        state = self.state()
        message = multi_tool_reply(
            tool_call("finish", "finish_task", {"summary": "done"}),
            tool_call("read-after", "read_file", {"path": "calculator.py"}),
        ).message
        messages: list[dict] = []
        with patch.object(main, "execute_tool_call") as execute:
            main.run_tool_round(
                messages,
                message,
                set(),
                main.always_allow,
                contract=self.contract,
                task_state=state,
            )

        execute.assert_not_called()
        self.assertIs(state.status, acceptance.TaskStatus.RUNNING)
        self.assertEqual(len(messages[0]["tool_calls"]), 1)
        self.assertEqual(json.loads(messages[-1]["content"])["status"], "REJECTED")
        self.assert_history_is_paired(messages)

    def test_mock_f_identical_finish_is_not_duplicate_blocked(self):
        state = self.state()
        self.write_fixed_calculator()
        state.event_seq = 1
        state.last_mutation_event_seq = 1
        first_finish = tool_reply("finish-1", "finish_task", {"summary": "done"}).message
        test_call = tool_reply(
            "test", "run_command", {"command": "python", "args": REQUIRED_ARGS}
        ).message
        second_finish = tool_reply("finish-2", "finish_task", {"summary": "done"}).message
        messages: list[dict] = []
        executed: set[tuple[str, str]] = set()

        main.run_tool_round(
            messages, first_finish, executed, main.always_allow,
            required_test=self.required_test(), contract=self.contract, task_state=state,
        )
        main.run_tool_round(
            messages, test_call, executed, main.always_allow,
            required_test=self.required_test(), contract=self.contract, task_state=state,
        )
        main.run_tool_round(
            messages, second_finish, executed, main.always_allow,
            required_test=self.required_test(), contract=self.contract, task_state=state,
        )

        results = [json.loads(message["content"]) for message in messages if message["role"] == "tool" and message["content"].startswith("{")]
        self.assertEqual([result["status"] for result in results], ["REJECTED", "FINISHED"])
        self.assertIs(state.status, acceptance.TaskStatus.FINISHED)

    def test_mock_g_plain_final_stays_running_until_finish_task(self):
        self.write_fixed_calculator()
        state = self.state()
        state.event_seq = 2
        state.last_mutation_event_seq = 1
        state.last_successful_exact_required_test_seq = 2
        messages, trace = self.run_loop(
            final_reply("The code is fixed."),
            [tool_reply("finish", "finish_task", {"summary": "Fixed it."})],
            state,
        )

        self.assertIs(state.status, acceptance.TaskStatus.FINISHED)
        self.assertEqual(messages[1]["content"], "The code is fixed.")
        self.assertEqual(messages[2]["content"], main.CODING_FINISH_PROTOCOL_NOTICE)
        self.assertEqual(trace.model_calls, 2)

    def test_mock_h_last_step_finish_has_priority_over_limit(self):
        self.write_fixed_calculator()
        state = self.state()
        state.event_seq = 2
        state.last_mutation_event_seq = 1
        state.last_successful_exact_required_test_seq = 2
        _, trace = self.run_loop(
            tool_reply("finish", "finish_task", {"summary": "done"}), [], state, max_steps=1
        )

        self.assertIs(state.status, acceptance.TaskStatus.FINISHED)
        self.assertFalse(trace.max_steps_reached)

    def test_mock_i_last_step_rejected_finish_records_then_limits(self):
        state = self.state()
        messages, trace = self.run_loop(
            tool_reply("finish", "finish_task", {"summary": "done"}), [], state, max_steps=1
        )

        self.assertIs(state.status, acceptance.TaskStatus.LIMIT_REACHED)
        self.assertIsNotNone(state.last_finish_rejection)
        self.assertTrue(trace.max_steps_reached)
        self.assertEqual(json.loads(messages[-1]["content"])["status"], "REJECTED")
        self.assert_history_is_paired(messages)

    def test_finish_tool_has_control_flow_kind_and_never_requests_approval(self):
        definition = tools.TOOL_REGISTRY["finish_task"]
        self.assertIs(definition.tool_kind, tools.ToolKind.CONTROL_FLOW)
        self.assertIs(definition.risk_level, tools.RiskLevel.READ_ONLY)
        approval = Mock(return_value=False)
        state = self.state()
        main.run_tool_round(
            [],
            tool_reply("finish", "finish_task", {"summary": "done"}).message,
            set(),
            approval,
            contract=self.contract,
            task_state=state,
        )
        approval.assert_not_called()

    def test_task_state_records_only_real_mutations_and_exact_test_success(self):
        state = self.state()
        original = (self.workspace / "calculator.py").read_text(encoding="utf-8")
        messages: list[dict] = []
        main.run_tool_round(
            messages,
            tool_reply("same", "write_file", {"path": "calculator.py", "content": original}).message,
            set(),
            main.always_allow,
            contract=self.contract,
            task_state=state,
        )
        self.assertEqual(state.event_seq, 1)
        self.assertIsNone(state.last_mutation_event_seq)

        main.run_tool_round(
            messages,
            tool_reply("changed", "write_file", {"path": "calculator.py", "content": FIXED_CALCULATOR}).message,
            set(),
            main.always_allow,
            contract=self.contract,
            task_state=state,
        )
        self.assertEqual(state.last_mutation_event_seq, 2)

        with patch.object(main, "execute_tool_call", return_value="Exit code: 0"):
            main.run_tool_round(
                messages,
                tool_reply(
                    "other-command",
                    "run_command",
                    {"command": "python", "args": ["-m", "unittest", "-q"]},
                ).message,
                set(),
                main.always_allow,
                required_test=self.required_test(),
                contract=self.contract,
                task_state=state,
            )
            self.assertIsNone(state.last_successful_exact_required_test_seq)
            main.run_tool_round(
                messages,
                tool_reply(
                    "required-command",
                    "run_command",
                    {"command": "python", "args": REQUIRED_ARGS},
                ).message,
                set(),
                main.always_allow,
                required_test=self.required_test(),
                contract=self.contract,
                task_state=state,
            )
        self.assertEqual(state.last_successful_exact_required_test_seq, 4)

    def test_gate_missing_contract_and_runtime_error_are_deterministic(self):
        state = self.state()
        missing = acceptance.evaluate_finish_request(None, state, self.workspace)
        self.assertFalse(missing.accepted)
        self.assertEqual(missing.reasons, ("coding_contract_missing",))

        state.unresolved_runtime_error = "APIError: provider unavailable"
        runtime_error = acceptance.evaluate_finish_request(
            self.contract, state, self.workspace
        )
        self.assertIn("unresolved_runtime_error", runtime_error.reasons)

    def test_runtime_exception_records_error_state_for_coding_task(self):
        state = self.state()
        messages = [{"role": "user", "content": self.contract.instruction}]
        trace = main.CodingTaskTrace()
        first = tool_reply("read", "read_file", {"path": "calculator.py"})

        with patch.object(main, "ask", side_effect=ConnectionError("provider lost")):
            with self.assertRaises(ConnectionError):
                main.run_agent_loop(
                    None,
                    "mock-model",
                    messages,
                    first,
                    set(),
                    main.always_allow,
                    trace=trace,
                    required_test=self.required_test(),
                    contract=self.contract,
                    task_state=state,
                )

        self.assertIs(state.status, acceptance.TaskStatus.ERROR)
        self.assertEqual(state.unresolved_runtime_error, "ConnectionError: provider lost")
        self.assertEqual(trace.task_status, acceptance.TaskStatus.ERROR.value)


if __name__ == "__main__":
    unittest.main()
