"""Read-only Obsidian RAG-chain search adapter.

This deliberately calls /api/search rather than /api/chat: one retrieval
tool call supplies grounded source text to the existing LangChain agent.
The model never supplies the service URL or credentials.
"""
from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from mini_agent.result import ToolResult
from mini_agent.sandbox import redact_text

MAX_RESPONSE_BYTES = 512 * 1024
MAX_RESULT_CHARS = 4_000
MAX_CHUNK_CHARS = 1_000


class KnowledgeSearchInput(BaseModel):
    query: str = Field(description="Search query for the Obsidian knowledge base", min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


def validate_rag_url(url: str) -> str:
    """Allow HTTP only for loopback; require HTTPS for other hosts."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("RAG_CHAIN_URL 必须是非空 URL")
    parsed = urlsplit(url.strip())
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.scheme not in {"http", "https"}):
        raise ValueError("RAG_CHAIN_URL 必须是无凭据、查询参数和 fragment 的 http(s) URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("RAG_CHAIN_URL 端口无效") from exc
    if port is not None and not 0 < port <= 65535:
        raise ValueError("RAG_CHAIN_URL 端口无效")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("非本机 RAG 服务必须使用 HTTPS")
    return url.strip().rstrip("/")


def search_rag(
    query: str, top_k: int = 5, *, base_url: str, timeout: float = 20.0,
    client_factory=httpx.Client,
) -> ToolResult:
    """Read only, bounded response with per-hit source references and no second LLM."""
    base_url = validate_rag_url(base_url)
    if not isinstance(query, str) or not 0 < len(query.strip()) <= 500:
        raise ValueError("query 长度必须为 1-500 字符")
    query = query.strip()
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise ValueError("top_k 必须为 1-20 的整数")
    if isinstance(timeout, bool):
        raise ValueError("RAG timeout 必须为有限正数")
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("RAG timeout 必须为有限正数") from exc
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("RAG timeout 必须为有限正数")

    try:
        # Never forward environment proxy or Authorization headers to a local
        # knowledge service; redirects also must not change the target host.
        with client_factory(timeout=timeout_value, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                "GET", base_url + "/api/search", params={"q": query, "k": top_k},
            ) as response:
                if response.status_code != 200:
                    return ToolResult(
                        "知识库检索失败（HTTP 状态码 " + str(response.status_code) + "）",
                        "tool_error", "rag_http_error",
                        {"http_status": response.status_code},
                    )
                chunks = bytearray()
                for part in response.iter_bytes():
                    if len(chunks) + len(part) > MAX_RESPONSE_BYTES:
                        return ToolResult(
                            "知识库响应过大，检索已停止。", "tool_error", "rag_response_too_large",
                        )
                    chunks.extend(part)
        payload = json.loads(chunks.decode("utf-8"))
    except (httpx.RequestError, UnicodeError, json.JSONDecodeError) as exc:
        return ToolResult(
            "无法读取知识库检索结果（" + type(exc).__name__ + "）。",
            "tool_error", "rag_unavailable",
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return ToolResult("知识库返回的数据结构不符合 /api/search 协议。", "tool_error", "rag_invalid_response")
    hits = payload["results"][:top_k]
    if not hits:
        return ToolResult(
            "知识库没有检索到相关原文；不能把无来源内容当作知识库结论。",
            metadata={"source_count": 0, "sources": [], "query": query, "top_k": top_k},
        )
    sections = [f"知识库检索命中 {len(hits)} 条（原文片段，不是已验证的完整答案）："]
    sources: list[dict[str, Any]] = []
    for index, hit in enumerate(hits, 1):
        if not isinstance(hit, dict):
            return ToolResult("检索结果包含无效条目。", "tool_error", "rag_invalid_response")
        raw_body = hit.get("content")
        if not isinstance(raw_body, str) or not raw_body.strip():
            return ToolResult("检索结果包含无效条目。", "tool_error", "rag_invalid_response")
        title = _bounded_text(hit.get("title"), "未知文档", 160)
        file_path = _bounded_text(hit.get("file_path"), "", 240)
        source = _bounded_text(hit.get("source"), "", 160)
        heading = _bounded_text(hit.get("heading"), "", 100)
        body = redact_text(raw_body)[:MAX_CHUNK_CHARS]
        rank = _positive_int(hit.get("rank"), index)
        chunk_index = _nonnegative_int(hit.get("chunk_index"))
        score_value = _finite_score(hit.get("score"))
        score_text = f" score={score_value:.4f}" if score_value is not None else ""
        source_text = source or "未标记来源"
        path_text = file_path or source or "未标记路径"
        sections.append(
            f"[{rank}] {title} / {heading}{score_text}\n"
            f"来源：{source_text}\n文件：{path_text}\n原文：{body}"
        )
        sources.append({
            "rank": rank,
            "score": score_value,
            "title": title,
            "file_path": file_path,
            "heading": heading,
            "source": source,
            "chunk_index": chunk_index,
        })
    output = "\n\n".join(sections)
    truncated = len(output) > MAX_RESULT_CHARS
    if truncated:
        output = output[:MAX_RESULT_CHARS] + "\n[输出已截断，请缩小 top_k 或细化查询]"
    metadata = {
        "source_count": len(hits),
        "truncated": truncated,
        "query": query,
        "top_k": top_k,
        "sources": sources,
    }
    return ToolResult(redact_text(output), metadata=metadata)


def _bounded_text(value: object, fallback: str, limit: int) -> str:
    """Keep provenance fields textual and bounded before exposing them to a model."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return redact_text(fallback)
    if not isinstance(value, str):
        value = str(value)
    return redact_text(value)[:limit]


def _finite_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        score = float(value)
    except (OverflowError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _positive_int(value: object, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return fallback


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def build_rag_tool(base_url: str, *, timeout: float = 20.0) -> StructuredTool:
    """Bind configured URL, not model input; mark as read-only policy metadata."""
    url = validate_rag_url(base_url)

    def invoke(query: str, top_k: int = 5) -> tuple[str, dict]:
        result = search_rag(query, top_k, base_url=url, timeout=timeout)
        return result.text, {
            "status": result.status, "code": result.code, "metadata": dict(result.metadata),
        }

    return StructuredTool.from_function(
        func=invoke, name="search_knowledge",
        description="只读搜索已配置的 Obsidian RAG 知识库，返回原文片段及标题、路径；查不到时不要编造笔记内容。",
        args_schema=KnowledgeSearchInput,
        response_format="content_and_artifact",
        metadata={"risk": "read", "mutation": False, "workspace_arguments": ()},
    )
