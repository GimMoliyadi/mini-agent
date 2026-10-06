"""Protocol-preserving context compaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass
import json


def _compact_write_arguments(arguments: str) -> str | None:
    """Return a valid, smaller historical view of a completed write call."""
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("content"), str):
        return None
    compact = dict(parsed)
    compact["content"] = (
        f"[previous write content omitted; {len(parsed['content'])} characters]"
    )
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _compact_write_round(message: dict, results: list[dict]) -> dict | None:
    """Make a protocol-preserving view of successful write calls in a tool round."""
    from . import runtime

    calls = message.get("tool_calls") or []
    if not calls:
        return None
    if len(results) != len(calls):
        return None
    compact_calls = []
    changed = False
    for call, result in zip(calls, results):
        if call["function"]["name"] != "write_file":
            compact_calls.append(call)
            continue
        if (
            result.get("role") != "tool"
            or result.get("tool_call_id") != call.get("id")
            or (not isinstance(result.get("content"), str))
            or result["content"].startswith(runtime.TOOL_FAILURE_PREFIX)
            or result["content"].startswith(runtime.APPROVAL_DENIED_PREFIX)
            or runtime.is_duplicate_notice(result["content"])
        ):
            return None
        compact_arguments = _compact_write_arguments(
            call["function"].get("arguments", "")
        )
        if compact_arguments is None:
            return None
        compact_call = dict(call)
        compact_function = dict(call["function"])
        compact_function["arguments"] = compact_arguments
        compact_call["function"] = compact_function
        compact_calls.append(compact_call)
        changed = True
    if not changed:
        return None
    compact_message = dict(message)
    compact_message["tool_calls"] = compact_calls
    return compact_message


def _tool_round(messages: list[dict], index: int) -> tuple[dict, list[dict]] | None:
    """Return one complete assistant-tool round, or leave malformed history alone."""
    message = messages[index]
    if message.get("role") != "assistant":
        return None
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    results = messages[index + 1 : index + 1 + len(calls)]
    if len(results) != len(calls):
        return None
    if any(
        (
            result.get("role") != "tool" or result.get("tool_call_id") != call.get("id")
            for call, result in zip(calls, results)
        )
    ):
        return None
    return (message, results)


def _read_reference(call: dict, result: dict) -> str | None:
    """Replace an old successful read with a small instruction to read it again."""
    from . import runtime

    content = result.get("content")
    if (
        call["function"]["name"] != "read_file"
        or not isinstance(content, str)
        or content.startswith(runtime.TOOL_FAILURE_PREFIX)
        or runtime.is_duplicate_notice(content)
    ):
        return None
    try:
        arguments = json.loads(call["function"].get("arguments") or "{}")
        path = arguments.get("path", "?")
    except (json.JSONDecodeError, AttributeError):
        path = "?"
    return f"[historical read compacted]\npath: {path}\ncharacters: {len(content)}\nThe full content is no longer in this context; call read_file again if needed."


def _compact_old_round(message: dict, results: list[dict]) -> tuple[dict, list[dict]]:
    """Compact only recoverable payloads in an old, otherwise valid tool round."""
    compact_message = _compact_write_round(message, results)
    if compact_message is None:
        compact_message = message
    compact_results = []
    for call, result in zip(message["tool_calls"], results):
        compact_result = _read_reference(call, result)
        compact_results.append(
            {**result, "content": compact_result}
            if compact_result is not None
            else result
        )
    return (compact_message, compact_results)


def build_model_context(messages: list[dict], mode: str | None = None) -> list[dict]:
    """Build the outbound model view without changing canonical history.

    OFF sends the original history unchanged. WRITE_ONLY compacts successful
    write contents in every completed tool round but leaves read results and
    round recency untouched. FULL keeps the existing write/read compaction and
    recent-round policy.
    """
    from . import runtime

    selected_mode = runtime.get_context_mode() if mode is None else mode.strip().upper()
    if selected_mode not in runtime.CONTEXT_MODES:
        allowed = ", ".join(runtime.CONTEXT_MODES)
        raise ValueError(
            f"Context mode 必须是 {allowed} 之一，当前是：{selected_mode!r}"
        )
    if selected_mode == "OFF":
        return list(messages)
    rounds: list[tuple[int, int, dict, list[dict]]] = []
    index = 0
    while index < len(messages):
        found = _tool_round(messages, index)
        if found is None:
            index += 1
            continue
        message, results = found
        rounds.append((index, index + 1 + len(message["tool_calls"]), message, results))
        index += 1 + len(message["tool_calls"])
    recent_rounds = (
        {start for start, _, _, _ in rounds[-runtime.MAX_RECENT_TOOL_ROUNDS :]}
        if selected_mode == "FULL"
        else set()
    )
    context: list[dict] = []
    index = 0
    while index < len(messages):
        round_at_index = next((item for item in rounds if item[0] == index), None)
        if round_at_index is not None:
            _, end, message, results = round_at_index
            if index in recent_rounds:
                context.append(message)
                context.extend(results)
            else:
                if selected_mode == "WRITE_ONLY":
                    compact_message = _compact_write_round(message, results) or message
                    compact_results = results
                else:
                    compact_message, compact_results = _compact_old_round(
                        message, results
                    )
                context.append(compact_message)
                context.extend(compact_results)
            index = end
            continue
        context.append(messages[index])
        index += 1
    return context
