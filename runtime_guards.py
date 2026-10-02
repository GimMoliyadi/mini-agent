"""单任务资源额度；token 估算不是计费承诺或操作系统沙盒。"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar


DEFAULT_TASK_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_TOOL_CALLS = 64
DEFAULT_MAX_BATCH_TOOL_CALLS = 8
DEFAULT_MAX_CONTEXT_CHARS = 120_000
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_MAX_TOTAL_TOKENS = 100_000
DEFAULT_UNKNOWN_USAGE_POLICY = "STOP"
UNKNOWN_USAGE_POLICIES = frozenset({"STOP", "ESTIMATE"})
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
TOKEN_ACCOUNTING_NOTE = "已知用量与缺失用量分开记录；UTF-8字节估算不是精确token或硬收费上限。"


class BudgetExceeded(RuntimeError):
    """任务资源额度不足。"""


class TaskCancelled(KeyboardInterrupt):
    """任务已被用户取消。"""


def _positive_setting(env: Mapping[str, str], name: str, default: int | float) -> int | float:
    raw = env.get(name, str(default))
    parser = int if isinstance(default, int) else float
    expected = "正整数" if parser is int else "正数有限值"
    try:
        value = parser(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是{expected}，收到：{raw!r}") from exc
    if value <= 0 or (parser is float and not math.isfinite(value)):
        raise ValueError(f"{name} 必须是{expected}，收到：{raw!r}")
    return value


def _usage_value(reply: object, name: str) -> int | None:
    value = getattr(reply, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class RunBudget:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ
        self.task_timeout_seconds = _positive_setting(env, "MINI_AGENT_TASK_TIMEOUT_SECONDS", DEFAULT_TASK_TIMEOUT_SECONDS)
        self.max_tool_calls = _positive_setting(env, "MINI_AGENT_MAX_TOOL_CALLS", DEFAULT_MAX_TOOL_CALLS)
        self.max_batch_tool_calls = _positive_setting(env, "MINI_AGENT_MAX_BATCH_TOOL_CALLS", DEFAULT_MAX_BATCH_TOOL_CALLS)
        self.max_context_chars = _positive_setting(env, "MINI_AGENT_MAX_CONTEXT_CHARS", DEFAULT_MAX_CONTEXT_CHARS)
        self.max_output_tokens = _positive_setting(env, "MINI_AGENT_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)
        self.max_total_tokens = _positive_setting(env, "MINI_AGENT_MAX_TOTAL_TOKENS", DEFAULT_MAX_TOTAL_TOKENS)
        self.unknown_usage_policy = env.get("MINI_AGENT_UNKNOWN_USAGE_POLICY", DEFAULT_UNKNOWN_USAGE_POLICY).strip().upper()
        if self.unknown_usage_policy not in UNKNOWN_USAGE_POLICIES:
            raise ValueError("MINI_AGENT_UNKNOWN_USAGE_POLICY 必须是 STOP 或 ESTIMATE")
        self._clock = clock
        self._deadline = clock() + self.task_timeout_seconds
        self._cancelled = False
        self._tool_calls = 0
        self._model_requests = 0
        self._usage_responses = 0
        self._known_usage = dict.fromkeys(USAGE_FIELDS, 0)
        self._missing_usage = dict.fromkeys(USAGE_FIELDS, 0)
        self._known_token_sum = 0
        self._unknown_usage_responses = 0
        self._estimated_unknown_tokens = 0
        self._pending_request: tuple[int, int] | None = None

    def check(self) -> None:
        if self._cancelled:
            raise TaskCancelled("任务已取消。")
        if self._clock() >= self._deadline:
            raise BudgetExceeded("任务总时限已到，停止继续执行。")

    def cancel(self) -> None:
        self._cancelled = True

    def validate_batch_size(self, count: int) -> None:
        self.check()
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("工具批次大小必须是非负整数。")
        if count > self.max_batch_tool_calls:
            raise BudgetExceeded(f"单批工具数 {count} 超过上限 {self.max_batch_tool_calls}。")
        if count > self.max_tool_calls - self._tool_calls:
            raise BudgetExceeded("本批工具数超过任务剩余工具额度。")

    def before_tool_call(self) -> None:
        self.check()
        if self._tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("任务工具调用总数已达上限。")
        self._tool_calls += 1

    def before_model_request(self, messages: Sequence[Mapping[str, object]]) -> dict[str, int | float]:
        self.check()
        if self._pending_request is not None:
            raise BudgetExceeded("上一模型请求尚未完成用量记账，不能继续请求。")
        if self._unknown_usage_responses and self.unknown_usage_policy == "STOP":
            raise BudgetExceeded("模型未返回完整用量，保守策略禁止继续模型请求。")
        serialized = self._serialize_context(messages)
        try:
            estimated_prompt = len(serialized.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("模型上下文包含无法编码的Unicode字符。") from exc
        remaining = self.max_total_tokens - self._known_token_sum - self._estimated_unknown_tokens
        max_tokens = min(self.max_output_tokens, remaining - estimated_prompt)
        if max_tokens <= 0:
            raise BudgetExceeded("累计token策略额度不足以容纳上下文和下一次输出。")
        self.check()
        timeout = self._deadline - self._clock()
        if timeout <= 0:
            raise BudgetExceeded("任务总时限已到，停止模型请求。")
        self._pending_request = (estimated_prompt, max_tokens)
        self._model_requests += 1
        return {"max_tokens": max_tokens, "timeout": timeout}

    def _serialize_context(self, messages: Sequence[Mapping[str, object]]) -> str:
        try:
            serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("模型上下文必须是可序列化的有限JSON消息。") from exc
        if len(serialized) > self.max_context_chars:
            raise BudgetExceeded(f"模型上下文字符数超过上限 {self.max_context_chars}。")
        return serialized

    def record_usage(self, reply: object) -> None:
        if self._pending_request is None:
            raise ValueError("没有待记账的模型请求，不能重复记录用量。")
        usage = {name: _usage_value(reply, name) for name in USAGE_FIELDS}
        for name, value in usage.items():
            if value is None:
                self._missing_usage[name] += 1
            else:
                self._known_usage[name] += value
        self._record_token_charge(usage)
        self._pending_request = None
        self._usage_responses += 1

    def _record_token_charge(self, usage: dict[str, int | None]) -> None:
        prompt, completion, total = (usage[name] for name in USAGE_FIELDS)
        known_components = sum(value for value in (prompt, completion) if value is not None)
        self._known_token_sum += max(total or 0, known_components)
        if total is not None or (prompt is not None and completion is not None):
            return
        self._unknown_usage_responses += 1
        estimated_prompt, reserved_output = self._pending_request
        self._estimated_unknown_tokens += (
            (estimated_prompt if prompt is None else 0)
            + (reserved_output if completion is None else 0)
        )

    def snapshot(self) -> dict[str, object]:
        pending_tokens = sum(self._pending_request) if self._pending_request is not None else 0
        usage = {
            name: self._known_usage[name] if self._missing_usage[name] == 0 and self._pending_request is None else None
            for name in USAGE_FIELDS
        }
        return {
            **usage,
            "known_usage": dict(self._known_usage),
            "missing_usage_fields": dict(self._missing_usage),
            "known_token_sum": self._known_token_sum,
            "unknown_usage_responses": self._unknown_usage_responses,
            "estimated_unknown_tokens": self._estimated_unknown_tokens,
            "estimated_pending_tokens": pending_tokens,
            "accounted_tokens": self._known_token_sum + self._estimated_unknown_tokens + pending_tokens,
            "model_requests": self._model_requests,
            "usage_responses": self._usage_responses,
            "pending_responses": int(self._pending_request is not None),
            "tool_calls": self._tool_calls,
            "cancelled": self._cancelled,
            "remaining_seconds": max(0.0, self._deadline - self._clock()),
            "unknown_usage_policy": self.unknown_usage_policy,
            "token_accounting_note": TOKEN_ACCOUNTING_NOTE,
            "limits": {
                "task_timeout_seconds": self.task_timeout_seconds,
                "max_tool_calls": self.max_tool_calls,
                "max_batch_tool_calls": self.max_batch_tool_calls,
                "max_context_chars": self.max_context_chars,
                "max_output_tokens": self.max_output_tokens,
                "max_total_tokens": self.max_total_tokens,
            },
        }


_CURRENT_BUDGET: ContextVar[RunBudget | None] = ContextVar("mini_agent_run_budget", default=None)


@contextmanager
def budget_context(budget: RunBudget) -> Iterator[RunBudget]:
    token = _CURRENT_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)


def get_current_budget() -> RunBudget | None:
    return _CURRENT_BUDGET.get()
