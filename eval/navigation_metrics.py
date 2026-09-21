"""Pure metric extraction for repository-navigation evaluation traces."""

import json
import re


_LISTED_FILE = re.compile(r"^\[f\] (.+?)(?:  \(\d+ 字节\))?$")
_SEARCH_FILE = re.compile(r"^(.+):\d+$")


def _arguments(call) -> dict:
    try:
        value = json.loads(call["function"].get("arguments") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bounded_arguments(call) -> str:
    raw = call["function"].get("arguments") or "{}"
    return raw[:240] + ("…" if len(raw) > 240 else "")


def _listed_files(result: str, path: str) -> set[str]:
    base = "" if not path or path == "." else path.strip("/") + "/"
    found = set()
    for line in result.splitlines():
        match = _LISTED_FILE.match(line)
        if match:
            found.add((base + match.group(1)).replace("\\", "/"))
    return found


def _searched_files(result: str) -> set[str]:
    found = set()
    for line in result.splitlines():
        match = _SEARCH_FILE.match(line)
        if match and not line.startswith(("query:", "path:", "matches_")):
            found.add(match.group(1).replace("\\", "/"))
    return found


def _successful(result: str) -> bool:
    return bool(result) and not result.startswith(("[工具失败]", "[用户拒绝执行]"))


def _exit_code(result: str) -> str | None:
    for line in result.splitlines():
        if line.startswith("Exit code: "):
            return line.removeprefix("Exit code: ").strip()
    return None


def calculate_navigation_metrics(model_replies: list, messages: list[dict], target_file: str) -> dict:
    """Extract navigation counts and first-ground-truth discovery from history."""
    counts = {name: 0 for name in ("list_files", "search_text", "read_file")}
    candidate_files: set[str] = set()
    tool_chain = []
    first_correct_file_turn = None
    first_correct_method = None
    tool_calls_before_correct = None
    model_turn = 0
    tool_calls_seen = 0

    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant":
            index += 1
            continue
        model_turn += 1
        calls = message.get("tool_calls") or []
        for offset, call in enumerate(calls):
            name = call["function"]["name"]
            result_message = messages[index + 1 + offset] if index + 1 + offset < len(messages) else {}
            result = result_message.get("content", "") if result_message.get("role") == "tool" else ""
            args = _arguments(call)
            discovered = set()
            if name == "list_files":
                counts[name] += 1
                discovered = _listed_files(result, args.get("path", "."))
            elif name == "search_text":
                counts[name] += 1
                discovered = _searched_files(result)
            elif name == "read_file":
                counts[name] += 1
                path = str(args.get("path", "")).replace("\\", "/")
                if _successful(result) and path:
                    discovered = {path}

            candidate_files.update(discovered)
            located_target = target_file in discovered
            if located_target and first_correct_file_turn is None:
                first_correct_file_turn = model_turn
                first_correct_method = name
                tool_calls_before_correct = tool_calls_seen

            tool_chain.append(
                {
                    "turn": model_turn,
                    "tool": name,
                    "arguments": _bounded_arguments(call),
                    "candidate_files": sorted(discovered),
                    "result_chars": len(result),
                    "exit_code": _exit_code(result),
                }
            )
            tool_calls_seen += 1
        index += 1 + len(calls)

    final_answer = next(
        (message.get("content") or "" for message in reversed(messages)
         if message.get("role") == "assistant" and not message.get("tool_calls")),
        None,
    )
    return {
        "model_calls": len(model_replies),
        "tool_calls": len(tool_chain),
        "list_files_calls": counts["list_files"],
        "search_text_calls": counts["search_text"],
        "read_file_calls": counts["read_file"],
        "candidate_files_inspected": len(candidate_files),
        "candidate_files": sorted(candidate_files),
        "first_correct_file_turn": first_correct_file_turn,
        "first_correct_file_method": first_correct_method,
        "navigation_tool_calls_before_correct_file": tool_calls_before_correct,
        "prompt_tokens": sum(getattr(reply, "prompt_tokens", 0) or 0 for reply in model_replies),
        "completion_tokens": sum(getattr(reply, "completion_tokens", 0) or 0 for reply in model_replies),
        "total_tokens": sum(getattr(reply, "total_tokens", 0) or 0 for reply in model_replies),
        "final_answer": final_answer,
        "tool_chain": tool_chain,
    }


def navigation_accepted(metrics: dict, target_file: str) -> bool:
    final_answer = metrics.get("final_answer") or ""
    return (
        metrics.get("first_correct_file_turn") is not None
        and target_file in final_answer.replace("\\", "/")
    )
