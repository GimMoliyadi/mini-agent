"""Deterministic HTTP RAG fixture for live mini-agent acceptance.

This is a tiny, dependency-free service with the same read-only contract as
rag-chain's ``GET /api/search`` endpoint.  It uses a file-backed SQLite FTS5
index and synthetic documents, so live agent runs never touch a user's Vault
or require a downloaded embedding model.

Run from the repository root::

    python tests/fixtures/rag_http_server.py --db .state/rag-fixture.sqlite3

The selected port and the source documents are printed as JSON on startup.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit


DOCUMENTS = (
    {
        "title": "stable_unique 规范",
        "file_path": "fixtures/stable-unique.md",
        "heading": "行为契约",
        "source": "mini-agent deterministic RAG fixture",
        "chunk_index": 0,
        "content": (
            "stable_unique(items) 必须过滤 None，保留第一次出现的顺序，"
            "并删除之后的重复值。输入 [None, 3, 1, 3, None, 2] 的结果是 [3, 1, 2]。"
        ),
    },
    {
        "title": "stable_unique 规范",
        "file_path": "fixtures/stable-unique.md",
        "heading": "边界示例",
        "source": "mini-agent deterministic RAG fixture",
        "chunk_index": 1,
        "content": (
            "空列表返回空列表；字符串等可哈希值也保留 first seen order。"
            "规范要求稳定顺序，不能使用会重新排序的集合结果作为返回值。"
        ),
    },
)


def initialize_index(path: Path) -> sqlite3.Connection:
    """Create or refresh the synthetic FTS5 index and return its connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False)
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks USING fts5(
            title, file_path, heading, source, content, chunk_index UNINDEXED
        )
        """
    )
    connection.execute("DELETE FROM rag_chunks")
    connection.executemany(
        """
        INSERT INTO rag_chunks(title, file_path, heading, source, content, chunk_index)
        VALUES (:title, :file_path, :heading, :source, :content, :chunk_index)
        """,
        DOCUMENTS,
    )
    connection.commit()
    return connection


def _match_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", query.lower())
    if not terms:
        return ""
    # FTS5 MATCH values are quoted so user text cannot alter the query grammar.
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def search(connection: sqlite3.Connection, query: str, top_k: int) -> list[dict]:
    match = _match_query(query)
    if not match:
        return []
    rows = connection.execute(
        """
        SELECT title, file_path, heading, source, chunk_index, content, bm25(rag_chunks) AS rank_score
          FROM rag_chunks
         WHERE rag_chunks MATCH ?
         ORDER BY rank_score ASC, rowid ASC
         LIMIT ?
        """,
        (match, top_k),
    ).fetchall()
    results = []
    for rank, row in enumerate(rows, 1):
        # SQLite's bm25 is a negative lower-is-better value.  The public
        # rag-chain contract exposes a bounded, higher-is-better score.
        score = 1.0 / (1.0 + abs(float(row[6])))
        results.append(
            {
                "rank": rank,
                "score": round(score, 6),
                "title": row[0],
                "file_path": row[1],
                "heading": row[2],
                "source": row[3],
                "chunk_index": int(row[4]),
                "content": row[5],
            }
        )
    return results


class RagRequestHandler(BaseHTTPRequestHandler):
    server_version = "mini-agent-rag-fixture/1.0"

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path == "/health":
            self._send_json({"status": "ok", "documents": 1, "chunks": len(DOCUMENTS)})
            return
        if request.path != "/api/search":
            self._send_json({"detail": "Not Found"}, HTTPStatus.NOT_FOUND)
            return
        params = parse_qs(request.query, keep_blank_values=True)
        query = params.get("q", [""])[0]
        try:
            top_k = int(params.get("k", ["5"])[0])
        except ValueError:
            self._send_json({"detail": "k must be an integer"}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        if not query.strip() or not 1 <= top_k <= 20:
            self._send_json({"detail": "q and k are invalid"}, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        service = self.server  # type: ignore[attr-defined]
        with service.rag_lock:
            results = search(service.rag_db, query, top_k)
        self._send_json({"query": query, "top_k": top_k, "count": len(results), "results": results})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RagFixtureServer(ThreadingHTTPServer):
    rag_db: sqlite3.Connection
    rag_lock: Lock

    def __init__(self, host: str, port: int, db_path: Path):
        super().__init__((host, port), RagRequestHandler)
        self.rag_db = initialize_index(db_path)
        self.rag_lock = Lock()

    def server_close(self) -> None:
        self.rag_db.close()
        super().server_close()


def create_server(host: str = "127.0.0.1", port: int = 0, db_path: Path | None = None) -> RagFixtureServer:
    path = db_path or Path.cwd() / ".state" / "rag-fixture.sqlite3"
    return RagFixtureServer(host, port, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=Path.cwd() / ".state" / "rag-fixture.sqlite3")
    args = parser.parse_args()
    server = RagFixtureServer(args.host, args.port, args.db)
    print(json.dumps({
        "url": f"http://{args.host}:{server.server_port}",
        "db": str(args.db.resolve()),
        "documents": [item["file_path"] for item in DOCUMENTS[:1]],
        "chunks": len(DOCUMENTS),
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
