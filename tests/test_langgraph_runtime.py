"""A-D acceptance through the actual create_agent graph, filesystem and subprocess."""

from dataclasses import replace
import json

import pytest
from langchain_core.tools import tool

from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from mini_agent.verifier import CodingTaskContract, TestCommand as RequiredCommand
from helpers import ScriptedModel, call


def test_configured_batch_limit_is_visible_to_model(settings):
    model = ScriptedModel(replies=["Ready"])
    with AgentApplication(replace(settings, budget_options={"MINI_AGENT_MAX_BATCH_TOOL_CALLS": "2"}),
                          model=model) as app:
        result = app.run("Report readiness")
    assert result["status"] == "completed"
    prompt = next(message.content for message in model.observed[0] if message.type == "system")
    assert "本轮最多调用 2 个工具" in prompt


@pytest.fixture
def settings(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return Settings(workspace=workspace, state_dir=tmp_path / "state", approval_mode="ALLOW")


def calculator(settings):
    (settings.workspace / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (settings.workspace / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8",
    )
    return CodingTaskContract("fix-add", "Fix addition and run its test", ("calculator.py",),
                              RequiredCommand("python", ("-m", "unittest", "test_calculator", "-q")))


def test_a_multistep_file_task(settings):
    (settings.workspace / "notes.txt").write_text("status: draft\n", encoding="utf-8")
    model = ScriptedModel(replies=[call("list_files"), call("search_text", query="draft"),
        call("read_file", path="notes.txt"),
        call("apply_patch", path="notes.txt", old_text="draft", new_text="ready"),
        call("read_file", path="notes.txt"), "Changed notes.txt to ready and read it back."])
    with AgentApplication(settings, model=model) as app:
        result = app.run("Find the draft note, change it to ready and verify")
        assert result["status"] == "completed", result
        assert [e["name"] for e in result["trace"] if e["event"] == "tool"] == [
            "list_files", "search_text", "read_file", "apply_patch", "read_file"]
    assert (settings.workspace / "notes.txt").read_text() == "status: ready\n"


def test_b_real_code_failure_fix_test_and_independent_verifier(settings):
    contract = calculator(settings)
    test = call("run_command", command="python", args=list(contract.test_command.args))
    model = ScriptedModel(replies=[call("read_file", path="calculator.py"), test,
        call("apply_patch", path="calculator.py", old_text="a - b", new_text="a + b"),
        test, "Fixed addition. The required test passes."])
    with AgentApplication(settings, model=model) as app:
        result = app.run(contract.instruction, contract=contract)
    assert result["status"] == "completed", result
    assert result["verification"]["accepted"]
    assert result["verification"]["final_test_count"] == 1
    assert result["verification"]["changed_files"] == ["calculator.py"]
    command_events = [e for e in result["trace"] if e.get("name") == "run_command"]
    assert [e["status"] for e in command_events] == ["error", "success"]


def test_c_resume_after_model_failure_does_not_replay_confirmed_write(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("write_file", path="note.txt", content="done\n"), RuntimeError("provider disconnected"),
    ])) as app:
        interrupted = app.run("Write note.txt and verify")
        assert interrupted["status"] == "failed"
    stamp = (settings.workspace / "note.txt").stat().st_mtime_ns
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("read_file", path="note.txt"), "The note is saved and verified.",
    ])) as app:
        resumed = app.run(None, thread_id=interrupted["session_id"])
        assert resumed["status"] == "completed", resumed
        assert [e["name"] for e in resumed["trace"] if e["event"] == "tool"] == ["read_file"]
    assert (settings.workspace / "note.txt").stat().st_mtime_ns == stamp


def test_native_hitl_persists_approval_and_resumes_in_new_application(settings):
    settings = replace(settings, approval_mode="ASK")
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("write_file", path="note.txt", content="approved"),
    ])) as app:
        paused = app.run("Create note.txt")
        assert paused["status"] == "interrupted", paused
        assert not (settings.workspace / "note.txt").exists()
    with AgentApplication(settings, model=ScriptedModel(replies=["Created the note."])) as app:
        still_paused = app.run(None, thread_id=paused["session_id"])
        assert still_paused["interrupts"]
        completed = app.run(None, thread_id=paused["session_id"], decisions=[{"type": "approve"}])
        assert completed["status"] == "completed", completed
    assert (settings.workspace / "note.txt").read_text() == "approved"


