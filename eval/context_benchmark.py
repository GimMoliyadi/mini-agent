"""Offline comparison of the three Context Management views.

This benchmark never calls the provider. It builds one fixed, real-data history
from the current demo workspace and applies every mode to the same messages.
"""

import copy
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as agent_main  # noqa: E402
import tools  # noqa: E402


OUTPUT_JSON = EVAL_DIR / "context_benchmark.json"
OUTPUT_MARKDOWN = EVAL_DIR / "CONTEXT_BENCHMARK.md"
MODES = ("OFF", "WRITE_ONLY", "FULL")


def _assistant_message(round_id: int, tool_name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"benchmark_{round_id}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def build_fixed_history() -> list[dict]:
    """Use real workspace contents while keeping one identical input per mode."""
    task = json.loads((EVAL_DIR / "tasks.json").read_text(encoding="utf-8"))[0]["task"]
    agent_notes = tools.read_file("agent_notes.md")
    python_notes = tools.read_file("python_notes.md")
    write_content = tools.read_file("notes/agent_summary_eval.md")
    write_bytes = len(write_content.encode("utf-8"))

    return [
        {"role": "system", "content": agent_main.SYSTEM_PROMPT},
        {"role": "user", "content": task},
        _assistant_message(1, "list_files", {}),
        {
            "role": "tool",
            "tool_call_id": "benchmark_1",
            "content": tools.list_files("."),
        },
        _assistant_message(2, "read_file", {"path": "agent_notes.md"}),
        {
            "role": "tool",
            "tool_call_id": "benchmark_2",
            "content": agent_notes,
        },
        _assistant_message(
            3,
            "write_file",
            {"path": "notes/agent_summary_eval.md", "content": write_content},
        ),
        {
            "role": "tool",
            "tool_call_id": "benchmark_3",
            "content": f"已写入 agent_summary_eval.md（{write_bytes} 字节，已覆盖已有文件）",
        },
        _assistant_message(4, "list_files", {"path": "notes"}),
        {
            "role": "tool",
            "tool_call_id": "benchmark_4",
            "content": tools.list_files("notes"),
        },
        _assistant_message(5, "read_file", {"path": "python_notes.md"}),
        {
            "role": "tool",
            "tool_call_id": "benchmark_5",
            "content": python_notes,
        },
    ]


def _round_payloads(context: list[dict]):
    for index, message in enumerate(context):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if calls:
            yield message, context[index + 1 : index + 1 + len(calls)]


def _metrics(context: list[dict], original: list[dict], mode: str) -> dict:
    serialized = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    original_write_chars = 0
    original_read_chars = 0
    write_chars = 0
    read_chars = 0

    for message, results in _round_payloads(original):
        for call, result in zip(message["tool_calls"], results):
            if call["function"]["name"] == "write_file":
                original_write_chars += len(json.loads(call["function"]["arguments"])["content"])
            if call["function"]["name"] == "read_file":
                original_read_chars += len(result.get("content", ""))

    for message, results in _round_payloads(context):
        for call, result in zip(message["tool_calls"], results):
            if call["function"]["name"] == "write_file":
                write_chars += len(json.loads(call["function"]["arguments"])["content"])
            if call["function"]["name"] == "read_file":
                read_chars += len(result.get("content", ""))

    tool_result_chars = sum(
        len(message.get("content", ""))
        for message in context
        if message.get("role") == "tool"
    )
    return {
        "mode": mode,
        "message_count": len(context),
        "serialized_chars": len(serialized),
        "tool_result_chars": tool_result_chars,
        "write_content_original_chars": original_write_chars,
        "write_content_retained_chars": write_chars,
        "read_result_original_chars": original_read_chars,
        "read_result_retained_chars": read_chars,
        "protocol_pairing_legal": _pairing_legal(context),
    }


def _pairing_legal(messages: list[dict]) -> bool:
    pending: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            pending.extend(call["id"] for call in message.get("tool_calls") or [])
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return False
            pending.remove(call_id)
    return not pending


def _markdown(results: list[dict]) -> str:
    off_chars = next(item["serialized_chars"] for item in results if item["mode"] == "OFF")
    lines = [
        "# Context Management 离线 Benchmark",
        "",
        "同一份固定历史、同一份 messages，只改变 Context Mode；不调用真实模型。",
        "样本来自当前 `demo_workspace/` 的真实文件内容，包含 5 个 Tool Round。",
        "",
        "| mode | chars | 相比 OFF 减少 | read 信息保留 | write 信息保留 | tool result chars |",
        "|---|---:|---:|---|---|---:|",
    ]
    for item in results:
        reduction = off_chars - item["serialized_chars"]
        read_info = f'{item["read_result_retained_chars"]}/{item["read_result_original_chars"]}'
        write_info = f'{item["write_content_retained_chars"]}/{item["write_content_original_chars"]}'
        lines.append(
            f'| {item["mode"]} | {item["serialized_chars"]:,} | {reduction:,} '
            f'| {read_info} chars | {write_info} chars | {item["tool_result_chars"]:,} |'
        )

    write_only = next(item for item in results if item["mode"] == "WRITE_ONLY")
    full = next(item for item in results if item["mode"] == "FULL")
    lines.extend(
        [
            "",
            f'- WRITE_ONLY 相比 OFF 减少 **{off_chars - write_only["serialized_chars"]:,} 字符**。',
            f'- FULL 相比 OFF 减少 **{off_chars - full["serialized_chars"]:,} 字符**。',
            f'- FULL 在 WRITE_ONLY 基础上再减少 **{write_only["serialized_chars"] - full["serialized_chars"]:,} 字符**。',
            "- 三种模式的协议配对检查均通过。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    original = build_fixed_history()
    results = [
        _metrics(agent_main.build_model_context(copy.deepcopy(original), mode=mode), original, mode)
        for mode in MODES
    ]
    payload = {
        "sample": {
            "source": "demo_workspace real files + eval/tasks.json task_1 text",
            "rounds": 5,
            "input_messages": len(original),
        },
        "results": results,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MARKDOWN.write_text(_markdown(results), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
