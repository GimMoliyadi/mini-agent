"""Real LangGraph progress events are live, redactable and preserve final results."""
from __future__ import annotations

from dataclasses import replace
import json

from mini_agent.cli import build_parser
from mini_agent.runtime import AgentApplication
from mini_agent.settings import Settings
from helpers import ScriptedModel, call


def _settings(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    return Settings(workspace=workspace, state_dir=tmp_path / "state", approval_mode="ALLOW")


def test_stream_updates_uses_real_graph_and_keeps_final_result(tmp_path):
    settings = _settings(tmp_path)
    (settings.workspace / "notes.txt").write_text("hello", encoding="utf-8")
    events = []
    model = ScriptedModel(replies=[call("read_file", path="notes.txt"), "Read note successfully."])
    with AgentApplication(settings, model=model, event_callback=events.append) as app:
        result = app.run("Read the note", stream_updates=True)
        assert result["status"] == "completed", result
        assert "Read note successfully." in result["answer"]
        assert app.graph.get_state(app.config(result["session_id"])).values["status"] == "completed"
    steps = [event for event in events if event["event"] == "graph_step"]
    assert steps, events
    assert all(isinstance(event["node"], str) and event["node"] for event in steps)
    assert {event["task_id"] for event in steps} == {result["task_id"]}
    assert any(event["event"] == "tool" and event["name"] == "read_file" for event in events)
    # No raw model content, tool parameters or file bodies in graph-step events.
    for event in steps:
        assert set(event) == {"event", "time", "task_id", "node"}
        assert "notes.txt" not in json.dumps(event)
        assert "hello" not in json.dumps(event)


def test_default_invoke_does_not_emit_graph_steps(tmp_path):
    settings = _settings(tmp_path)
    with AgentApplication(settings, model=ScriptedModel(replies=["Done"])) as app:
        result = app.run("Simple task")
    assert result["status"] == "completed"
    assert all(event["event"] != "graph_step" for event in result["trace"])


def test_cli_opt_in_argument_for_task_and_start():
    parser = build_parser()
    task = parser.parse_args(["task", "--task", "hello", "--progress-jsonl"])
    start = parser.parse_args(["start", "--progress-jsonl"])
    assert task.progress_jsonl
    assert start.progress_jsonl
    assert parser.parse_args(["task", "--task", "hello"]).progress_jsonl is False


def test_streamed_interruption_and_resume_do_not_replay_mutation(tmp_path):
    settings = replace(_settings(tmp_path), approval_mode="ASK")
    with AgentApplication(settings, model=ScriptedModel(replies=[
        call("write_file", path="note.txt", content="saved"), "Finished.",
    ])) as app:
        result = app.run("Create note", stream_updates=True)
        assert result["status"] == "interrupted", result
        assert not (settings.workspace / "note.txt").exists()
        thread_id = result["session_id"]

    with AgentApplication(settings, model=ScriptedModel(replies=["Finished."])) as app:
        outcome = app.run(
            None, thread_id=thread_id, decisions=[{"type": "approve"}], stream_updates=True,
        )
        assert outcome["status"] == "completed", outcome
        assert (settings.workspace / "note.txt").read_text(encoding="utf-8") == "saved"
        assert [event["name"] for event in outcome["trace"] if event["event"] == "tool"] == ["write_file"]
