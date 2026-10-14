"""Real TCP integration against the deterministic RAG HTTP fixture."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from rag_http_server import create_server  # noqa: E402
from mini_agent.rag_chain import search_rag  # noqa: E402


class RagChainHttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mini-agent-rag-")
        self.server = create_server(db_path=Path(self.temp_dir.name) / "rag.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp_dir.cleanup()

    def test_real_http_search_preserves_fixture_evidence(self) -> None:
        response = httpx.get(self.base_url + "/health", timeout=5, trust_env=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chunks"], 2)

        result = search_rag(
            "stable_unique None first seen order",
            top_k=2,
            base_url=self.base_url,
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.metadata["source_count"], 2)
        self.assertIn("fixtures/stable-unique.md", result.text)
        self.assertIn("过滤 None", result.text)
        self.assertEqual(result.metadata["sources"][0]["source"],
                         "mini-agent deterministic RAG fixture")
        self.assertEqual(
            {source["chunk_index"] for source in result.metadata["sources"]},
            {0, 1},
        )


if __name__ == "__main__":
    unittest.main()
