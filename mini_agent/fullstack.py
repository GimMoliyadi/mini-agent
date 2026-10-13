"""Map literal FastAPI route declarations to JS/TS frontend HTTP calls.

Read-only project navigation for cross-stack coding tasks. This is a bounded
static analysis aid, not a running-server integration or OpenAPI validator.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from typing import Any

from langchain.tools import tool

from . import sandbox
from .config import MAX_TOOL_RESULT_CHARS
from .tools.filesystem import (
    MAX_SEARCH_ENTRIES, _iter_search_files, resolve_inside_workspace,
)

_MAX_FILES_PER_SIDE = 150
_MAX_BYTES_PER_SIDE = 2 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024
_MAX_ENDPOINTS = 100
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_DECORATOR_METHODS = {name.lower() for name in _HTTP_METHODS}
_FETCH = re.compile(
    r"""\bfetch\s*\(\s*(?P<quote>['"])(?P<path>/[^'"]*)(?P=quote)"""
)
_AXIOS = re.compile(
    r"""\b(?:axios|api|client)\s*\.\s*(?P<method>get|post|put|patch|delete|head|options)\s*\(\s*(?P<quote>['"])(?P<path>/[^'"]*)(?P=quote)""",
    re.IGNORECASE,
)
_FETCH_METHOD = re.compile(r"""\bmethod\s*:\s*['"](?P<method>[A-Za-z]+)['"]""", re.IGNORECASE)


def _as_literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _endpoint(path: str) -> str:
    return "/" + "/".join(part for part in path.split("/") if part)


def _fastapi_routes(text: str, file: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    router_prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        function_name = call.func.id if isinstance(call.func, ast.Name) else (
            call.func.attr if isinstance(call.func, ast.Attribute) else ""
        )
        if function_name not in {"APIRouter", "FastAPI"}:
            continue
        prefix = ""
        if function_name == "APIRouter":
            for keyword in call.keywords:
                if keyword.arg == "prefix":
                    prefix = _as_literal(keyword.value) or ""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                router_prefixes[target.id] = prefix

    found: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            receiver = decorator.func.value
            if not isinstance(receiver, ast.Name) or receiver.id not in router_prefixes:
                continue
            named = decorator.func.attr
            if named.lower() not in _DECORATOR_METHODS and named != "api_route":
                continue
            route_path = _as_literal(decorator.args[0]) if decorator.args else None
            if route_path is None:
                route_path = next(
                    (_as_literal(keyword.value) for keyword in decorator.keywords if keyword.arg == "path"),
                    None,
                )
            if route_path is None or not route_path.startswith("/"):
                continue
            methods = [named.upper()]
            if named == "api_route":
                methods = ["GET"]
                for keyword in decorator.keywords:
                    if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                        values = [_as_literal(item) for item in keyword.value.elts]
                        if values and all(value and value.upper() in _HTTP_METHODS for value in values):
                            methods = [str(value).upper() for value in values]
            path = _endpoint(router_prefixes[receiver.id] + "/" + route_path)
            for method in methods:
                found.append({
                    "file": file, "line": decorator.lineno,
                    "method": method, "path": path,
                    "handler": node.name,
                })
    return found


def _frontend_calls(text: str, file: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for pattern, kind in ((_FETCH, "fetch"), (_AXIOS, "client")):
        for match in pattern.finditer(text):
            path = match.group("path")
            # Only literal, project-relative API requests can be compared.
            if not path or not path.startswith("/") or path.startswith("//"):
                continue
            method = "GET" if kind == "fetch" else match.group("method").upper()
            if kind == "fetch":
                # Inspect only the immediate options object after the URL.
                trailer = text[match.end():match.end() + 350]
                if re.match(r"\s*,\s*\{", trailer):
                    closing = trailer.find("}")
                    option_text = trailer[:closing] if closing >= 0 else trailer
                    selected = _FETCH_METHOD.search(option_text)
                    if selected:
                        method = selected.group("method").upper()
            if method not in _HTTP_METHODS:
                continue
            path = _endpoint(path.split("?", 1)[0].split("#", 1)[0])
            found.append({
                "file": file, "line": text.count("\n", 0, match.start()) + 1,
                "method": method, "path": path,
            })
    return sorted(found, key=lambda item: (item["file"], item["line"], item["method"]))


def _endpoint_matches(route: str, request: str) -> bool:
    # FastAPI's {item_id} and typed {item_id:int} placeholders match one URL segment.
    regex = re.escape(route)
    regex = re.sub(r"\\\{[^{}\/]+\\\}", r"[^/]+", regex)
    return re.fullmatch(regex, request) is not None


def _scan_side(scope: Path, root: Path, extensions: frozenset[str]) -> tuple[list[tuple[str, str]], bool]:
    scan: dict[str, Any] = {"entries": 0, "files": 0, "bytes": 0, "truncated": False}
    collected: list[tuple[str, str]] = []
    for path, relative in _iter_search_files(scope, scan, workspace=root):
        if scan["entries"] > MAX_SEARCH_ENTRIES:
            scan["truncated"] = True
            break
        if path.suffix.lower() not in extensions:
            continue
        scan["files"] += 1
        if scan["files"] > _MAX_FILES_PER_SIDE:
            scan["truncated"] = True
            break
        try:
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES or size + scan["bytes"] > _MAX_BYTES_PER_SIDE:
                scan["truncated"] = True
                continue
            data = sandbox.read_bounded_bytes(path, _MAX_FILE_BYTES)
            text = data.decode("utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        scan["bytes"] += len(data)
        collected.append((relative.as_posix(), text))
    return collected, bool(scan["truncated"])


def inspect_fullstack_interfaces(
    backend_path: str = "backend",
    frontend_path: str = "frontend",
    *,
    workspace: Path,
) -> str:
    """Compare statically declared FastAPI routes and literal frontend HTTP calls."""
    root = workspace.resolve()
    backend = resolve_inside_workspace(backend_path, root)
    frontend = resolve_inside_workspace(frontend_path, root)
    if not backend.is_dir() or not frontend.is_dir():
        raise NotADirectoryError("后端和前端路径均须为工作区内已存在的目录")
    backend_files, backend_truncated = _scan_side(backend, root, frozenset({".py"}))
    frontend_files, frontend_truncated = _scan_side(
        frontend, root, frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}),
    )
    routes = [route for path, text in backend_files for route in _fastapi_routes(text, path)]
    calls = [call for path, text in frontend_files for call in _frontend_calls(text, path)]
    routes.sort(key=lambda item: (item["path"], item["method"], item["file"], item["line"]))
    calls.sort(key=lambda item: (item["file"], item["line"], item["method"]))
    truncated = backend_truncated or frontend_truncated
    if len(routes) > _MAX_ENDPOINTS or len(calls) > _MAX_ENDPOINTS:
        truncated = True
        routes = routes[:_MAX_ENDPOINTS]
        calls = calls[:_MAX_ENDPOINTS]
    for call in calls:
        matching = [
            {"file": route["file"], "line": route["line"], "handler": route["handler"]}
            for route in routes
            if call["method"] == route["method"] and _endpoint_matches(route["path"], call["path"])
        ]
        call["status"] = "matched" if matching else "unmatched"
        if matching:
            call["backend_matches"] = matching[:3]
    result: dict[str, Any] = {
        "backend_path": backend_path, "frontend_path": frontend_path,
        "backend_files_scanned": len(backend_files), "frontend_files_scanned": len(frontend_files),
        "route_count": len(routes), "frontend_call_count": len(calls),
        "unmatched_frontend_calls": sum(call["status"] == "unmatched" for call in calls),
        "routes": routes, "frontend_calls": calls,
        "truncated": truncated,
        "scope": "Literal FastAPI decorators/APIRouter prefixes and fetch/axios/client/api calls only; "
                 "runtime router registration, base URLs, aliases, template strings and dynamic routes are not resolved.",
    }
    serialized = json.dumps(result, ensure_ascii=False)
    while len(serialized) > MAX_TOOL_RESULT_CHARS and (routes or calls):
        (routes if len(routes) > len(calls) else calls).pop()
        result["truncated"] = True
        serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("前后端接口分析结果太长，请缩小项目范围")
    return sandbox.redact_text(serialized)


def build_fullstack_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool
    def inspect_fullstack_interfaces_tool(
        backend_path: str = "backend", frontend_path: str = "frontend",
    ) -> str:
        """Compare FastAPI route declarations with literal JS/TS HTTP calls.

        Use before coordinating backend API and frontend call changes.
        Return unmatched calls with source file/line and backend matches.
        Static source hints only: does not start a server or run frontend tests.
        """
        return inspect_fullstack_interfaces(
            backend_path, frontend_path, workspace=root,
        )

    inspect_fullstack_interfaces_tool.name = "inspect_fullstack_interfaces"
    inspect_fullstack_interfaces_tool.metadata = {
        "risk": "read", "mutation": False,
        "workspace_arguments": ("backend_path", "frontend_path"),
    }
    return [inspect_fullstack_interfaces_tool]
