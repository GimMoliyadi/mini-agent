"""Read-only project and runtime introspection tools."""

import json
from collections.abc import Iterable, Mapping

from mini_agent.config import PROJECT_ROOT
from .filesystem import _read_file_range

_PROJECT_INSPECTION_FILES = frozenset({
    "README.md",
    "pyproject.toml",
    "main.py",
    "tools.py",
    "config.py",
    "cli.py",
    "launcher.py",
    "configure.py",
    "agent.cmd",
    "mini.cmd",
    "file_safety.py",
    "process_runner.py",
    "runtime_guards.py",
    "docs/framework-references.md",
    "docs/architecture.md",
    "docs/migration.md",
    "mini_agent/__init__.py",
    "mini_agent/cli.py",
    "mini_agent/config.py",
    "mini_agent/configure.py",
    "mini_agent/context.py",
    "mini_agent/launcher.py",
    "mini_agent/models.py",
    "mini_agent/observability.py",
    "mini_agent/persistence.py",
    "mini_agent/policies.py",
    "mini_agent/process_runner.py",
    "mini_agent/result.py",
    "mini_agent/runtime.py",
    "mini_agent/runtime_guards.py",
    "mini_agent/sandbox.py",
    "mini_agent/settings.py",
    "mini_agent/state.py",
    "mini_agent/verification.py",
    "mini_agent/verifier.py",
    "mini_agent/tools/__init__.py",
    "mini_agent/tools/registry.py",
    "mini_agent/tools/filesystem.py",
    "mini_agent/tools/command.py",
    "mini_agent/tools/introspection.py",
})

def inspect_project(
    path: str | None = None, start_line: int = 1, max_lines: int = 100
) -> str:
    """List or read a fixed set of this Agent's project files, never the workspace."""
    if path is None:
        return "可读的 Agent 项目文件：\n" + "\n".join(sorted(_PROJECT_INSPECTION_FILES))
    if not isinstance(path, str) or path not in _PROJECT_INSPECTION_FILES:
        raise PermissionError("只能读取 inspect_project 列出的项目文件")
    target = (PROJECT_ROOT / path).resolve()
    if not target.is_relative_to(PROJECT_ROOT.resolve()):
        raise PermissionError("项目文件指向项目目录之外，拒绝读取")
    return _read_file_range(target, path, start_line, max_lines)

def inspect_capabilities(
    *,
    tools: Iterable[object] = (),
    runtime_context: Mapping[str, object] | None = None,
) -> str:
    """Return a read-only view of the injected tool/runtime capabilities.

    The tool layer must stay independent from the Agent runtime.  The caller
    supplies the current tool objects and optional runtime facts, so this
    helper never imports or reaches into runtime state itself.
    """
    callable_tools = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            continue
        metadata = dict(getattr(tool, "metadata", None) or {})
        item = {
            "name": name,
            "description": getattr(tool, "description", ""),
            "risk": metadata.get("risk"),
            "mutation": bool(metadata.get("mutation", False)),
            "workspace_arguments": list(metadata.get("workspace_arguments", ())),
        }
        if "mutation_paths" in metadata:
            item["mutation_paths"] = list(metadata["mutation_paths"])
        callable_tools.append(item)

    context = dict(runtime_context or {})
    features = context.pop("runtime_features", context)
    snapshot = {
        "callable_tools": callable_tools,
        "runtime_features": features,
    }
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