def test_d_new_standard_tool_without_runtime_changes(settings):
    @tool
    def multiply(left: int, right: int) -> int:
        """Multiply two integers."""
        return left * right
    multiply.metadata = {"risk": "read"}
    with AgentApplication(settings, extra_tools=[multiply], model=ScriptedModel(replies=[
        call("multiply", left=6, right=7), call("inspect_capabilities"), "42",
    ])) as app:
        result = app.run("Multiply 6 by 7")
        assert result["status"] == "completed", result
        messages = app.graph.get_state(app.config(result["session_id"])).values["messages"]
        assert next(message.content for message in messages if message.type == "tool" and message.name == "multiply") == "42"
        capabilities = json.loads(next(message.content for message in messages
                                      if message.type == "tool" and message.name == "inspect_capabilities"))
        assert "multiply" in {item["name"] for item in capabilities["callable_tools"]}


def test_deny_and_path_escape_are_real_failures(settings):
    with AgentApplication(replace(settings, approval_mode="DENY"), model=ScriptedModel(replies=[
        call("write_file", path="note.txt", content="blocked"), "The operation was denied.",
    ])) as app:
        result = app.run("Create note.txt")
        assert result["status"] == "incomplete"
    assert not (settings.workspace / "note.txt").exists()
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("write_file", path="../escape.txt", content="blocked"), "Cannot access that path.",
    ])) as app:
        result = app.run("Write outside workspace")
        assert result["status"] == "incomplete"
    assert not (settings.workspace.parent / "escape.txt").exists()


def test_contract_rejects_premature_finish_and_stale_test(settings):
    contract = calculator(settings)
    test = call("run_command", command="python", args=list(contract.test_command.args))
    with AgentApplication(settings, model=ScriptedModel(replies=[
        "Claiming complete too early", call("apply_patch", path="calculator.py", old_text="a - b", new_text="a + b"),
        test, call("write_file", path="calculator.py", content="def add(a, b):\n    return a + b\n# edited\n"),
        "Claiming complete with stale test", test, "Complete with fresh test",
    ])) as app:
        result = app.run(contract.instruction, contract=contract)
    assert result["status"] == "completed", result
    rejections = [e for e in result["trace"] if e["event"] == "verification_rejected"]
    assert len(rejections) == 2
    assert "successful_exact_required_test_stale" in rejections[-1]["reasons"]


def test_resume_refuses_other_workspace_and_preserves_old_json(settings, tmp_path):
    legacy = settings.state_dir / "sessions" / "old.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"version": 1, "messages": []}))
    with AgentApplication(settings, model=ScriptedModel(replies=["Hi"])) as app:
        result = app.run("Hi")
    other = tmp_path / "other"
    other.mkdir()
    with AgentApplication(replace(settings, workspace=other), model=ScriptedModel(replies=[])) as app:
        with pytest.raises(PermissionError):
            app.run(None, thread_id=result["session_id"])
    assert json.loads(legacy.read_text()) == {"version": 1, "messages": []}


def test_approval_content_change_is_rejected(settings):
    settings = replace(settings, approval_mode="ASK")
    (settings.workspace / "note.txt").write_text("old")
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("apply_patch", path="note.txt", old_text="old", new_text="agent"), "Could not change altered file.",
    ])) as app:
        result = app.run("Patch note.txt")
        (settings.workspace / "note.txt").write_text("human")
        result = app.run(None, thread_id=result["session_id"], decisions=[{"type": "approve"}])
        assert result["status"] == "incomplete", result
    assert (settings.workspace / "note.txt").read_text() == "human"


def test_multiturn_history_is_retained_and_failed_turn_has_no_previous_answer(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=["First answer", RuntimeError("disconnected")])) as app:
        first = app.run("First task", thread_id="multiturn")
        assert first["status"] == "completed"
        failed = app.run("Second task", thread_id="multiturn")
        assert failed["status"] == "failed"
        assert failed["answer"] is None
        history = app.graph.get_state(app.config("multiturn")).values["messages"]
        assert [message.content for message in history] == ["First task", "First answer", "Second task"]
    with AgentApplication(settings, model=ScriptedModel(replies=["Second answer"])) as app:
        result = app.run(None, thread_id="multiturn")
        assert result["status"] == "completed"
        assert result["answer"] == "Second answer"
        assert app.session_info("multiturn")["message_count"] == 4


