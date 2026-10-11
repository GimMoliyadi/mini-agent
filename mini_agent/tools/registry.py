"""Canonical LangChain tool construction for the local agent.

The handlers remain small, dependency-free functions.  ``build_tools`` binds
one explicit workspace to those handlers and returns the only tool collection
the Agent should expose to a model.  Runtime-specific control flow such as
``finish_task`` is intentionally kept outside this registry.
"""

from __future__ import annotations

from functools import partial, wraps
from inspect import Signature, signature
from pathlib import Path
from typing import Any, Callable, Mapping

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from .command import run_command
from .filesystem import apply_patch, list_files, read_file, rename_file, search_text, write_file
from .introspection import inspect_capabilities, inspect_project
from ..result import ToolResult


ToolHandler = Callable[..., object]


class ListFilesInput(BaseModel):
    path: str | None = None


class ReadFileInput(BaseModel):
    path: str
    start_line: int = 1
    max_lines: int = 100


class SearchTextInput(BaseModel):
    query: str
    path: str = "."
    max_results: int = 20


class WriteFileInput(BaseModel):
    path: str
    content: str


class ApplyPatchInput(BaseModel):
    path: str
    old_text: str
    new_text: str


class RenameFileInput(BaseModel):
    source: str
    destination: str


class RunCommandInput(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str = "."


class InspectProjectInput(BaseModel):
    path: str | None = None
    start_line: int = 1
    max_lines: int = 100


class NoArgumentsInput(BaseModel):
    pass


def _tool_artifact(value: object) -> tuple[str, dict[str, Any]]:
    """Convert a handler result to LangChain's content/artifact pair."""
    if isinstance(value, ToolResult):
        return value.text, {
            "status": value.status,
            "code": value.code,
            "metadata": dict(value.metadata),
        }
    return str(value), {"status": "success", "code": "ok", "metadata": {}}


def _make_tool(
    *,
    name: str,
    description: str,
    handler: ToolHandler,
    workspace: Path | None,
    metadata: Mapping[str, Any],
    args_schema: type[BaseModel],
    inject_workspace: bool = True,
) -> StructuredTool:
    """Build one StructuredTool without exposing the injected workspace arg."""
    bound: ToolHandler = (
        partial(handler, workspace=workspace) if inject_workspace else handler
    )

    @wraps(handler)
    def invoke(*args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        return _tool_artifact(bound(*args, **kwargs))

    # ``StructuredTool`` infers a Pydantic schema from inspect.signature.  A
    # wrapped partial has the useful model-facing parameters, while the bound
    # keyword-only workspace remains hidden from that schema.
    invoke.__signature__ = signature(bound)  # type: ignore[attr-defined]
    return StructuredTool.from_function(
        func=invoke,
        name=name,
        description=description,
        args_schema=args_schema,
        response_format="content_and_artifact",
        metadata=dict(metadata),
    )


def _metadata(
    *,
    risk: str,
    mutation: bool,
    workspace_arguments: tuple[str, ...] = (),
    mutation_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "risk": risk,
        "mutation": mutation,
        "workspace_arguments": workspace_arguments,
    }
    if mutation_paths:
        values["mutation_paths"] = mutation_paths
    return values


def build_tools(
    workspace: Path,
    *,
    runtime_context: Mapping[str, object] | None = None,
) -> list[BaseTool]:
    """Return the canonical tools bound to ``workspace``.

    ``workspace`` is captured by closures/partials and is never a model input.
    The returned list contains only operational and read-only tools; runtime
    control-flow tools are registered by the Agent assembly layer.
    """
    root = Path(workspace).expanduser().resolve()
    bound_tools: list[BaseTool] = []

    bound_tools.extend(
        [
            _make_tool(
                name="list_files",
                description=(
                    "列出工作目录内指定目录的一层内容；省略 path 时列出工作目录。"
                ),
                handler=list_files,
                workspace=root,
                args_schema=ListFilesInput,
                metadata=_metadata(
                    risk="read", mutation=False, workspace_arguments=("path",)
                ),
            ),
            _make_tool(
                name="read_file",
                description="读取工作目录内 UTF-8 文件的一段文本内容。",
                handler=read_file,
                workspace=root,
                args_schema=ReadFileInput,
                metadata=_metadata(
                    risk="read", mutation=False, workspace_arguments=("path",)
                ),
            ),
            _make_tool(
                name="search_text",
                description="在工作目录内递归搜索固定字符串并返回匹配上下文。",
                handler=search_text,
                workspace=root,
                args_schema=SearchTextInput,
                metadata=_metadata(
                    risk="read", mutation=False, workspace_arguments=("path",)
                ),
            ),
            _make_tool(
                name="write_file",
                description="把完整 UTF-8 文本写入工作目录内的文件；可能覆盖已有文件。",
                handler=write_file,
                workspace=root,
                args_schema=WriteFileInput,
                metadata=_metadata(
                    risk="write",
                    mutation=True,
                    workspace_arguments=("path",),
                    mutation_paths=("path",),
                ),
            ),
            _make_tool(
                name="apply_patch",
                description="在工作目录内已有 UTF-8 文件中唯一精确替换一段文本。",
                handler=apply_patch,
                workspace=root,
                args_schema=ApplyPatchInput,
                metadata=_metadata(
                    risk="write",
                    mutation=True,
                    workspace_arguments=("path",),
                    mutation_paths=("path",),
                ),
            ),
            _make_tool(
                name="rename_file",
                description="重命名工作目录内文件；目标存在时拒绝覆盖。",
                handler=rename_file,
                workspace=root,
                args_schema=RenameFileInput,
                metadata=_metadata(
                    risk="write",
                    mutation=True,
                    workspace_arguments=("source", "destination"),
                    mutation_paths=("source", "destination"),
                ),
            ),
            _make_tool(
                name="run_command",
                description="在工作目录内执行受控的无 shell 本地开发命令。",
                handler=run_command,
                workspace=root,
                args_schema=RunCommandInput,
                metadata=_metadata(
                    risk="execute", mutation=False, workspace_arguments=("cwd",)
                ),
            ),
            _make_tool(
                name="inspect_project",
                description="只读查看 Agent 项目允许公开的源码文件并支持分页。",
                handler=inspect_project,
                workspace=None,
                args_schema=InspectProjectInput,
                metadata=_metadata(risk="read", mutation=False),
                inject_workspace=False,
            ),
        ]
    )

    def capabilities() -> tuple[str, dict[str, Any]]:
        result = inspect_capabilities(
            tools=bound_tools,
            runtime_context=runtime_context,
        )
        return _tool_artifact(result)

    capabilities.__signature__ = Signature()  # type: ignore[attr-defined]
    bound_tools.append(
        StructuredTool.from_function(
            func=capabilities,
            name="inspect_capabilities",
            description="只读查看当前可调用工具及由 Runtime 注入的能力状态。",
            args_schema=NoArgumentsInput,
            response_format="content_and_artifact",
            metadata=_metadata(risk="read", mutation=False),
        )
    )
    return bound_tools


__all__ = [
    "BaseTool",
    "StructuredTool",
    "build_tools",
]
