"""Identify frontend HTTP call sites affected by OpenAPI contract changes.

Compare two already exported OpenAPI 3 JSON files; don't execute project
code or attempt to infer undocumented runtime endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain.tools import tool

from . import fullstack
from . import openapi_contract as openapi
from . import sandbox
from .config import MAX_TOOL_RESULT_CHARS
from .tools.filesystem import resolve_inside_workspace

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
_MAX_ITEMS = 60


def _operations(data: dict[str, Any]) -> dict[tuple[str, str], Any]:
    out: dict[tuple[str, str], Any] = {}
    for path, entries in data["paths"].items():
        if not isinstance(path, str) or not isinstance(entries, dict):
            continue
        for method in _METHODS:
            if isinstance(entries.get(method), dict):
                out[(method.upper(), path)] = entries[method]
    return out


def _schemas(data: dict[str, Any]) -> dict[str, Any]:
    components = data.get("components", {})
    if not isinstance(components, dict):
        return {}
    schemas = components.get("schemas", {})
    return schemas if isinstance(schemas, dict) else {}


def inspect_openapi_changes(
    old_schema_path: str = "backend/openapi.previous.json",
    new_schema_path: str = "backend/openapi.json",
    frontend_path: str = "frontend",
    *,
    workspace: Path,
) -> str:
    """Report added/removed/modified operations and matching frontend usages."""
    root = workspace.resolve()
    _, old = openapi._read_contract(old_schema_path, root)
    _, new = openapi._read_contract(new_schema_path, root)
    frontend = resolve_inside_workspace(frontend_path, root)
    if not frontend.is_dir():
        raise NotADirectoryError("前端目录不存在")
    old_ops = _operations(old)
    new_ops = _operations(new)
    keys = sorted(old_ops.keys() | new_ops.keys())
    change_summary: list[dict[str, Any]] = []
    total_changes = 0
    for method, path in keys:
        key = (method, path)
        if key not in old_ops:
            change = "added"
        elif key not in new_ops:
            change = "removed"
        elif old_ops[key] != new_ops[key]:
            change = "modified"
        else:
            continue
        total_changes += 1
        if len(change_summary) < _MAX_ITEMS:
            change_summary.append({"method": method, "path": path, "change": change})

    a, b = _schemas(old), _schemas(new)
    schema_names = sorted(set(a) | set(b))
    modified_schemas = [name for name in schema_names if a.get(name) != b.get(name)]
    files, frontend_truncated = fullstack._scan_side(
        frontend, root, frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}),
    )
    calls = [call for file, contents in files for call in fullstack._frontend_calls(contents, file)]
    affected: list[dict[str, Any]] = []
    for call in calls:
        for operation in change_summary:
            if operation["change"] == "added":
                continue
            if (call["method"] == operation["method"]
                    and fullstack._endpoint_matches(operation["path"], call["path"])):
                if len(affected) < _MAX_ITEMS:
                    affected.append({
                        "file": call["file"], "line": call["line"],
                        "method": call["method"], "path": call["path"],
                        "change": operation["change"],
                    })
                break
    result: dict[str, Any] = {
        "old_schema": old_schema_path, "new_schema": new_schema_path,
        "frontend": frontend_path,
        "operation_changes_total": total_changes,
        "operation_changes": change_summary,
        "schema_changes_total": len(modified_schemas),
        "changed_schema_names": modified_schemas[:_MAX_ITEMS],
        "frontend_calls_scanned": len(calls),
        "affected_frontend_calls": affected,
        "truncated": frontend_truncated or total_changes > len(change_summary)
            or len(modified_schemas) > _MAX_ITEMS or len(affected) >= _MAX_ITEMS,
        "note": "Static literal-path references only. Changes to shared schemas can affect "
                "additional callers not present in this list. Review generated TS typecheck output.",
    }
    encoded = json.dumps(result, ensure_ascii=False)
    while len(encoded) > MAX_TOOL_RESULT_CHARS and (affected or change_summary):
        if affected:
            affected.pop()
        else:
            change_summary.pop()
        result["truncated"] = True
        encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("OpenAPI 变更范围过大，请拆分前端子目录")
    return sandbox.redact_text(encoded)


def build_openapi_impact_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool
    def inspect_openapi_changes_tool(
        old_schema_path: str = "backend/openapi.previous.json",
        new_schema_path: str = "backend/openapi.json",
        frontend_path: str = "frontend",
    ) -> str:
        """Compare two exported OpenAPI versions and show impacted frontend calls.

        Reports changed routes, methods, named schemas and code locations.
        Use before updating generated types, hooks and UI. Static hints only.
        """
        return inspect_openapi_changes(
            old_schema_path, new_schema_path, frontend_path, workspace=root,
        )

    inspect_openapi_changes_tool.name = "inspect_openapi_changes"
    inspect_openapi_changes_tool.metadata = {
        "risk": "read", "mutation": False,
        "workspace_arguments": ("old_schema_path", "new_schema_path", "frontend_path"),
    }
    return [inspect_openapi_changes_tool]
