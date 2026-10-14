"""Cross-language source navigation using the mature Tree-sitter grammar pack.

Read-only, bounded syntax-tree scanning; unlike text grep this excludes
comments and string literals. Identifiers are *candidate* references and do
not resolve imports, scopes, or type information across files.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from langchain.tools import tool

from . import sandbox
from .config import MAX_TOOL_RESULT_CHARS
from .tools.filesystem import _iter_search_files, resolve_inside_workspace

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
}
_DEFINITION_NODES = frozenset({
    "class_definition", "function_definition", "async_function_definition",
    "function_declaration", "generator_function_declaration", "class_declaration",
    "method_definition", "interface_declaration", "type_alias_declaration",
    "enum_declaration", "variable_declarator",
})
_IDENTIFIER_NODES = frozenset({
    "identifier", "property_identifier", "type_identifier", "jsx_identifier",
})
_MAX_FILES = 160
_MAX_BYTES = 5 * 1024 * 1024
_MAX_FILE_SIZE = 128 * 1024
_MAX_NODES_PER_FILE = 20000
_MAX_RESULTS = 80


def _parser(language: str) -> Any:
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
    except ImportError as exc:
        raise ValueError(
            "多语言代码索引需安装可选依赖：pip install -e '.[code_index]'"
        ) from exc
    return Parser(get_language(language))


def _scan(
    root: Path, scope: str, *, name: str, references: bool,
    max_results: int,
) -> dict[str, Any]:
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= _MAX_RESULTS:
        raise ValueError(f"max_results 必须是 1–{_MAX_RESULTS}")
    if name and (not isinstance(name, str) or len(name) > 120):
        raise ValueError("符号名或查询不得超过 120 字符")
    if references and (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name)):
        raise ValueError("引用查询必须提供单一 Python/JS/TS 标识符名")
    start = resolve_inside_workspace(scope, root)
    if not start.is_dir():
        raise NotADirectoryError("代码扫描范围必须是项目内的目录")
    scan: dict[str, Any] = {"entries": 0, "files": 0, "bytes": 0, "truncated": False}
    parsers: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    matches_total = 0
    for path, relative in _iter_search_files(start, scan, workspace=root):
        language = _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if not language:
            continue
        if scan["files"] >= _MAX_FILES or scan["bytes"] >= _MAX_BYTES:
            scan["truncated"] = True
            break
        try:
            size = path.stat().st_size
            if size > _MAX_FILE_SIZE or size > _MAX_BYTES - scan["bytes"]:
                scan["truncated"] = True
                continue
            data = sandbox.read_bounded_bytes(path, _MAX_FILE_SIZE)
            text = data.decode("utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        scan["files"] += 1
        scan["bytes"] += len(data)
        parser = parsers.get(language)
        if parser is None:
            parser = _parser(language)
            parsers[language] = parser
        tree = parser.parse(data)
        source_lines = text.splitlines()
        stack = [tree.root_node]
        node_count = 0
        while stack:
            node = stack.pop()
            node_count += 1
            if node_count > _MAX_NODES_PER_FILE:
                scan["truncated"] = True
                break
            identifier = None
            if references and node.type in _IDENTIFIER_NODES:
                if data[node.start_byte:node.end_byte].decode("utf-8", "replace") == name:
                    identifier = node
            elif not references and node.type in _DEFINITION_NODES:
                candidate = node.child_by_field_name("name")
                if candidate is not None:
                    label = data[candidate.start_byte:candidate.end_byte].decode("utf-8", "replace")
                    if not name or name.casefold() in label.casefold():
                        identifier = candidate
            if identifier is not None:
                matches_total += 1
                if len(results) < max_results:
                    row = identifier.start_point.row
                    results.append({
                        "file": relative.as_posix(),
                        "line": row + 1,
                        "column": identifier.start_point.column + 1,
                        "name": data[identifier.start_byte:identifier.end_byte].decode("utf-8", "replace"),
                        "kind": node.type,
                        "source": source_lines[row][:220] if row < len(source_lines) else "",
                    })
            if len(results) >= max_results and matches_total >= max_results:
                scan["truncated"] = True
                break
            stack.extend(reversed(node.named_children))
        if len(results) >= max_results:
            break
    report: dict[str, Any] = {
        "scope": scope,
        "mode": "references" if references else "definitions",
        "query": name, "files_scanned": scan["files"],
        "bytes_scanned": scan["bytes"],
        "matches_found": matches_total,
        "items": results,
        "truncated": scan["truncated"],
        "interpretation": (
            "Syntax-level identifier occurrences, including definitions; not a resolved call graph."
            if references else
            "Parsed Python/JS/TS declaration names; nested symbols and variables may be included."
        ),
    }
    serialized = json.dumps(report, ensure_ascii=False)
    while len(serialized) > MAX_TOOL_RESULT_CHARS and results:
        results.pop()
        report["truncated"] = True
        serialized = json.dumps(report, ensure_ascii=False)
    if len(serialized) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("代码结构索引结果过大，请缩小目录范围")
    return report


def build_code_index_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool
    def inspect_code_symbols(
        path: str = ".", query: str = "", max_results: int = 60,
    ) -> str:
        """Find parsed Python/JS/TS classes, functions, methods and interfaces.

        Unlike text search, skips identifier-like text inside comments and
        strings. Read-only bounded Tree-sitter traversal in one workspace path.
        """
        return sandbox.redact_text(json.dumps(
            _scan(root, path, name=query, references=False, max_results=max_results),
            ensure_ascii=False,
        ))

    @tool
    def find_code_references(
        symbol: str, path: str = ".", max_results: int = 60,
    ) -> str:
        """Find syntax-level references of a symbol across Python/JS/TS/TSX.

        Results are source identifier occurrences (including definitions);
        they do not resolve aliases or prove call-graph relationships.
        """
        return sandbox.redact_text(json.dumps(
            _scan(root, path, name=symbol, references=True, max_results=max_results),
            ensure_ascii=False,
        ))

    for tool_item in (inspect_code_symbols, find_code_references):
        tool_item.metadata = {
            "risk": "read", "mutation": False, "workspace_arguments": ("path",),
        }
    return [inspect_code_symbols, find_code_references]
