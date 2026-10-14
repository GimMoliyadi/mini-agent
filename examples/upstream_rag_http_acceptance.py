"""Exercise the upstream rag-chain storage, retrieval, and HTTP boundary.

The fixture uses the upstream ``ChromaStore`` and SQLite repository with a
small deterministic vocabulary embedding.  It does not import or download a
sentence-transformers model.  Run a disposable HTTP smoke test with::

    python examples/upstream_rag_http_acceptance.py \
        --checkout .tmp/rag-chain

To leave the same fixture available for another process (for example the
mini-agent RAG adapter), use ``--serve --port 8766``.  The default report is
written to ``.state/upstream-rag.json``; use ``--state`` to choose another
path.  ``--checkout`` is intentionally explicit so the script has no machine
specific upstream path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import math
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any

import httpx
import uvicorn


FEATURES = (
    "stable_unique",
    "去重",
    "顺序",
    "None",
    "empty",
    "hashable",
    "discount",
    "折扣",
)


class VocabularyEmbedding:
    """Deterministic, labelled test embedding with no model or network use."""

    model_name = "acceptance-vocabulary-8d"
    dimensions = len(FEATURES)

    @property
    def fingerprint(self) -> dict[str, object]:
        return {"model": self.model_name, "dimensions": self.dimensions}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)

    def _one(self, text: str) -> list[float]:
        lowered = text.lower()
        values = [float(lowered.count(feature.lower())) for feature in FEATURES]
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            return [0.0] * self.dimensions
        return [value / norm for value in values]


class AcceptanceFixture:
    """Own the disposable SQL/Chroma state used by one acceptance run."""

    def __init__(self, checkout: Path):
        self.checkout = checkout.resolve()
        self._temp = tempfile.TemporaryDirectory(prefix="upstream-rag-acceptance-")
        self.root = Path(self._temp.name)
        self.conn: sqlite3.Connection | None = None
        self.store: Any = None
        self.app: Any = None
        self.embedding = VocabularyEmbedding()
        self.document_ids: dict[str, int] = {}
        self.chunk_ids: list[int] = []
        self._build()

    def _build(self) -> None:
        sys.path.insert(0, str(self.checkout))
        from app.config import Settings
        from app.database import connect, get_chunks_for_reindex, init_db
        from app.llm import MockLLM
        from app.main import create_app
        from app.vectorstore import ChromaStore

        settings = Settings(
            vault_path=str(self.root / "vault"),
            db_path=str(self.root / "knowledge.db"),
            chroma_path=str(self.root / "chroma"),
            knowledge_domains_config=str(self.checkout / "config" / "knowledge_domains.yaml"),
            embedding_provider="local",
            llm_provider="mock",
            top_k=2,
        )
        settings.vault_path_resolved.mkdir(parents=True, exist_ok=True)
        self.conn = connect(settings.db_path_resolved)
        init_db(self.conn)
        self._insert_rows()

        self.store = ChromaStore(settings.chroma_path_resolved)
        rows = get_chunks_for_reindex(self.conn)
        self.store.upsert_chunks(
            [
                {**row, "embedding": vector}
                for row, vector in zip(rows, self.embedding.embed([row["content"] for row in rows]))
            ]
        )
        self.store.write_index_info(self.embedding.fingerprint)
        self.app = create_app(
            settings=settings,
            embedding=self.embedding,
            store=self.store,
            llm=MockLLM(),
        )

    def _insert_rows(self) -> None:
        assert self.conn is not None
        from app.database import content_hash

        docs = (
            {
                "key": "stable_unique",
                "title": "stable_unique 规范",
                "file_path": "fixtures/stable-unique.md",
                "folder": "fixtures",
                "source": "upstream acceptance fixture",
                "domain": "acceptance",
                "source_type": "fixture",
                "content": (
                    "stable_unique 过滤 None，保留第一次出现的顺序，并去重。"
                    "输入 [None, 3, 1, 3, None, 2] 应得到 [3, 1, 2]。"
                ),
                "chunks": (
                    (
                        "行为契约",
                        "stable_unique 过滤 None，保留第一次出现的顺序，并去重。",
                    ),
                    (
                        "边界示例",
                        "stable_unique 边界：空输入返回空列表；只保留可 hash 的元素并维持稳定顺序。",
                    ),
                ),
            },
            {
                "key": "discount",
                "title": "discount 说明",
                "file_path": "fixtures/discount.md",
                "folder": "fixtures",
                "source": "unrelated acceptance fixture",
                "domain": "acceptance",
                "source_type": "fixture",
                "content": "discount 折扣计算：原价 100 元打八折后是 80 元。",
                "chunks": (("折扣", "discount 折扣计算：原价 100 元打八折后是 80 元。"),),
            },
        )
        now = "2026-10-12T00:00:00Z"
        for doc in docs:
            digest = content_hash(doc["content"])
            cursor = self.conn.execute(
                """
                INSERT INTO documents
                    (title, file_path, folder, tags, source, content, content_hash,
                     domain, source_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc["title"], doc["file_path"], doc["folder"], "acceptance",
                    doc["source"], doc["content"], digest, doc["domain"],
                    doc["source_type"], now, now,
                ),
            )
            document_id = int(cursor.lastrowid)
            self.document_ids[doc["key"]] = document_id
            for chunk_index, (heading, content) in enumerate(doc["chunks"]):
                chunk_cursor = self.conn.execute(
                    """
                    INSERT INTO chunks
                        (document_id, chunk_index, content, heading,
                         start_position, end_position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id, chunk_index, content, heading,
                        0, len(content), now,
                    ),
                )
                self.chunk_ids.append(int(chunk_cursor.lastrowid))
        self.conn.commit()

    def close(self) -> None:
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.ProgrammingError:
                pass
        if self.app is not None:
            try:
                self.app.state.conn.close()
            except sqlite3.ProgrammingError:
                pass
        self._temp.cleanup()


def _wait_for_socket(host: str, port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"upstream app did not listen on {host}:{port}")


def _http_probe(fixture: AcceptanceFixture, host: str, port: int, query: str) -> dict[str, Any]:
    if port == 0:
        with socket.socket() as sock:
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])
    config = uvicorn.Config(fixture.app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_socket(host, port)
    base_url = f"http://{host}:{port}"
    try:
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
            health_response = client.get("/health")
            documents_response = client.get("/api/documents")
            search_response = client.get("/api/search", params={"q": query, "k": 2})
            chat_response = client.post("/api/chat", json={"question": query, "top_k": 2})
        return {
            "url": base_url,
            "health_status": health_response.status_code,
            "health": health_response.json(),
            "documents_status": documents_response.status_code,
            "documents": documents_response.json(),
            "search_status": search_response.status_code,
            "search": search_response.json(),
            "chat_status": chat_response.status_code,
            "chat": chat_response.json(),
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _direct_probe(fixture: AcceptanceFixture, query: str) -> dict[str, Any]:
    from app.database import get_document

    assert fixture.conn is not None
    assert fixture.store is not None
    hits = fixture.app.state.retriever.search(query, top_k=2)
    raw_hits = fixture.store.query(fixture.embedding.embed_query(query), n_results=2)
    sql_documents = [get_document(fixture.conn, hit.document_id) for hit in hits]
    stable_hits = [hit for hit in hits if hit.title == "stable_unique 规范"]
    return {
        "query": query,
        "chroma_count": fixture.store.count(),
        "sql_document_count": fixture.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "sql_chunk_count": fixture.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "raw_chroma_hits": raw_hits,
        "retrieval_hits": [
            {
                "rank": index,
                "chunk_id": hit.chunk_id,
                "document_id": hit.document_id,
                "title": hit.title,
                "file_path": hit.file_path,
                "heading": hit.heading,
                "source": hit.source,
                "score": hit.score,
                "content": hit.content,
            }
            for index, hit in enumerate(hits, start=1)
        ],
        "sql_rows_for_hits": sql_documents,
        "metadata_roundtrip": bool(
            stable_hits
            and all(hit.file_path == "fixtures/stable-unique.md" for hit in stable_hits)
            and all(row and row["file_path"] == "fixtures/stable-unique.md" for row in sql_documents if row)
        ),
        "stable_unique_top_two": len(stable_hits) == 2,
        "discount_excluded": all(hit.title != "discount 说明" for hit in hits),
    }


def run_once(checkout: Path, host: str, port: int, query: str, state_path: Path) -> dict[str, Any]:
    fixture = AcceptanceFixture(checkout)
    try:
        direct = _direct_probe(fixture, query)
        http = _http_probe(fixture, host, port, query)
        report = {
            "mode": "smoke",
            "checkout": str(checkout.resolve()),
            "embedding": fixture.embedding.fingerprint,
            "direct": direct,
            "http": http,
            "passed": bool(
                direct["metadata_roundtrip"]
                and direct["stable_unique_top_two"]
                and direct["discount_excluded"]
                and http["health_status"] == 200
                and http["search_status"] == 200
                and http["chat_status"] == 200
                and http["search"]["results"][0]["file_path"] == "fixtures/stable-unique.md"
                and len(http["chat"]["sources"]) == 2
            ),
        }
    finally:
        fixture.close()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def serve_forever(checkout: Path, host: str, port: int, query: str, state_path: Path) -> None:
    fixture = AcceptanceFixture(checkout)
    try:
        direct = _direct_probe(fixture, query)
        report = {
            "mode": "serve",
            "checkout": str(checkout.resolve()),
            "url": f"http://{host}:{port}",
            "embedding": fixture.embedding.fingerprint,
            "direct": direct,
            "passed": bool(
                direct["metadata_roundtrip"]
                and direct["stable_unique_top_two"]
                and direct["discount_excluded"]
            ),
        }
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        uvicorn.run(fixture.app, host=host, port=port, log_level="warning")
    finally:
        fixture.close()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True, help="path to the upstream rag-chain checkout")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="HTTP port; --serve defaults to 8766 when omitted")
    parser.add_argument("--query", default="stable_unique None 去重顺序")
    parser.add_argument("--state", type=Path, default=root / ".state" / "upstream-rag.json")
    parser.add_argument("--serve", action="store_true", help="keep the real Chroma/SQL HTTP fixture running")
    args = parser.parse_args()
    checkout = args.checkout.expanduser().resolve()
    if not (checkout / "app" / "main.py").is_file():
        parser.error(f"--checkout does not look like rag-chain: {checkout}")
    if args.serve:
        serve_forever(checkout, args.host, args.port or 8766, args.query, args.state)
        return 0
    report = run_once(checkout, args.host, args.port, args.query, args.state)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
