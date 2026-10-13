"""FastAPI OpenAPI export and typed frontend client scaffolding.

Both tools extend the existing OpenAPI + full-stack workflow: the backend's
project-local interpreter exports its schema; the frontend uses the mature
openapi-fetch package and types from openapi-typescript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from langchain.tools import tool

from . import dev_environment as dev
from . import openapi_contract
from . import sandbox
from .result import ToolResult

_IMPORT_SPEC = re.compile(r"^([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*):([a-zA-Z_]\w*)$")
_EXPORT_SCRIPT = (
    "import importlib, json, pathlib, sys\n"
    "app = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])\n"
    "schema = app.openapi()\n"
    "if not isinstance(schema, dict): raise TypeError('FastAPI openapi() returned a non-object')\n"
    "pathlib.Path(sys.argv[3]).write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')\n"
)


def export_fastapi_openapi(
    *, workspace: Path, backend_path: str = "backend",
    app_import: str = "main:app", output_path: str = "backend/openapi.json",
) -> ToolResult:
    """Export a real local FastAPI application's openapi() without a server."""
    root = workspace.resolve()
    backend = dev._project(root, backend_path)
    dev._manager(backend, "python")
    match = _IMPORT_SPEC.fullmatch(app_import)
    if match is None:
        raise ValueError("app_import 应采用 Python 模块:对象 的格式，例如 app.main:app")
    interpreter = dev._python_exe(backend)
    if not interpreter.is_file():
        raise ValueError("后端 .venv 尚未就绪，请先用 prepare_dev_environment 准备 Python 环境")
    target = sandbox.resolve_path(output_path, root)
    if not target.is_relative_to(backend) or target.suffix.lower() != ".json":
        raise ValueError("导出的 OpenAPI JSON 必须位于所选后端项目内")
    if not target.parent.is_dir():
        raise FileNotFoundError("输出目录不存在；请先创建目录")
    if target.exists() and not target.is_file():
        raise ValueError("输出路径必须对应普通文件")
    with tempfile.NamedTemporaryFile(
        prefix=".mini-agent-export-", suffix=".json", dir=target.parent, delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        outcome = dev._execute(
            [str(interpreter), "-c", _EXPORT_SCRIPT, match.group(1), match.group(2), str(temporary)],
            backend, root, timeout=120,
        )
        if outcome.status != "success":
            return outcome
        # Ensure the generated document is consumable by the existing tool.
        _, spec = openapi_contract._read_contract(
            temporary.relative_to(root).as_posix(), root,
        )
        generated = sandbox.read_bounded_bytes(temporary)
        with sandbox.file_change({target: generated}, root):
            sandbox.atomic_write_bytes(target, generated)
    finally:
        temporary.unlink(missing_ok=True)
    return ToolResult(
        sandbox.redact_text(json.dumps({
            "schema": target.relative_to(root).as_posix(),
            "openapi_version": spec["openapi"], "bytes": len(generated),
            "app": app_import, "next_step": "generate_openapi_types",
        }, ensure_ascii=False)),
        "success", "fastapi_openapi_exported",
        {"output": target.relative_to(root).as_posix(), "bytes": len(generated)},
    )


def _types_module_import(types_path: Path, client_path: Path) -> str:
    """Build an ESM import path from a generated .d.ts or .ts file."""
    name = types_path.name
    suffix = ".d.ts" if name.endswith(".d.ts") else ".ts"
    if not name.endswith((".ts", ".d.ts")) or name.endswith(".tsx"):
        raise ValueError("TypeScript 类型必须位于 .ts 或 .d.ts 文件")
    module = os.path.relpath(types_path, start=client_path.parent).replace("\\", "/")
    module = module[:-len(suffix)]
    return module if module.startswith(".") else "./" + module


def generate_typed_api_client(
    *, workspace: Path, frontend_path: str = "frontend",
    types_path: str = "frontend/src/openapi.d.ts",
    output_path: str = "frontend/src/api-client.ts",
    base_url: str = "",
) -> ToolResult:
    """Create a typed openapi-fetch API client reusing generated paths."""
    root = workspace.resolve()
    frontend = dev._project(root, frontend_path)
    dev._manager(frontend, "node")
    types = sandbox.resolve_path(types_path, root)
    target = sandbox.resolve_path(output_path, root)
    if not types.is_relative_to(frontend) or not types.is_file():
        raise FileNotFoundError("先执行 generate_openapi_types 生成前端 TypeScript 类型文件")
    if not target.is_relative_to(frontend) or target.suffix != ".ts" or target.name.endswith(".d.ts"):
        raise ValueError("类型化 API 客户端必须是前端项目内的 .ts 源码文件")
    if target == types:
        raise ValueError("API 客户端不能覆盖 OpenAPI 类型文件")
    if not target.parent.is_dir():
        raise FileNotFoundError("客户端输出目录不存在；请先创建对应目录")
    package = dev._manifest(frontend) or {}
    groups = (package.get("dependencies"), package.get("devDependencies"))
    if not any(isinstance(group, dict) and "openapi-fetch" in group for group in groups):
        raise ValueError("请先将 openapi-fetch 声明为前端项目依赖，并用 prepare_dev_environment 安装")
    if not isinstance(base_url, str) or len(base_url) > 500 or "\n" in base_url or "\r" in base_url:
        raise ValueError("base_url 应为简短的 URL 字符串")
    module = _types_module_import(types, target)
    generated = (
        "// Generated by mini-agent. Uses openapi-typescript types and openapi-fetch.\n"
        'import createClient from "openapi-fetch";\n'
        f"import type {{ paths }} from {json.dumps(module)};\n\n"
        f"export const api = createClient<paths>({{ baseUrl: {json.dumps(base_url)} }});\n"
    )
    content = sandbox.encode_content(generated)
    with sandbox.file_change({target: content}, root):
        sandbox.atomic_write_bytes(target, content)
    return ToolResult(
        sandbox.redact_text(json.dumps({
            "output": target.relative_to(root).as_posix(),
            "types": types.relative_to(root).as_posix(),
            "package": "openapi-fetch", "base_url": base_url,
        }, ensure_ascii=False)),
        "success", "typed_api_client_generated",
        {"output": target.relative_to(root).as_posix(), "bytes": len(content)},
    )


def build_fullstack_codegen_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool(response_format="content_and_artifact")
    def export_fastapi_openapi_tool(
        backend_path: str = "backend",
        app_import: str = "main:app",
        output_path: str = "backend/openapi.json",
    ) -> tuple[str, dict[str, Any]]:
        """Export an installed FastAPI application's OpenAPI JSON without serving HTTP.

        app_import is a Python import target like app.main:app. Requires the
        backend's prepared local .venv and performs project code import.
        """
        result = export_fastapi_openapi(
            workspace=root, backend_path=backend_path,
            app_import=app_import, output_path=output_path,
        )
        return dev._outcome(result)

    export_fastapi_openapi_tool.name = "export_fastapi_openapi"
    export_fastapi_openapi_tool.metadata = {
        "risk": "execute", "mutation": True,
        "workspace_arguments": ("backend_path", "output_path"),
        "mutation_paths": ("output_path",),
    }

    @tool(response_format="content_and_artifact")
    def generate_typed_api_client_tool(
        frontend_path: str = "frontend",
        types_path: str = "frontend/src/openapi.d.ts",
        output_path: str = "frontend/src/api-client.ts",
        base_url: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Generate a typed openapi-fetch API client from existing OpenAPI TS types.

        Requires project package.json to declare openapi-fetch, and a generated
        paths interface from generate_openapi_types. Supports optional base URL.
        """
        result = generate_typed_api_client(
            workspace=root, frontend_path=frontend_path,
            types_path=types_path, output_path=output_path, base_url=base_url,
        )
        return dev._outcome(result)

    generate_typed_api_client_tool.name = "generate_typed_api_client"
    generate_typed_api_client_tool.metadata = {
        "risk": "write", "mutation": True,
        "workspace_arguments": ("frontend_path", "types_path", "output_path"),
        "mutation_paths": ("output_path",),
    }
    return [export_fastapi_openapi_tool, generate_typed_api_client_tool]
