"""Model-only context projection; checkpoints retain full messages and artifacts."""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage, trim_messages, convert_to_openai_messages
from types import SimpleNamespace
from .runtime_guards import get_current_budget


class ContextMiddleware(AgentMiddleware):
    def __init__(self, max_tokens: int, tool_chars: int = 4000, *, request_timeout: float = 120):
        self.max_tokens = max_tokens
        self.tool_chars = tool_chars
        self.request_timeout = request_timeout

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
        def estimate(messages):
            return sum(len(str(m.content)) // 3 + 20 for m in messages)
        if estimate(active) > self.max_tokens:
            raise ValueError("当前任务上下文超过预算；请缩小任务或增大 MINI_AGENT_CONTEXT_TOKENS")
        older = trim_messages(
            projected[:latest_user], max_tokens=self.max_tokens - estimate(active),
            token_counter=estimate, strategy="last", start_on="human", allow_partial=False,
        ) if latest_user else []
        messages = [*older, *active]
        budget = get_current_budget()
        model_settings = dict(request.model_settings)
        if budget is not None:
            limits = budget.before_model_request(convert_to_openai_messages(messages))
            limits["timeout"] = min(limits["timeout"], self.request_timeout)
            model_settings.update(limits)
        response = handler(request.override(messages=messages, model_settings=model_settings))
        if budget is not None:
            usage = response.result[0].usage_metadata or {}
            budget.record_usage(SimpleNamespace(
                prompt_tokens=usage.get("input_tokens"), completion_tokens=usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
            ))
        return response
