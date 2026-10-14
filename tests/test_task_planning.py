"""Planning through real LangChain tool calls and LangGraph checkpoints."""

import pytest

from helpers import ScriptedModel, call
from mini_agent.planning import build_plan_tools
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.fixture
def settings(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings(workspace=workspace, state_dir=tmp_path / "state", approval_mode="ALLOW")


def test_plan_state_survives_new_application_and_resets_for_new_task(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect project", "Fix tests"]),
        call("update_task_step", step_id=1, status="in_progress"),
        call("update_task_step", step_id=1, status="completed", note="Inspected files"),
        call("get_task_plan"),
        "Inspection complete; tests remain pending.",
    ])) as app:
        first = app.run("Inspect and fix", thread_id="plan-session")
        assert first["status"] == "incomplete", first
        assert first["plan"]["completed"] == 1
        assert first["plan"]["total"] == 2
        assert first["plan"]["steps"][0]["note"] == "Inspected files"
    with AgentApplication(settings, model=ScriptedModel(replies=["New task done"])) as app:
        restored = app.run(None, thread_id="plan-session")
        assert restored["plan"] == first["plan"]
        assert app.session_info("plan-session")["plan"]["completed"] == 1
        second = app.run("Do an unrelated task", thread_id="plan-session")
        assert second["status"] == "completed"
        assert second["plan"] is None


def test_plan_revision_after_blocked_step(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect", "Implement"]),
        call("update_task_step", step_id=1, status="in_progress"),
        call("update_task_step", step_id=1, status="blocked", note="Missing dependency"),
        call("set_task_plan", steps=["Resolve dependency", "Inspect", "Implement"], reason="Dependency blocked inspection"),
        "Plan revised.",
    ])) as app:
        result = app.run("Repair project")
    assert result["status"] == "incomplete", result
    assert result["plan"]["revision"] == 2
    assert result["plan"]["total"] == 3
    assert all(item["status"] == "pending" for item in result["plan"]["steps"])


def test_invalid_step_transition_fails_without_false_completion(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect", "Fix"]),
        call("update_task_step", step_id=1, status="completed"),
        "Could not verify the step.",
    ])) as app:
        result = app.run("Plan and work")
    assert result["status"] == "incomplete", result
    assert result["plan"]["completed"] == 0
    assert any(e.get("event") == "tool" and e.get("name") == "update_task_step" and e.get("status") == "error" for e in result["trace"])


def test_parallel_plan_writes_are_rejected(settings):
    batch = call("set_task_plan", steps=["Inspect", "Fix"])
    batch.tool_calls.append({
        "name": "update_task_step",
        "args": {"step_id": 1, "status": "in_progress"},
        "id": "plan-update-2", "type": "tool_call",
    })
    with AgentApplication(settings, model=ScriptedModel(replies=[batch])) as app:
        result = app.run("Parallel plan writes")
    assert result["status"] == "failed"
    assert "计划更新" in result["error"]
    assert result["plan"] is None


def test_plan_tools_are_not_external_mutations():
    tools = build_plan_tools()
    assert {t.name for t in tools} == {"set_task_plan", "update_task_step", "get_task_plan"}
    assert all(t.metadata["risk"] == "read" and not t.metadata["mutation"] for t in tools)
