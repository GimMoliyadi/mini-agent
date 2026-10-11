"""OpenAI-compatible transport; no direct SDK tool loop."""

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from .settings import Settings


def create_model(settings: Settings) -> ChatOpenAI:
    if not all((settings.api_key, settings.base_url, settings.model)):
        raise ValueError("模型需要 API Key、Base URL 和模型名")
    return ChatOpenAI(
        model=settings.model, api_key=SecretStr(settings.api_key),
        base_url=settings.base_url, timeout=settings.request_timeout,
        max_retries=0, temperature=0, use_responses_api=False,
        http_socket_options=(),
        model_kwargs={"parallel_tool_calls": False},
        http_client=httpx.Client(timeout=settings.request_timeout, trust_env=True),
    )
