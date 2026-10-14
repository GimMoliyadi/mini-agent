"""Deterministic framework model and low-level test adapters."""

from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult



def call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name + str(len(str(args))), "type": "tool_call"}])


class ScriptedModel(BaseChatModel):
    replies: list[Any]
    cursor: int = 0
    observed: list = []
    model_options: list[dict] = []

    @property
    def _llm_type(self):
        return "scripted-test-model"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.observed.append(list(messages))
        self.model_options.append(kwargs)
        if self.cursor >= len(self.replies):
            raise RuntimeError("Scripted model exhausted")
        reply = self.replies[self.cursor]
        self.cursor += 1
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, str):
            reply = AIMessage(content=reply)
        reply = reply.model_copy(deep=True)
        reply.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        return ChatResult(generations=[ChatGeneration(message=reply)])
