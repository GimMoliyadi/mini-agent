"""Autorecovery must make progress on checkpointed playbooks without a manual plan."""

import json

from langchain_core.messages import AIMessage, ToolMessage

from mini_agent.recovery import decide_recovery, recovery_message, work_signature


def _pair(name, ident, *, mutation=False, args=None):
    content = "success"
    if name == "run_command":
        content = "Exit code: 0\nTimed out: false\nOutput limit exceeded: false\nSTDOUT:\n2 passed in 0.01s"
    elif name == "review_code_changes":
        content = json.dumps({"changed_files": ["calc.py"], "findings": [], "truncated": False})
    return [
        AIMessage(content="", tool_calls=[{
            "id": ident, "name": name, "args": args or {}, "type": "tool_call",
        }]),
        ToolMessage(
            tool_call_id=ident, name=name, content=content, status="success",
            artifact={"status": "success", "mutation": mutation},
        ),
    ]


def _state(*messages):
    return {
        "status": "incomplete", "task_start": 0, "messages": list(messages),
        "plan": None, "contract": None,
        "development_workflow": {
            "goal": "Fix a failing Python function", "profile": "general",
            "require_browser": False, "require_migration": False,
            "deliver_pr": False,
        },
    }


def test_incomplete_workflow_without_manual_plan_is_recoverable():
    state = _state(*_pair("read_file", "inspect"))
    decision = decide_recovery(state, ())
    assert decision.retry
    assert decision.category == "workflow_progress"
    assert "development_workflow_status" in recovery_message(decision.category)


def test_successful_stage_transition_counts_as_progress():
    read = _pair("read_file", "inspect")
    edited = _pair("write_file", "write", mutation=True, args={"path": "calc.py"})
    testing = _pair(
        "run_command", "test",
        args={"command": "python", "args": ["-m", "pytest", "-q"]},
    )
    initial = _state(*read)
    implemented = _state(*read, *edited)
    tested = _state(*read, *edited, *testing)
    assert work_signature(initial) != work_signature(implemented)
    assert work_signature(implemented) != work_signature(tested)
    assert decide_recovery(tested, ()).category == "workflow_progress"


def test_completed_workflow_and_pending_checkpoint_dont_retry():
    start = _state(*_pair("read_file", "inspect"))
    assert not decide_recovery(start, ("tools",)).retry
    completed = _state(
        *_pair("read_file", "inspect"),
        *_pair("write_file", "write", mutation=True),
        *_pair(
            "run_command", "test",
            args={"command": "python", "args": ["-m", "pytest"]},
        ),
        *_pair("review_code_changes", "review"),
    )
    assert not decide_recovery(completed, ()).retry


def test_incomplete_task_is_still_conservatively_bounded():
    # Regression for non-workflow requests: no plan, no failures, no contract
    # should not trigger useless automatic model calls.
    plain = {"status": "incomplete", "messages": [], "task_start": 0,
             "plan": None, "contract": None, "development_workflow": None}
    assert not decide_recovery(plain, ()).retry
