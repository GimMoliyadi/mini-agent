"""Incomplete plans must not pass; the same task can continue from its checkpoint."""

from dataclasses import replace

import pytest

from helpers import ScriptedModel, call
from mini_agent.cli import build_parser
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "trusted"
    root.mkdir()
    return Settings(workspace=root, state_dir=tmp_path / "state", approval_mode="ALLOW")


def test_continue_incomplete_plan_keeps_original_task_and_checkpoint(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("set_task_plan", steps=["Read code", "Fix bug"]),
        "I have a plan but I have not implemented the fix.",
    ])) as app:
        first = app.run("Fix this project", thread_id="fixed-session")
        assert first["status"] == "incomplete"
        assert first["plan"]["completed"] == 0
        assert app.session_info("fixed-session")["status"] == "incomplete"
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("get_task_plan"),
        call("update_task_step", step_id=1, status="in_progress"),
        call("update_task_step", step_id=1, status="completed", note="Read files"),
        call("update_task_step", step_id=2, status="in_progress"),
        call("update_task_step", step_id=2, status="completed", note="Verified the fix"),
        "Both steps are complete.",
    ])) as app:
        completed = app.run(None, thread_id="fixed-session", continue_incomplete=True)
        assert completed["status"] == "completed", completed
        assert completed["session_id"] == first["session_id"]
        assert completed["task_id"] == first["task_id"]
        assert completed["plan"]["completed"] == 2
        assert completed["plan"]["revision"] == 1
        assert app.session_info("fixed-session")["status"] == "completed"


def test_blocked_plan_is_incomplete_and_continuable(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("set_task_plan", steps=["Inspect", "Implement"]),
        call("update_task_step", step_id=1, status="blocked", note="Missing config"),
        "Blocked pending configuration.",
    ])) as app:
        result = app.run("Build something")
    assert result["status"] == "incomplete"
    assert result["plan"]["steps"][0]["status"] == "blocked"


def test_cannot_continue_completed_or_missing_plan(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=["Done"])) as app:
        result = app.run("No plan", thread_id="done")
        assert result["status"] == "completed"
        with pytest.raises(ValueError, match="incomplete"):
            app.run(None, thread_id="done", continue_incomplete=True)
        with pytest.raises(ValueError, match="thread_id"):
            app.run(None, continue_incomplete=True)
        with pytest.raises(ValueError, match="不能同时"):
            app.run("New task", thread_id="done", continue_incomplete=True)


def test_pending_hitl_steps_require_normal_resume_not_plan_continuation(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    settings = Settings(workspace=root, state_dir=tmp_path / "state", approval_mode="ASK")
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("set_task_plan", steps=["Write file", "Verify"]),
        call("write_file", path="note.txt", content="hello"),
    ])) as app:
        stopped = app.run("Make note")
        assert stopped["status"] == "interrupted"
        with pytest.raises(ValueError, match="待恢复"):
            app.run(None, thread_id=stopped["session_id"], continue_incomplete=True)


def test_cli_continuation_flag_is_explicit():
    parser = build_parser()
    options = parser.parse_args([
        "task", "--resume", "example-thread", "--continue-plan", "--progress-jsonl",
    ])
    assert options.continue_plan and options.progress_jsonl
    assert parser.parse_args(["task", "--task", "hello"]).continue_plan is False
