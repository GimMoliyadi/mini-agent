"""Offline tests for the read-only RAG-chain HTTP tool."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import ToolMessage

from mini_agent.rag_chain import build_rag_tool, search_rag, validate_rag_url
from mini_agent.result import ToolResult


def fake_client(handler):
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    return factory


class RagChainAdapterTests(unittest.TestCase):
    def test_local_url_and_remote_https_rules(self):
        self.assertEqual(validate_rag_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(validate_rag_url("https://rag.example.org"), "https://rag.example.org")
        for url in ("http://rag.example.org", "http://user:pass@localhost:8000",
                    "ftp://localhost:8000", "http://localhost:8000?key=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_rag_url(url)

    def test_search_returns_bounded_sources_without_another_llm_call(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "query": "agent", "count": 1,
                "results": [{
                    "score": 0.932, "title": "Agent Notes",
                    "file_path": "notes/agent.md", "heading": "Planning",
                    "content": "Use evidence before choosing an action.",
                }],
            })

        result = search_rag("agent", 3, base_url="http://127.0.0.1:8000",
                            client_factory=fake_client(handler))
        self.assertEqual(result.status, "success")
        self.assertIn("notes/agent.md", result.text)
        self.assertIn("Use evidence", result.text)
        self.assertEqual(result.metadata["source_count"], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/api/search")
        self.assertEqual(requests[0].url.params["q"], "agent")
        self.assertEqual(requests[0].url.params["k"], "3")

    def test_empty_result_does_not_fabricate_knowledge(self):
        handler = lambda _: httpx.Response(200, json={"results": []})
        result = search_rag("missing", base_url="http://localhost:8000",
                            client_factory=fake_client(handler))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.metadata["source_count"], 0)
        self.assertIn("没有检索到", result.text)

    def test_http_error_invalid_json_and_oversized_body(self):
        cases = [
            (lambda _: httpx.Response(503, text="down"), "rag_http_error"),
            (lambda _: httpx.Response(200, text="not-json"), "rag_unavailable"),
            (lambda _: httpx.Response(200, content=b"x" * (512 * 1024 + 1)), "rag_response_too_large"),
            (lambda _: httpx.Response(200, json={"results": {}}), "rag_invalid_response"),
        ]
        for handler, code in cases:
            with self.subTest(code=code):
                result = search_rag("check", base_url="http://localhost:8000",
                                    client_factory=fake_client(handler))
                self.assertEqual(result.status, "tool_error")
                self.assertEqual(result.code, code)

    def test_parameters_are_bounded(self):
        with self.assertRaises(ValueError):
            search_rag("query", 0, base_url="http://localhost:8000")
        with self.assertRaises(ValueError):
            search_rag("x" * 501, 5, base_url="http://localhost:8000")

    def test_langchain_tool_preserves_artifact_and_read_only_policy(self):
        with patch("mini_agent.rag_chain.search_rag", return_value=ToolResult(
            "source: notes/example.md", metadata={"source_count": 1},
        )):
            tool = build_rag_tool("http://localhost:8000")
            value = tool.invoke({
                "type": "tool_call", "name": tool.name,
                "args": {"query": "RAG", "top_k": 5}, "id": "rag-1",
            })
        self.assertIsInstance(value, ToolMessage)
        self.assertEqual(value.artifact["status"], "success")
        self.assertEqual(value.artifact["metadata"]["source_count"], 1)
        self.assertEqual(tool.metadata["risk"], "read")
        self.assertFalse(tool.metadata["mutation"])
        self.assertNotIn("base_url", tool.args)
        self.assertNotIn("timeout", tool.args)


if __name__ == "__main__":
    unittest.main()