def test_context_projection_preserves_full_tool_output_in_checkpoint(settings):
    @tool
    def long_document() -> str:
        """Return a long document."""
        return "x" * 9000
    long_document.metadata = {"risk": "read"}
    model = ScriptedModel(replies=[call("long_document"), "Read the document."])
    with AgentApplication(settings, extra_tools=[long_document], model=model) as app:
        result = app.run("Read a long document")
        assert result["status"] == "completed", result
        full = app.graph.get_state(app.config(result["session_id"])).values["messages"][-2]
        assert len(full.content) == 9000
    projected = next(message for message in model.observed[-1] if message.type == "tool")
    assert len(projected.content) < 4100
    assert "工具结果已截断" in projected.content


def test_native_hitl_rejection_is_incomplete(settings):
    with AgentApplication(replace(settings, approval_mode="ASK"), model=ScriptedModel(replies=[
        call("write_file", path="note.txt", content="rejected"), "The write was rejected.",
    ])) as app:
        paused = app.run("Create a note")
        result = app.run(None, thread_id=paused["session_id"], decisions=[{"type": "reject", "message": "User declined"}])
        assert result["status"] == "incomplete", result
    assert not (settings.workspace / "note.txt").exists()


def test_model_call_limit_stops_with_resumable_checkpoint(settings):
    with AgentApplication(replace(settings, max_model_calls=1), model=ScriptedModel(replies=[
        call("list_files"), "Must not be called",
    ])) as app:
        result = app.run("List files")
        assert result["status"] == "failed", result
        assert result["pending_nodes"]
        assert "ModelCallLimit" in result["error"]


def test_success_on_another_file_does_not_clear_failed_operation(settings):
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("read_file", path="missing.txt"),
        call("write_file", path="other.txt", content="done"),
        call("read_file", path="other.txt"), "One file was created; the other is missing.",
    ])) as app:
        result = app.run("Read missing.txt and create other.txt")
        assert result["status"] == "incomplete", result


def test_request_timeout_is_bounded_by_configured_transport_timeout(settings):
    model = ScriptedModel(replies=["Hello"])
    with AgentApplication(replace(settings, request_timeout=1.5), model=model) as app:
        assert app.run("Hello")["status"] == "completed"
    assert model.model_options[0]["timeout"] == 1.5


def test_trace_is_scoped_to_each_task_in_one_application(settings):
    model = ScriptedModel(replies=["First answer", "Second answer"])
    with AgentApplication(settings, model=model) as app:
        first = app.run("First task")
        second = app.run("Second task", thread_id="second-task")

    first_events = [item["event"] for item in first["trace"]]
    second_events = [item["event"] for item in second["trace"]]
    assert first_events == ["model", "budget"]
    assert second_events == ["model", "budget"]


def test_resuming_completed_task_does_not_include_later_task_trace(settings):
    model = ScriptedModel(replies=["A answer", "B answer"])
    with AgentApplication(settings, model=model) as app:
        first = app.run("Task A", thread_id="task-a")
        app.run("Task B", thread_id="task-b")
        resumed = app.run(None, thread_id=first["session_id"])

    assert [item["event"] for item in resumed["trace"]] == ["model", "budget"]


def test_rejected_tool_batch_keeps_one_model_usage_event(settings):
    oversized_batch = call("list_files")
    oversized_batch.tool_calls.append({
        "name": "list_files", "args": {}, "id": "list-files-2", "type": "tool_call",
    })
    limited = replace(settings, budget_options={"MINI_AGENT_MAX_BATCH_TOOL_CALLS": "1"})
    with AgentApplication(limited, model=ScriptedModel(replies=[oversized_batch])) as app:
        result = app.run("List the files")
        resumed = app.run(None, thread_id=result["session_id"])

    assert result["status"] == "failed"
    for observed in (result, resumed):
        model_events = [item for item in observed["trace"] if item["event"] == "model"]
        assert len(model_events) == 1
        assert model_events[0]["usage"] == {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        }
