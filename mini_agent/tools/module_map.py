"""Cross-module dependency and reverse-impact navigation for Python/JS projects.

Read-only, static AST/manifest import scanning: no project code is executed.
Dependencies are resolved only to real files that are in the bounded scan.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from mini_agent import sandbox
from mini_agent.config import MAX_TOOL_RESULT_CHARS
from .filesystem import (
    MAX_SEARCH_ENTRIES, _active_workspace, _iter_search_files,
    resolve_inside_workspace,
)

_MAX_MODULE_FILES = 250
_MAX_MODULE_BYTES = 5 * 1024 * 1024
_MAX_PER_FILE = 64 * 1024
_JS_IMPORT = re.compile(
    r"""(?:\b(?:import|export)\b[\s\S]{0,200}?\bfrom\s*|\bimport\s*|\brequire\s*\(\s*)["'](\.{1,2}/[^"']+)["']""",
)


def _module_key(path: str) -> str:
    return path[:-3].replace("/", ".") if path.endswith(".py") else path


def _py_imports(text: str, source: str) -> list[tuple[str, str]]:
    """Resolve only candidate in-repo Python paths, no sys.path imports."""
    imports: list[tuple[str, str]] = []
    try:
        tree = ast.parse(text, filename=source)
    except SyntaxError:
        return imports
    parent = Path(source).parent
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name.replace(".", "/"), "python"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                directory = parent
                for _ in range(node.level - 1):
                    directory = directory.parent
                base = directory.joinpath(*(node.module or "").split(".")) if node.module else directory
                base_string = base.as_posix()
            else:
                base_string = (node.module or "").replace(".", "/")
            if base_string:
                imports.append((base_string, "python"))
            for alias in node.names:
                if alias.name != "*":
                    imports.append(((base_string + "/" if base_string else "") + alias.name, "python"))
    return imports


def _js_imports(text: str) -> list[str]:
    return _JS_IMPORT.findall(text)


def _resolve_python(candidate: str, files: set[str]) -> str | None:
    matches = [candidate + ".py", candidate + "/__init__.py"]
    return next((name for name in matches if name in files), None)


def _resolve_js(source: str, candidate: str, files: set[str]) -> str | None:
    # Relative path only; absolute/bare package imports are external.
    joined = (Path(source).parent / candidate).as_posix()
    parts: list[str] = []
    for part in joined.split("/"):
        if part == ".":
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    prefix = "/".join(parts)
    candidates = [
        prefix, *[prefix + ext for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")],
        *[prefix + "/index" + ext for ext in (".js", ".jsx", ".ts", ".tsx")],
    ]
    return next((name for name in candidates if name in files), None)



def _is_test_module(path: str) -> bool:
    """Recognize common Python and JS/TS test-file naming conventions."""
    name = Path(path).name
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return True
    return any(
        name.endswith(f".{marker}{extension}")
        for marker in ("test", "spec")
        for extension in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    ) or ("__tests__" in Path(path).parts and Path(path).suffix in {
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    })


def inspect_module_dependencies(
    path: str = ".",
    focus: str = "",
    max_files: int = 150,
    *,
    workspace: str | Path | None = None,
) -> str:
    """Build a bounded local import graph with reverse dependents for a focus file."""
    if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= _MAX_MODULE_FILES:
        raise ValueError(f"max_files 必须为 1–{_MAX_MODULE_FILES}")
    root = _active_workspace(workspace)
    scope = resolve_inside_workspace(path, root)
    if not scope.is_dir():
        raise NotADirectoryError("依赖分析路径必须是目录")
    relative_focus = ""
    if focus:
        resolved = resolve_inside_workspace(focus, root)
        relative_focus = resolved.relative_to(root).as_posix()
        if not resolved.is_file():
            raise FileNotFoundError(f"目标源码文件不存在：{focus}")

    scan: dict[str, Any] = {"entries": 0, "files": 0, "bytes": 0, "truncated": False}
    candidates: dict[str, Path] = {}
    valid_extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
    for file, relative in _iter_search_files(scope, scan, workspace=root):
        if file.suffix.lower() not in valid_extensions:
            continue
        scan["files"] += 1
        if scan["files"] > max_files or scan["entries"] > MAX_SEARCH_ENTRIES:
            scan["truncated"] = True
            break
        candidates[relative.as_posix()] = file
    names = set(candidates)
    if relative_focus and relative_focus not in names:
        raise ValueError("目标文件不在本次受控扫描结果中，请调整 path 或 max_files")
    edges: set[tuple[str, str, str]] = set()
    for source, file in sorted(candidates.items()):
        try:
            if file.stat().st_size > _MAX_PER_FILE or file.stat().st_size + scan["bytes"] > _MAX_MODULE_BYTES:
                scan["truncated"] = True
                continue
            text = sandbox.read_bounded_bytes(file, _MAX_PER_FILE).decode("utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        scan["bytes"] += len(text.encode("utf-8"))
        if file.suffix == ".py":
            for candidate, kind in _py_imports(text, source):
                located = _resolve_python(candidate, names)
                if located and located != source:
                    edges.add((source, located, kind))
        else:
            for candidate in _js_imports(text):
                located = _resolve_js(source, candidate, names)
                if located and located != source:
                    edges.add((source, located, "javascript"))
    all_edges = sorted(edges)
    result: dict[str, Any] = {
        "path": path, "files_scanned": len(candidates),
        "edges_total": len(all_edges), "truncated": scan["truncated"],
    }
    if relative_focus:
        dependencies = sorted({dst for src, dst, _ in all_edges if src == relative_focus})
        dependents = sorted({src for src, dst, _ in all_edges if dst == relative_focus})
        found = {relative_focus}
        frontier = [relative_focus]
        while frontier:
            item = frontier.pop()
            for src, dst, _ in all_edges:
                if dst == item and src not in found:
                    found.add(src)
                    frontier.append(src)
        result.update({
            "focus": relative_focus,
            "dependencies": dependencies,
            "direct_dependents": dependents,
            "transitive_dependents": sorted(found - {relative_focus}),
            # Test candidates are dependency-derived hints, not verified coverage.
            "candidate_test_files": sorted(name for name in found if _is_test_module(name)),
            "edges": [
                {"from": a, "to": b, "language": kind}
                for a, b, kind in all_edges if a == relative_focus or b == relative_focus
            ],
        })
    else:
        result["edges"] = [
            {"from": a, "to": b, "language": kind} for a, b, kind in all_edges[:80]
        ]
    serialized = json.dumps(result, ensure_ascii=False)
    while len(serialized) > MAX_TOOL_RESULT_CHARS and result["edges"]:
        result["edges"].pop()
        result["truncated"] = True
        serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("依赖结果超过工具输出上限，请缩小 path 或 focus")
    return sandbox.redact_text(serialized)
