"""Bounded, read-only code navigation tools.

These use the Python standard library and the existing workspace/scan guards.
They deliberately return stable line references rather than maintaining a
second search index or reading the complete repository into an LLM context.
"""
from __future__ import annotations

import ast
from fnmatch import fnmatchcase
from pathlib import Path

from mini_agent import sandbox as file_safety
from mini_agent.config import MAX_TOOL_RESULT_CHARS
from .filesystem import (
    MAX_SEARCH_FILES,
    _active_workspace,
    _iter_search_files,
    resolve_inside_workspace,
)

MAX_NAV_RESULTS = 100


def _limit(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_NAV_RESULTS:
        raise ValueError(f"{name} 必须为 1 到 {MAX_NAV_RESULTS} 的整数")


def find_files(
    pattern: str = "*.py",
    path: str = ".",
    max_results: int = 50,
    *,
    workspace: str | Path | None = None,
) -> str:
    """Search file paths recursively, using fnmatch-style patterns.

    Pattern is matched against either the relative full path or basename.
    Symlinks and private directories obey the same protection as search_text.
    """
    if not isinstance(pattern, str) or not pattern or len(pattern) > 160:
        raise ValueError("pattern 必须是长度 1-160 的文件名匹配模式")
    _limit(max_results, name="max_results")
    root = _active_workspace(workspace)
    target = resolve_inside_workspace(path or ".", root)
    if not target.is_dir():
        raise NotADirectoryError(f"查找范围不是目录：{path}")
    scan: dict = {"entries": 0, "files": 0, "bytes": 0, "truncated": False}
    matches: list[str] = []
    total = 0
    for candidate, relative in _iter_search_files(target, scan, workspace=root):
        scan["files"] += 1
        if scan["files"] > MAX_SEARCH_FILES:
            scan["truncated"] = True
            break
        name = relative.as_posix()
        if not (fnmatchcase(name, pattern) or fnmatchcase(candidate.name, pattern)):
            continue
        total += 1
        if len(matches) < max_results:
            matches.append(name)
    matches.sort()
    header = [
        f"pattern: {pattern}",
        f"path: {path or '.'}",
        f"matches_total: {total}",
        f"matches_shown: {len(matches)}",
        f"scan_truncated: {'true' if scan['truncated'] else 'false'}",
        f"scan_entries: {scan['entries']}",
    ]
    result = "\n".join([*header, *matches])
    # Keep the displayed matches deterministic and under the same tool budget.
    while len(result) > MAX_TOOL_RESULT_CHARS and matches:
        matches.pop()
        header[3] = f"matches_shown: {len(matches)}"
        result = "\n".join([*header, *matches])
    return file_safety.redact_text(result)


class _Definitions(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.rows: list[tuple[int, int, str, str]] = []

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        name = getattr(node, "name")
        qualified = ".".join([*self.stack, name])
        self.rows.append((node.lineno, node.end_lineno or node.lineno, kind, qualified))
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self.stack else "function"
        self._visit_scope(node, kind)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = "async method" if self.stack else "async function"
        self._visit_scope(node, kind)


def inspect_python_symbols(
    path: str,
    query: str = "",
    max_results: int = 50,
    *,
    workspace: str | Path | None = None,
) -> str:
    """Return line-accurate Python definitions without executing the source."""
    if not isinstance(path, str) or not path.lower().endswith(".py"):
        raise ValueError("path 必须指向工作区内的 .py 文件")
    if not isinstance(query, str) or len(query) > 160:
        raise ValueError("query 必须是不超过 160 字符的字符串")
    _limit(max_results, name="max_results")
    root = _active_workspace(workspace)
    target = resolve_inside_workspace(path, root)
    content = file_safety.read_bounded_bytes(target).decode("utf-8-sig")
    tree = ast.parse(content, filename=path)
    visitor = _Definitions()
    visitor.visit(tree)
    found = [row for row in visitor.rows if query.casefold() in row[3].casefold()]
    selected = sorted(found, key=lambda x: (x[0], x[3]))[:max_results]
    header = [
        f"path: {path}",
        f"query: {query}",
        f"symbols_total: {len(found)}",
        f"symbols_shown: {len(selected)}",
    ]
    lines = [f"{path}:{start}-{end} {kind} {name}" for start, end, kind, name in selected]
    result = "\n".join([*header, *lines])
    while len(result) > MAX_TOOL_RESULT_CHARS and lines:
        lines.pop()
        header[-1] = f"symbols_shown: {len(lines)}"
        result = "\n".join([*header, *lines])
    return file_safety.redact_text(result)
