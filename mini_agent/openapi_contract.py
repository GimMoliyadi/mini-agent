"""Inspect exported OpenAPI contracts and generate TS types with openapi-typescript.

Parsing is intentionally small and read-only; full OpenAPI/JSON Schema type
semantics belong to the mature, project-installed openapi-typescript CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from langchain.tools import tool

from . import dev_environment as dev
from . import sandbox
from .config import MAX_TOOL_RESULT_CHARS
from .result import ResultStatus, ToolResult

_MAX_CONTRACT_BYTES = 2 * 1024 * 1024
_MAX_SUMMARY_OPERATIONS = 60
_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


def _read_contract(path: str, root: Path) -> tuple[Path, dict[str, Any]]:
    file = sandbox.resolve_path(path, root)
    if not file.is_file() or file.suffix.lower() != ".json":
        raise ValueError("当前 OpenAPI 契约解析要求工作区内已有的 .json 文件")
    data = json.loads(sandbox.read_bounded_bytes(file, _MAX_CONTRACT_BYTES).decode("utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("openapi"), str):
        raise ValueError("JSON 不是有效的 OpenAPI 3.x 根对象")
    if not data["openapi"].startswith("3."):
        raise ValueError("目前只支持 OpenAPI 3.x；请先转换 Swagger/OpenAPI 2.x")
    if not isinstance(data.get("paths"), dict):
        raise ValueError("OpenAPI paths 必须是 JSON 对象")
    return file, data


def inspect_openapi_contract(schema_path: str = "backend/openapi.json", *, workspace: Path) -> str:
    """Summarize OpenAPI methods/paths/components without interpreting schema types."""
    root = workspace.resolve()
    _, contract = _read_contract(schema_path, root)
    title = contract.get("info", {})
    title = title.get("title", "") if isinstance(title, dict) else ""
    components = contract.get("components", {})
    components = components.get("schemas", {}) if isinstance(components, dict) else {}
    schemas = sorted(str(key) for key in components) if isinstance(components, dict) else []
    operations: list[dict[str, Any]] = []
    total = 0
    for path, path_info in sorted(contract["paths"].items()):
        if not isinstance(path, str) or not isinstance(path_info, dict):
            continue
        for method in _METHODS:
            operation = path_info.get(method)
            if not isinstance(operation, dict):
                continue
            total += 1
            if len(operations) < _MAX_SUMMARY_OPERATIONS:
                operations.append({
                    "method": method.upper(), "path": path,
                    "operation_id": str(operation.get("operationId") or ""),
                    "request_body": "requestBody" in operation,
                    "response_codes": sorted(str(key) for key in operation.get("responses", {}))
                    if isinstance(operation.get("responses"), dict) else [],
                })
    result: dict[str, Any] = {
        "schema_path": schema_path, "title": str(title),
        "openapi_version": contract["openapi"],
        "operation_count": total, "operations": operations,
        "component_schemas": schemas[:60],
        "component_schema_count": len(schemas),
        "truncated": total > len(operations) or len(schemas) > 60,
        "next_step": "Install openapi-typescript in frontend and call generate_openapi_types",
    }
    while len(json.dumps(result, ensure_ascii=False)) > MAX_TOOL_RESULT_CHARS and operations:
        operations.pop()
        result["truncated"] = True
    output = json.dumps(result, ensure_ascii=False)
    if len(output) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("OpenAPI 摘要超出工具输出上限")
    return sandbox.redact_text(output)


def generate_openapi_types(
    schema_path: str,
    frontend_path: str,
    output_path: str,
    *,
    workspace: Path,
) -> ToolResult:
    """Generate project-local TypeScript types via an already-installed CLI."""
    root = workspace.resolve()
    schema, contract = _read_contract(schema_path, root)
    frontend = dev._project(root, frontend_path)
    dev._manager(frontend, "node")
    target = sandbox.resolve_path(output_path, root)
    if not target.is_relative_to(frontend):
        raise PermissionError("输出类型文件必须位于指定前端项目内")
    if not target.name.endswith((".ts", ".d.ts")) or target.name.endswith((".tsx", ".jsx")):
        raise ValueError("生成目标必须是 .ts 或 .d.ts 文件")
    if not target.parent.is_dir():
        raise FileNotFoundError("请先使用 create_files_batch 或 write_file 创建输出目录")
    if target.exists() and not target.is_file():
        raise ValueError("目标路径必须是普通文件")
    package = dev._manifest(frontend) or {}
    installed = package.get("devDependencies", {})
    regular = package.get("dependencies", {})
    if not any(isinstance(group, dict) and "openapi-typescript" in group for group in (installed, regular)):
        raise ValueError("前端需先在 package.json 声明 openapi-typescript，然后 prepare_dev_environment 安装依赖")
    cli = frontend / "node_modules" / "openapi-typescript" / "bin" / "cli.js"
    if not cli.is_file():
        raise ValueError("未找到项目本地 openapi-typescript；先执行 prepare_dev_environment(path=frontend_path, language=node)")
    with tempfile.NamedTemporaryFile(
        prefix=".mini-agent-types-", suffix=".ts",
        dir=target.parent, delete=False,
    ) as temp:
        temporary = Path(temp.name)
    temporary.unlink(missing_ok=True)
    try:
        result = dev._execute(
            ["node", str(cli), str(schema), "-o", str(temporary)],
            frontend, root, timeout=120,
        )
        if result.status != "success":
            return result
        generated = sandbox.read_bounded_bytes(temporary)
        if not generated:
            raise ValueError("类型生成器返回成功但未写出内容")
        with sandbox.file_change({target: generated}, root):
            sandbox.atomic_write_bytes(target, generated)
    finally:
        temporary.unlink(missing_ok=True)
    return ToolResult(
        sandbox.redact_text(json.dumps({
            "schema": schema.relative_to(root).as_posix(),
            "output": target.relative_to(root).as_posix(),
            "bytes": len(generated), "openapi_version": contract["openapi"],
            "generator": "openapi-typescript",
        }, ensure_ascii=False)),
        "success", "openapi_types_generated",
        {"output": target.relative_to(root).as_posix(), "bytes": len(generated)},
    )


def build_openapi_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool
    def inspect_openapi_contract_tool(schema_path: str = "backend/openapi.json") -> str:
        """Inspect OpenAPI JSON, list backend endpoints and named schemas.

        Use a locally exported FastAPI openapi.json file. This does not
        execute backend code or infer types; generator uses openapi-typescript.
        """
        return inspect_openapi_contract(schema_path, workspace=root)

    inspect_openapi_contract_tool.name = "inspect_openapi_contract"
    inspect_openapi_contract_tool.metadata = {
        "risk": "read", "mutation": False,
        "workspace_arguments": ("schema_path",),
    }

    @tool(response_format="content_and_artifact")
    def generate_openapi_types_tool(
        schema_path: str = "backend/openapi.json",
        frontend_path: str = "frontend",
        output_path: str = "frontend/src/openapi.d.ts",
    ) -> tuple[str, dict[str, Any]]:
        """Generate TypeScript declarations from OpenAPI JSON with installed openapi-typescript.

        The frontend project must already declare and install openapi-typescript.
        Output location must already have a directory; writes .ts or .d.ts.
        In ASK mode requires approval. Run frontend typecheck/build afterwards.
        """
        result = generate_openapi_types(
            schema_path, frontend_path, output_path, workspace=root,
        )
        return dev._outcome(result)

    generate_openapi_types_tool.name = "generate_openapi_types"
    generate_openapi_types_tool.metadata = {
        "risk": "write", "mutation": True,
        "workspace_arguments": ("schema_path", "frontend_path", "output_path"),
        "mutation_paths": ("output_path",),
    }
    return [inspect_openapi_contract_tool, generate_openapi_types_tool]
