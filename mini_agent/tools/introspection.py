"""Read-only project and runtime introspection tools."""

import json

from mini_agent.config import PROJECT_ROOT
from .filesystem import _read_file_range

_PROJECT_INSPECTION_FILES = frozenset({
    "README.md", "main.py", "tools.py", "config.py", "capabilities.py",
    "session.py", "recovery.py", "acceptance.py", "cli.py", "agent.cmd",
    "requirements.txt",
    "mini_agent/__init__.py", "mini_agent/agent.py", "mini_agent/runtime.py",
    "mini_agent/config.py", "mini_agent/context.py", "mini_agent/permissions.py",
    "mini_agent/sandbox.py", "mini_agent/recovery.py", "mini_agent/verifier.py",
    "mini_agent/session.py", "mini_agent/trace.py", "mini_agent/result.py",
    "mini_agent/tools/__init__.py", "mini_agent/tools/registry.py",
    "mini_agent/tools/filesystem.py", "mini_agent/tools/command.py",
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

def finish_task(summary: str) -> str:
    """Validate the model-facing finish summary for the control-flow dispatcher."""
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 必须是非空字符串")
    return summary.strip()


def inspect_capabilities(*, runtime_context: dict | None = None) -> str:
    from capabilities import capability_snapshot

    return json.dumps(capability_snapshot(runtime_context or {}), ensure_ascii=False, separators=(",", ":"))
