"""Real LangGraph automatic recovery of failed coding tasks, with a shared budget."""

import pytest

from helpers import ScriptedModel, call
from mini_agent.cli import build_parser
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    return Settings(workspace=root, state_dir=tmp_path / "state", approval_mode="ALLOW")


def test_agent_revisits_failing_test_and_finishes_original_plan(settings):
    model = ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect the code", "Fix and test addition"]),
        call("update_task_step", step_id=1, status="in_progress"),
        call("read_file", path="calc.py"),
        call("update_task_step", step_id=1, status="completed", note="Read implementation"),
        call("update_task_step", step_id=2, status="in_progress"),
        call("run_command", command="python", args=["-m", "unittest", "test_calc", "-q"]),
        "The test failed; I have not fixed the bug.",
        call("apply_patch", path="calc.py", old_text="a - b", new_text="a + b"),
        call("run_command", command="python", args=["-m", "unittest", "test_calc", "-q"]),
        call("update_task_step", step_id=2, status="completed", note="Test passed"),
        "The code is fixed and test verified.",
    ])
    events = []
    with AgentApplication(settings, model=model, event_callback=events.append) as app:
        result = app.run("Fix the addition bug", thread_id="autorepair", auto_recover=True)
        assert result["status"] == "completed", result
        assert result["plan"]["completed"] == 2
        assert app.session_info("autorepair")["status"] == "completed"
    assert (settings.workspace / "calc.py").read_text(encoding="utf-8").count("a + b") == 1
    assert model.cursor == 11
    assert any(e["event"] == "recovery_started" and e["category"] == "command_failure" for e in events)
    assert {e["task_id"] for e in events if "task_id" in e} == {result["task_id"]}


def test_stalled_recovery_stops_after_one_attempt(settings):
    model = ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect files", "Implement feature"]),
        "Not finished.",
        "Still not finished.",
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Make the feature", auto_recover=True, max_recovery_attempts=3)
    assert result["status"] == "incomplete"
    assert model.cursor == 3
    assert any(e["event"] == "recovery_stopped" and e["reason"] == "no_progress" for e in result["trace"])


def test_recovery_identifies_the_unresolved_tool_and_repairs_its_precondition(settings):
    model = ScriptedModel(replies=[
        call("update_task_step", step_id=1, status="in_progress"),
        "Finished.",
        call("set_task_plan", steps=["Repair planning precondition", "Verify recovered planning state"]),
        call("update_task_step", step_id=1, status="in_progress"),
        call("update_task_step", step_id=1, status="completed", note="Planning precondition repaired"),
        call("update_task_step", step_id=2, status="in_progress"),
        call("update_task_step", step_id=2, status="completed", note="Recovered plan verified"),
        "Recovered and completed.",
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Verify planning recovery", auto_recover=True)
        messages = app.graph.get_state(app.config(result["session_id"])).values["messages"]
    assert result["status"] == "completed", result
    assert any(message.type == "human" and "失败工具（诊断数据）" in str(message.content)
               and '"update_task_step"' in str(message.content) for message in messages)
    assert sum(event["event"] == "recovery_started" for event in result["trace"]) == 1


def test_blocked_plan_is_not_retried(settings):
    model = ScriptedModel(replies=[
        call("set_task_plan", steps=["Investigate", "Fix"]),
        call("update_task_step", step_id=1, status="blocked", note="Missing permissions"),
        "Cannot continue.",
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Fix dependencies", auto_recover=True)
    assert result["status"] == "incomplete"
    assert model.cursor == 3
    assert any(e["event"] == "recovery_stopped" and e["reason"] == "blocked_step" for e in result["trace"])


def test_approval_interrupt_never_auto_approves(settings, tmp_path):
    settings = Settings(
        workspace=settings.workspace, state_dir=tmp_path / "approval",
        approval_mode="ASK",
    )
    model = ScriptedModel(replies=[
        call("set_task_plan", steps=["Write a file", "Verify result"]),
        call("write_file", path="new.txt", content="do not write without approval"),
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Create a file", auto_recover=True)
    assert result["status"] == "interrupted"
    assert not (settings.workspace / "new.txt").exists()
    assert not any(e["event"] == "recovery_started" for e in result["trace"])


def test_opt_in_maintains_existing_incomplete_behavior(settings):
    model = ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect", "Implement"]), "Not completed yet.",
    ])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Implement", auto_recover=False)
    assert model.cursor == 2
    assert result["status"] == "incomplete"


def test_model_limit_shared_across_attempts(settings):
    from dataclasses import replace
    bounded = replace(settings, max_model_calls=3)
    model = ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect", "Implement"]),
        "Need more work.",
        call("get_task_plan"),
        "I can't finish.",
    ])
    with AgentApplication(bounded, model=model) as app:
        result = app.run("Try the task", auto_recover=True, max_recovery_attempts=2)
    assert result["status"] == "failed"
    assert "模型" in result["error"] or "model" in result["error"].lower()
    assert model.cursor == 3


def test_recovery_mode_requires_valid_bounds():
    parser = build_parser()
    parsed = parser.parse_args([
        "task", "--task", "fix", "--auto-recover", "--max-recovery-attempts", "2",
    ])
    assert parsed.auto_recover and parsed.max_recovery_attempts == 2
