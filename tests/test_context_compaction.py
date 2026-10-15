"""Regression coverage for model-only, protocol-safe history compaction."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mini_agent.context import ContextMiddleware, project_task_history


def _history(rounds=18):
    result = [HumanMessage(content="Fix the failing API and run its tests")]
    for i in range(rounds):
        result.append(AIMessage(content="", tool_calls=[{
            "name": "read_file", "args": {"path": f"src/code_{i}.py"},
            "id": f"tool-{i}", "type": "tool_call",
        }]))
        result.append(ToolMessage(
            content="sample source\n" + "example = 12345\n" * 100,
            name="read_file", tool_call_id=f"tool-{i}", status="success",
            artifact={"status": "success"},
        ))
    return result


def _assert_pairs(projected):
    called = set()
    for message in projected:
        for item in getattr(message, "tool_calls", ()) or ():
            called.add(item["id"])
        if isinstance(message, ToolMessage):
            assert message.tool_call_id in called, "orphan result sent to provider"


def test_old_rounds_compact_without_mutating_full_history():
    original = _history()
    old_count = len(original)
    projected, omitted = project_task_history(original, token_limit=1600)
    assert omitted > 0
    assert len(original) == old_count
    assert original[1].tool_calls[0]["id"] == "tool-0"
    assert projected[0] is original[0]
    assert "Fix the failing API" in projected[0].content
    assert "Checkpoint" in projected[1].content
    assert "src/code_0.py" not in projected[1].content
    assert projected[-1] is original[-1]
    _assert_pairs(projected)


def test_no_omission_if_context_small_and_hard_fail_if_latest_round_does_not_fit():
    history = _history(rounds=1)
    kept, dropped = project_task_history(history, token_limit=4000)
    assert dropped == 0 and kept == history
    with pytest.raises(ValueError, match="最近一轮"):
        project_task_history(history, token_limit=80)


def test_model_request_is_projected_but_checkpoint_remains_full():
    class Events:
        def __init__(self):
            self.items = []

        def emit(self, kind, **fields):
            self.items.append((kind, fields))

    class Request:
        def __init__(self, messages):
            self.messages = messages
            self.state = {"task_start": 0, "task_id": "context-regression"}
            self.model_settings = {}
            self.system_message = SystemMessage(content="System prompt")

        def override(self, **kwargs):
            clone = Request(kwargs.get("messages", self.messages))
            clone.model_settings = kwargs.get("model_settings", self.model_settings)
            clone.system_message = kwargs.get("system_message", self.system_message)
            clone.state = self.state
            return clone

    history = _history()
    events = Events()
    observed = []

    def model_handler(request):
        observed.extend(request.messages)
        return SimpleNamespace(result=[AIMessage(content="Done")])

    ContextMiddleware(2800, events=events).wrap_model_call(
        Request(history), model_handler,
    )
    assert len(history) == 37
    assert len(observed) < len(history)
    assert observed[0] is history[0]
    assert any(isinstance(m, HumanMessage) and "Checkpoint" in m.content for m in observed)
    _assert_pairs(observed)
    assert any(name == "context_compacted" for name, _ in events.items)


def test_compaction_tracks_failed_tool_names_but_not_sensitive_tool_text():
    history = _history(rounds=14)
    history[2] = ToolMessage(
        content="secret from file must never enter compacted summary",
        name="read_file", tool_call_id="tool-0", status="error",
        artifact={"status": "command_failed"},
    )
    projected, _ = project_task_history(history, token_limit=1500)
    assert "失败工具" in projected[1].content
    assert "secret from file" not in projected[1].content
    _assert_pairs(projected)
