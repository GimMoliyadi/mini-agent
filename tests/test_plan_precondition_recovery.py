"""A missing-plan precondition can be repaired without hiding other failures."""

from langchain_core.messages import AIMessage, ToolMessage

from mini_agent.verification import unresolved_tool_failures


def _pair(name, ident, tool_status="success", content="ok", **args):
    return [
        AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": ident, "type": "tool_call",
        }]),
        ToolMessage(
            name=name, tool_call_id=ident, status=tool_status,
            content=content,
            artifact={"status": "success" if tool_status == "success" else "tool_error"},
        ),
    ]


def _state(*items):
    return {"messages": list(items), "task_start": 0}


def test_successful_plan_creation_clears_only_missing_plan_failure():
    missing = _pair(
        "update_task_step", "wrong-order", "error",
        "ValueError: 当前任务尚无计划，请先调用 set_task_plan",
        step_id=1, status="in_progress",
    )
    created = _pair("set_task_plan", "created", steps=["Inspect", "Test"])
    assert unresolved_tool_failures(_state(*missing)) == ["update_task_step"]
    assert unresolved_tool_failures(_state(*missing, *created)) == []


def test_failed_plan_creation_does_not_clear_missing_plan_failure():
    missing = _pair(
        "update_task_step", "wrong-order", "error",
        "当前任务尚无计划，请先调用 set_task_plan", step_id=1,
    )
    failed = _pair(
        "set_task_plan", "failed-plan", "error", "plan invalid", steps=[],
    )
    assert set(unresolved_tool_failures(_state(*missing, *failed))) == {
        "set_task_plan", "update_task_step",
    }


def test_other_plan_update_failures_are_preserved():
    missing = _pair(
        "update_task_step", "wrong-order", "error",
        "当前任务尚无计划，请先调用 set_task_plan", step_id=1,
    )
    unrelated = _pair(
        "update_task_step", "bad-order", "error",
        "步骤必须先标记 in_progress，验证后才能完成",
        step_id=2, status="completed",
    )
    created = _pair("set_task_plan", "created", steps=["Inspect", "Test"])
    assert unresolved_tool_failures(_state(*missing, *unrelated, *created)) == [
        "update_task_step",
    ]


def test_history_from_prior_task_does_not_contaminate_new_task():
    old = _pair(
        "update_task_step", "old-error", "error",
        "当前任务尚无计划，请先调用 set_task_plan",
    )
    fresh = _pair("read_file", "read", path="src/app.py")
    assert unresolved_tool_failures({
        "messages": [*old, *fresh], "task_start": len(old),
    }) == []



def test_file_scoped_review_failure_survives_unrelated_success():
    failed = _pair(
        "review_code_changes", "review-a", "error",
        "file a could not be reviewed", paths=["a.py"],
    )
    unrelated = _pair("review_code_changes", "review-b", paths=["b.py"])
    assert unresolved_tool_failures(_state(*failed, *unrelated)) == ["review_code_changes"]
    fixed = _pair("review_code_changes", "review-a-retry", paths=["a.py"])
    assert unresolved_tool_failures(_state(*failed, *unrelated, *fixed)) == []


def test_fullstack_failure_requires_same_project_scope_for_recovery():
    failure = _pair(
        "run_fullstack_checks", "backend-1", "error",
        "backend tests failed", backend_path="backend-one", frontend_path="frontend-one",
    )
    unrelated = _pair(
        "run_fullstack_checks", "backend-2", backend_path="backend-two",
        frontend_path="frontend-two",
    )
    assert unresolved_tool_failures(_state(*failure, *unrelated)) == ["run_fullstack_checks"]
    repair = _pair(
        "run_fullstack_checks", "backend-1-retry",
        backend_path="backend-one", frontend_path="frontend-one",
    )
    assert unresolved_tool_failures(_state(*failure, *unrelated, *repair)) == []
