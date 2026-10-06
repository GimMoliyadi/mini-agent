"""Task metrics and optional redacted JSONL events."""

from __future__ import annotations

from typing import TYPE_CHECKING
from .config import MAX_AGENT_STEPS

if TYPE_CHECKING:
    from . import runtime
from dataclasses import dataclass, field
import json
import os
from pathlib import Path

from .result import ToolResult, normalize_result


def _redact_trace_value(value):
    """Redact values before JSON encoding so redaction cannot damage framing."""
    from . import runtime

    if isinstance(value, str):
        value = runtime.redact_text(value)
        suffixes = ("API_KEY", "ACCESS_KEY", "ACCESS_KEY_ID", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTHORIZATION")
        secrets = {secret for key, secret in os.environ.items()
                   if secret and key.upper().endswith(suffixes)}
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: _redact_trace_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_trace_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ModelReply:
    """一次模型请求的结果：回复消息 + 结束原因 + token 用量。

    Phase 5 的 ask() 只返回 message，把 finish_reason 和 usage 都丢掉了。
    后果是「模型为什么不收口」根本无从查起——只知道它在反复要工具，
    不知道它是自己没判完、还是被步数上限掐断的。

    三个统计字段允许是 None：部分兼容服务商不返回 usage，
    返回了 finish_reason 却也不是 "stop" / "tool_calls" 的字面量。
    缺字段必须能显示出来，不能让程序直接崩掉。
    """

    message: runtime.ChatCompletionMessage
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_seconds: float = 0.0


@dataclass
class CodingTaskTrace:
    """Small per-task trace for bounded coding-loop validation.

    Memory is the default. JSONL is an explicit opt-in and excludes file bodies,
    model text, raw results, credentials, and arbitrary argument values.
    """

    model_calls: int = 0
    tool_calls: int = 0
    executed_tool_calls: int = 0
    list_files_calls: int = 0
    search_text_calls: int = 0
    read_file_calls: int = 0
    write_file_calls: int = 0
    apply_patch_calls: int = 0
    patch_successes: int = 0
    patch_failures: int = 0
    run_command_calls: int = 0
    productive_calls: int = 0
    duplicate_blocked: int = 0
    policy_rejected: int = 0
    failed_commands: int = 0
    successful_commands: int = 0
    finish_task_calls: int = 0
    finish_successes: int = 0
    finish_rejections: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    unknown_usage_calls: int = 0
    outcome: str | None = None
    max_steps_reached: bool = False
    recovery_grace: str | None = None
    recovery_state: dict | None = None
    final_model_call_limit: int = MAX_AGENT_STEPS
    final_answer: str | None = None
    task_status: str | None = None
    finish_attempts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    jsonl_path: Path | None = None

    def _record_event(self, event: dict) -> None:
        self.events.append(event)
        if self.jsonl_path is None:
            return
        path = Path(self.jsonl_path).expanduser()
        if path.suffix != ".jsonl" or path.is_symlink():
            raise ValueError("Trace output must be a regular .jsonl file")
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            import stat

            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ValueError("Trace output must be an unshared regular file")
            safe = {
                key: value
                for key, value in event.items()
                if key not in {"result", "write_target", "finish_gate"}
            }
            line = json.dumps(_redact_trace_value(safe), ensure_ascii=False) + "\n"
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                fd = -1
                stream.write(line)
        finally:
            if fd != -1:
                os.close(fd)

    def record_verifier(self, verdict: dict, *, duration_seconds: float = 0.0) -> None:
        self._record_event(
            {
                "turn": self.model_calls,
                "model_call": self.model_calls,
                "action": "verifier",
                "tool": None,
                "arguments": None,
                "result_status": "success"
                if verdict["accepted"]
                else "finish_rejected",
                "duration_seconds": duration_seconds,
                "token_usage": None,
                "approval": None,
                "mutation": False,
                "verifier_result": {
                    key: verdict.get(key)
                    for key in (
                        "accepted",
                        "artifact_passed",
                        "interaction_completed",
                        "final_test_status",
                        "reasons",
                    )
                },
            }
        )

    def record_model_turn(self, turn: int, reply: ModelReply) -> None:
        from . import runtime

        self.model_calls += 1
        values = (reply.prompt_tokens, reply.completion_tokens, reply.total_tokens)
        self.unknown_usage_calls += int(
            any((runtime._usage_count(value) is None for value in values))
        )
        self.prompt_tokens += runtime._usage_count(reply.prompt_tokens) or 0
        self.completion_tokens += runtime._usage_count(reply.completion_tokens) or 0
        self.total_tokens += runtime._usage_count(reply.total_tokens) or 0
        action = "tool_calls" if reply.message.tool_calls else "final_answer"
        event = {
            "turn": turn,
            "model_call": self.model_calls,
            "action": action,
            "finish_reason": reply.finish_reason,
            "tool": None,
            "arguments": None,
            "result_status": "success",
            "duration_seconds": reply.duration_seconds,
            "token_usage": {
                "prompt": reply.prompt_tokens,
                "completion": reply.completion_tokens,
                "total": reply.total_tokens,
            },
            "approval": None,
            "mutation": False,
            "verifier_result": None,
        }
        if not reply.message.tool_calls:
            self.final_answer = reply.message.content or ""
        self._record_event(event)

    def record_tool(
        self,
        turn: int,
        call,
        result: str,
        approval: str,
        executed: bool,
        classification: str | None = None,
        event_seq: int | None = None,
        finish_gate: dict | None = None,
        *,
        duration_seconds: float = 0.0,
        mutated: bool = False,
    ) -> dict:
        from . import runtime

        tool_name = call.function.name
        result = normalize_result(result, tool_name)
        arguments_summary, write_target = trace_arguments(call)
        exit_code = trace_exit_code(result)
        result_summary = (
            runtime.redact_text(result.splitlines()[0][:160]) if result else "<empty>"
        )
        classification = classification or classify_tool_call(call, result, approval)
        self.tool_calls += 1
        self.executed_tool_calls += int(executed)
        self.list_files_calls += int(tool_name == "list_files")
        self.search_text_calls += int(tool_name == "search_text")
        self.read_file_calls += int(tool_name == "read_file")
        self.write_file_calls += int(tool_name == "write_file")
        self.apply_patch_calls += int(tool_name == "apply_patch")
        if tool_name == "apply_patch":
            self.patch_successes += int(executed and result.status == "success")
            self.patch_failures += int(result.status == "tool_error")
        self.run_command_calls += int(tool_name == "run_command")
        self.productive_calls += int(classification == "PRODUCTIVE")
        self.duplicate_blocked += int(classification == "BLOCKED_DUPLICATE")
        self.policy_rejected += int(classification == "POLICY_REJECTED")
        self.failed_commands += int(classification == "FAILED_COMMAND")
        self.successful_commands += int(classification == "SUCCESSFUL_COMMAND")
        self.finish_task_calls += int(tool_name == "finish_task")
        self.finish_successes += int(classification == "FINISH_ACCEPTED")
        self.finish_rejections += int(classification == "FINISH_REJECTED")
        event = {
            "turn": turn,
            "action": "tool_call",
            "tool": tool_name,
            "arguments": arguments_summary,
            "approval": approval,
            "result": result_summary,
            "exit_code": exit_code,
            "write_target": write_target,
            "executed": executed,
            "classification": classification,
        }
        event.update(
            model_call=self.model_calls,
            result_status=result.status,
            result_code=result.code,
            duration_seconds=duration_seconds,
            token_usage=None,
            mutation=mutated,
            verifier_result=None,
        )
        if event_seq is not None:
            event["event_seq"] = event_seq
        if finish_gate is not None:
            event["finish_gate"] = finish_gate
            self.finish_attempts.append(dict(finish_gate))
        self._record_event(event)
        return event

    def mark_max_steps(self) -> None:
        self.max_steps_reached = True

    def set_task_state(self, task_state: runtime.TaskState | None) -> None:
        if task_state is None:
            return
        self.task_status = task_state.status.value
        self.finish_attempts = list(task_state.finish_attempts)

    def summary(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "executed_tool_calls": self.executed_tool_calls,
            "list_files_calls": self.list_files_calls,
            "search_text_calls": self.search_text_calls,
            "read_file_calls": self.read_file_calls,
            "write_file_calls": self.write_file_calls,
            "apply_patch_calls": self.apply_patch_calls,
            "patch_successes": self.patch_successes,
            "patch_failures": self.patch_failures,
            "run_command_calls": self.run_command_calls,
            "productive_calls": self.productive_calls,
            "executed_tools": self.executed_tool_calls,
            "duplicate_blocked": self.duplicate_blocked,
            "policy_rejected": self.policy_rejected,
            "failed_commands": self.failed_commands,
            "successful_commands": self.successful_commands,
            "finish_task_calls": self.finish_task_calls,
            "finish_successes": self.finish_successes,
            "finish_rejections": self.finish_rejections,
            "prompt_tokens": self.prompt_tokens
            if not self.unknown_usage_calls
            else None,
            "completion_tokens": self.completion_tokens
            if not self.unknown_usage_calls
            else None,
            "total_tokens": self.total_tokens if not self.unknown_usage_calls else None,
            "known_usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
            "usage_complete": self.unknown_usage_calls == 0,
            "unknown_usage_calls": self.unknown_usage_calls,
            "outcome": self.outcome,
            "max_steps_reached": self.max_steps_reached,
            "recovery_grace": self.recovery_grace,
            "recovery_state": self.recovery_state,
            "final_model_call_limit": self.final_model_call_limit,
            "final_answer": self.final_answer,
            "task_status": self.task_status,
            "finish_attempts": list(self.finish_attempts),
            "events": list(self.events),
        }


def trace_arguments(call) -> tuple[str, str | None]:
    """Return bounded argument text and the write target for a task trace."""
    from . import runtime

    raw_arguments = call.function.arguments or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        return ("<invalid JSON arguments>", None)
    if not isinstance(arguments, dict):
        return ("<non-object arguments>", None)
    summarized = {
        key: value
        for key, value in arguments.items()
        if key
        in {
            "path",
            "source",
            "destination",
            "command",
            "args",
            "cwd",
            "start_line",
            "max_lines",
            "max_results",
        }
    }
    write_target = (
        summarized.get("path")
        if call.function.name in {"write_file", "apply_patch"}
        else None
    )
    for key in ("content", "old_text", "new_text", "query", "summary"):
        value = arguments.get(key)
        if isinstance(value, str):
            summarized[key] = f"<{len(value)} characters>"
    return (
        runtime.redact_text(json.dumps(summarized, ensure_ascii=False, sort_keys=True))[
            :240
        ],
        write_target,
    )


def trace_exit_code(result: str) -> str | None:
    """Extract the command exit code from a formatted run_command result."""
    if isinstance(result, ToolResult):
        code = result.metadata.get("returncode")
        return str(code) if code is not None else None
    for line in result.splitlines():
        if line.startswith("Exit code: "):
            return line.removeprefix("Exit code: ")
    return None


def is_duplicate_notice(result: str) -> bool:
    return normalize_result(result).status == "duplicate_blocked"


def classify_tool_call(call, result: str, approval: str) -> str:
    """Classify one tool call using the existing Runtime result signals."""
    result = normalize_result(result, call.function.name)
    if approval == "DUPLICATE_BLOCKED" or result.status == "duplicate_blocked":
        return "BLOCKED_DUPLICATE"
    if approval in {"DENY", "BLOCKED"} or result.status == "policy_rejected":
        return "POLICY_REJECTED"
    if result.status == "finish_accepted":
        return "FINISH_ACCEPTED"
    if result.status == "finish_rejected":
        return "FINISH_REJECTED"
    if call.function.name == "run_command":
        return "SUCCESSFUL_COMMAND" if result.status == "success" else "FAILED_COMMAND"
    return "PRODUCTIVE"
