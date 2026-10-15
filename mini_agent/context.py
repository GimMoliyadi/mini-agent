"""Model-only context projection; checkpoints retain full messages and artifacts.

Completed, older tool-call rounds are summarized when a coding task grows.
Recent AI tool calls and the matching ToolMessages remain intact, so OpenAI-
compatible transports never receive orphan tool results. Projection never
mutates the checkpoint or alters the original task instruction.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    HumanMessage, SystemMessage, ToolMessage, convert_to_openai_messages,
    trim_messages,
)

from .runtime_guards import get_current_budget
from .observability import Events


def _estimate(messages) -> int:
    return sum(len(str(message.content)) // 3 + 20 for message in messages)


def _history_summary(dropped) -> HumanMessage:
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    for message in dropped:
        if not isinstance(message, ToolMessage):
            continue
        name = (message.name or "tool")[:64]
        if message.status == "error" or (
            isinstance(message.artifact, dict)
            and message.artifact.get("status", "success") != "success"
        ):
            errors[name] += 1
        else:
            counts[name] += 1
    completed = ", ".join(f"{name}×{n}" for name, n in sorted(counts.items()))[:450]
    failed = ", ".join(f"{name}×{n}" for name, n in sorted(errors.items()))[:240]
    # This is only a historical index, NOT test or acceptance evidence.
    return HumanMessage(content=(
        "[本轮较早的模型/工具历史已做只读上下文投影，完整原始记录仍在 SQLite Checkpoint。]"
        f" 已省略 {len(dropped)} 条消息。较早成功工具：{completed or '无'}；"
        f"较早失败工具：{failed or '无'}。"
        "该摘要不证明当前源码正确或工具失败已经解决。"
        "遇到不确定的文件内容、测试结果或失败前提时请核对实际状态，"
        "优先利用最近的工具结果继续开发。"
    ))


def project_task_history(active, *, token_limit: int):
    """Keep the original task and a complete recent tool-call suffix.

    A suffix may start with an AIMessage containing tool calls or a HumanMessage,
    but never with a ToolMessage, which would break provider protocol pairing.
    """
    if not active or _estimate(active) <= token_limit:
        return list(active), 0
    if token_limit < _estimate(active[:1]):
        raise ValueError("初始任务本身超过模型上下文预算；请缩小任务")
    # Keep at least one complete recent tool-call round, if one fits. Drop
    # older material only at non-ToolMessage boundaries.
    boundary = None
    for index in range(1, len(active)):
        if isinstance(active[index], ToolMessage):
            continue
        dropped = active[1:index]
        summary = _history_summary(dropped)
        candidate = [active[0], summary, *active[index:]]
        if _estimate(candidate) <= token_limit:
            boundary = index
            break
    if boundary is None:
        # A single unbounded newest round cannot safely be partially removed.
        # The caller should fail rather than send broken tool-call references.
        raise ValueError("最近一轮工具调用及原始任务超过上下文预算；请缩小工具输出或增大预算")
    return [active[0], _history_summary(active[1:boundary]), *active[boundary:]], boundary - 1


class ContextMiddleware(AgentMiddleware):
    def __init__(self, max_tokens: int, tool_chars: int = 4000, *, events: Events, request_timeout: float = 120):
        self.max_tokens = max_tokens
        self.tool_chars = tool_chars
        self.request_timeout = request_timeout
        self.events = events

    def wrap_model_call(self, request, handler):
        projected = []
        for message in request.messages:
            if isinstance(message, ToolMessage) and len(str(message.content)) > self.tool_chars:
                message = message.model_copy(update={
                    "content": str(message.content)[:self.tool_chars] + "\n[工具结果已截断]",
                    "artifact": None,
                })
            projected.append(message)
        latest_user = request.state.get("task_start", 0)
        active = projected[latest_user:]
        # For long tasks reduce repeated tool histories before the model reaches
        # the hard limit. Keep full checkpoint messages for verification/resume.
        target = min(self.max_tokens, max(512, int(self.max_tokens * 0.64)))
        try:
            active, omitted = project_task_history(active, token_limit=target)
        except ValueError:
            # A complete newest tool-call round may exceed the soft target
            # while remaining valid under the hard configured context limit.
            active, omitted = project_task_history(active, token_limit=self.max_tokens)
        if omitted:
            self.events.emit(
                "context_compacted", task_id=request.state.get("task_id"),
                omitted_messages=omitted, projected_tokens=_estimate(active),
            )
        remaining = self.max_tokens - _estimate(active)
        older = trim_messages(
            projected[:latest_user], max_tokens=remaining,
            token_counter=_estimate, strategy="last", start_on="human",
            allow_partial=False,
        ) if latest_user and not omitted and remaining > 0 else []
        messages = [*older, *active]
        budget = get_current_budget()
        model_settings = dict(request.model_settings)
        system_message = request.system_message
        if budget is not None:
            guidance = f"本轮最多调用 {budget.max_batch_tool_calls} 个工具。有依赖的操作必须等待前一步工具返回结果。"
            content = system_message.content if system_message is not None else ""
            if isinstance(content, str):
                content = content + "\n" + guidance
            else:
                content = [*content, {"type": "text", "text": guidance}]
            system_message = SystemMessage(content=content)
            limits = budget.before_model_request(convert_to_openai_messages(messages))
            limits["timeout"] = min(limits["timeout"], self.request_timeout)
            model_settings.update(limits)
        response = handler(request.override(
            messages=messages, model_settings=model_settings, system_message=system_message,
        ))
        # Count real responses here; resuming an after_model node must not
        # charge the same paid response a second time.
        self.events.emit("model", task_id=request.state.get("task_id"), usage=response.result[0].usage_metadata)
        if budget is not None:
            usage = response.result[0].usage_metadata or {}
            budget.record_usage(SimpleNamespace(
                prompt_tokens=usage.get("input_tokens"), completion_tokens=usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
            ))
        return response
