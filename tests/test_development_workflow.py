"""Checkpointed workflow milestones derive from actual tool messages, not text."""

from langchain_core.messages import AIMessage, ToolMessage

from helpers import ScriptedModel, call
from mini_agent.development_workflow import evaluate_workflow
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings


def _call(name, ident, args=None):
    return AIMessage(content="", tool_calls=[{
        "name": name, "args": args or {}, "id": ident, "type": "tool_call",
    }])


def _done(name, ident, *, successful=True, mutation=False):
    return ToolMessage(
        content="ok" if successful else "failed",
        name=name, tool_call_id=ident,
        status="success" if successful else "error",
        artifact={"status": "success" if successful else "command_failed",
                  "mutation": mutation},
    )


def _workflow_state(*messages, profile="general", browser=False, migration=False, deliver=False):
    return {
        "development_workflow": {
            "goal": "Implement a working project change",
            "profile": profile, "require_browser": browser,
            "require_migration": migration, "deliver_pr": deliver,
        },
        "task_start": 0,
        "messages": list(messages),
    }


def test_workflow_recommends_unfinished_stage_in_correct_order():
    state = _workflow_state()
    fresh = evaluate_workflow(state)
    assert fresh["next_stage"] == "inspect"
    assert fresh["completed"] == 0
    state["messages"] += [_call("read_file", "inspect"), _done("read_file", "inspect")]
    assert evaluate_workflow(state)["next_stage"] == "implement"
    state["messages"] += [_call("write_file", "write"), _done("write_file", "write", mutation=True)]
    assert evaluate_workflow(state)["next_stage"] == "test"
    state["messages"] += [_call("run_dev_task", "test"), _done("run_dev_task", "test")]
    assert evaluate_workflow(state)["next_stage"] == "review"
    state["messages"] += [_call("review_code_changes", "review"),
                          _done("review_code_changes", "review")]
    assert evaluate_workflow(state)["complete"] is True


def test_later_file_change_invalidates_prior_test_review_and_browser_evidence():
    state = _workflow_state(browser=True)
    state["messages"] = [
        _call("read_file", "inspect"), _done("read_file", "inspect"),
        _call("write_file", "a"), _done("write_file", "a", mutation=True),
        _call("run_dev_task", "test"), _done("run_dev_task", "test"),
        _call("browser_action", "open", {"action": "open"}), _done("browser_action", "open"),
        _call("browser_diagnostics", "browser"), _done("browser_diagnostics", "browser"),
        _call("review_code_changes", "review"), _done("review_code_changes", "review"),
    ]
    assert evaluate_workflow(state)["complete"] is True
    state["messages"] += [_call("apply_patch", "fix"),
                          _done("apply_patch", "fix", mutation=True)]
    status = evaluate_workflow(state)
    assert status["next_stage"] == "test"
    assert status["complete"] is False
    assert next(x for x in status["stages"] if x["id"] == "browser")["done"] is False
    assert next(x for x in status["stages"] if x["id"] == "review")["done"] is False


def test_failed_operations_and_prior_task_history_do_not_count():
    previous = [_call("write_file", "old"), _done("write_file", "old", mutation=True)]
    fresh = [_call("read_file", "current"), _done("read_file", "current"),
             _call("write_file", "failed"), _done("write_file", "failed",
                                                 successful=False, mutation=True)]
    state = _workflow_state(*(previous + fresh))
    state["task_start"] = len(previous)
    result = evaluate_workflow(state)
    assert result["next_stage"] == "implement"
    assert result["last_mutation_observed"] is False


def test_optional_migration_and_draft_pr_stages_require_specific_tool_results():
    state = _workflow_state(profile="backend", migration=True, deliver=True)
    state["messages"] = [
        _call("read_file", "read"), _done("read_file", "read"),
        _call("generate_alembic_migration", "migration"),
        _done("generate_alembic_migration", "migration", mutation=True),
    ]
    result = evaluate_workflow(state)
    assert result["next_stage"] == "test"
    assert any(x["id"] == "migration" and x["done"] for x in result["stages"])
    state["messages"] += [
        _call("run_command", "tests", {"command": "python", "args": ["-m", "pytest", "-q"]}),
        _done("run_command", "tests"),
        _call("review_code_changes", "review"), _done("review_code_changes", "review"),
    ]
    assert evaluate_workflow(state)["next_stage"] == "deliver"
    state["messages"] += [
        _call("github_create_draft_pr", "pr"), _done("github_create_draft_pr", "pr"),
    ]
    assert evaluate_workflow(state)["complete"] is True


def test_opt_in_workflow_persists_in_checkpoint_and_blocks_premature_completion(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    settings = Settings(
        workspace=root, state_dir=tmp_path / "state", approval_mode="ALLOW",
    )
    model = ScriptedModel(replies=[
        call("start_development_workflow", goal="Implement an API feature",
             profile="general"),
        call("list_files", path="."),
        "I have only inspected the project; the feature is not finished.",
        "Continuing development remains incomplete.",
    ])
    with AgentApplication(settings, model=model) as app:
        first = app.run("Implement an API feature with the coding workflow")
        assert first["status"] == "incomplete", first
        assert first["development_workflow"]["next_stage"] == "implement"
        assert first["development_workflow"]["completed"] == 1
        resumed = app.run(None, thread_id=first["session_id"], continue_incomplete=True)
    assert resumed["status"] == "incomplete", resumed
    assert resumed["development_workflow"]["next_stage"] == "implement"


def test_screenshot_is_not_a_new_source_mutation_and_pip_is_not_a_test():
    state = _workflow_state()
    state["messages"] = [
        _call("read_file", "inspect"), _done("read_file", "inspect"),
        _call("write_file", "write"), _done("write_file", "write", mutation=True),
        _call("run_command", "install", {"command": "python", "args": ["-m", "pip", "--version"]}),
        _done("run_command", "install"),
    ]
    assert evaluate_workflow(state)["next_stage"] == "test"
    state["messages"] += [
        _call("run_dev_task", "tested", {"task": "test"}),
        _done("run_dev_task", "tested"),
        _call("review_code_changes", "review"), _done("review_code_changes", "review"),
        _call("browser_screenshot", "screenshot"), _done(
            "browser_screenshot", "screenshot", mutation=True,
        ),
    ]
    assert evaluate_workflow(state)["complete"] is True
