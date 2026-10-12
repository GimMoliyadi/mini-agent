"""Independent verifier regressions, without an agent or provider."""

import pytest
from langchain_core.messages import ToolMessage

from mini_agent.verification import verification_facts
from mini_agent.verifier import (
    CodingTaskContract, TestCommand as RequiredCommand, TaskState, TaskStatus,
    parse_test_result, snapshot_workspace, verify_contract,
)
from mini_agent.tools.command import run_command


@pytest.mark.parametrize(("output", "expected"), [
    ("Ran 1 test in 0.001s\n\nOK", "PASS"),
    ("Ran 0 tests in 0.001s\n\nOK", "UNKNOWN"),
    ("Ran 1 test in 0.001s\n\nFAILED (failures=1)", "FAIL"),
    ("Ran 1 test in 0.001s\n\nOK (skipped=1)", "UNKNOWN"),
    ("2 passed in 0.01s", "PASS"),
    ("1 skipped in 0.01s", "UNKNOWN"),
    ("output was truncated", "UNKNOWN"),
])
def test_results_require_real_nonempty_test_summary(output, expected):
    text = f"Exit code: {1 if expected == 'FAIL' else 0}\nTimed out: false\nOutput limit exceeded: false\nSTDOUT:\n{output}\nSTDERR:\n<empty>"
    assert parse_test_result(text)["status"] == expected


def fixture(tmp_path):
    (tmp_path / "source.py").write_text("x=1\n")
    (tmp_path / "test_source.py").write_text(
        "import unittest\nclass TestSource(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1,1)\n"
    )
    contract = CodingTaskContract("task", "Fix source", ("source.py",),
                                  RequiredCommand("python", ("-m", "unittest", "test_source", "-q")))
    return contract, snapshot_workspace(tmp_path)


def test_verifier_executes_independent_real_test_and_rejects_test_change(tmp_path):
    contract, baseline = fixture(tmp_path)
    (tmp_path / "source.py").write_text("x=2\n")
    facts = TaskState(status=TaskStatus.FINISHED, initial_snapshot=baseline)
    passed = verify_contract(contract, tmp_path, baseline, task_state=facts, command_runner=run_command)
    assert passed["accepted"] and passed["final_test_count"] == 1
    (tmp_path / "test_source.py").write_text((tmp_path / "test_source.py").read_text() + "# tampered\n")
    rejected = verify_contract(contract, tmp_path, baseline, task_state=facts, command_runner=run_command)
    assert not rejected["accepted"]
    assert rejected["unexpected_changes"] == ["test_source.py"]


def test_no_authorized_runner_is_explicitly_unverified(tmp_path):
    contract, baseline = fixture(tmp_path)
    result = verify_contract(contract, tmp_path, baseline, agent_final_answer_present=True)
    assert not result["accepted"]
    assert result["final_test_status"] == "UNKNOWN"


def test_failed_required_test_tool_message_is_not_fresh_evidence(tmp_path):
    contract, baseline = fixture(tmp_path)
    message = ToolMessage(
        content=(
            "Exit code: 0\nTimed out: false\nOutput limit exceeded: false\n"
            "STDOUT:\nRan 1 test in 0.001s\n\nOK\nSTDERR:\n<empty>"
        ),
        name="run_command",
        tool_call_id="test-call",
        status="error",
        artifact={"command": contract.test_command.as_dict(), "snapshot": baseline},
    )

    facts = verification_facts({
        "initial_snapshot": baseline,
        "contract": contract.as_dict(),
        "task_start": 0,
        "messages": [message],
    })

    assert facts.last_test_status == "UNKNOWN"
    assert facts.last_successful_exact_required_test_seq is None
    assert facts.verified_snapshot is None


def test_snapshots_ignore_runtime_and_dependency_directories(tmp_path):
    for name in (".git", ".venv", "node_modules", ".mini-agent"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "state").write_text("ignored")
    (tmp_path / "source.py").write_text("tracked")
    assert set(snapshot_workspace(tmp_path)) == {"source.py"}
    (tmp_path / "important.py").write_text("new")
    assert set(snapshot_workspace(tmp_path)) == {"source.py", "important.py"}
