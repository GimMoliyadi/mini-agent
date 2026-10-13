"""Run Python backend tests and declared Node frontend checks as one workflow.

Reuses the existing project-local interpreters, script discovery, bounded
process runner and structured error locations; no new agent execution loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool

from . import dev_environment as dev
from .config import MAX_TOOL_RESULT_CHARS
from .result import ResultStatus, ToolResult


def run_fullstack_checks(
    *,
    workspace: Path,
    backend_path: str = "backend",
    frontend_path: str = "frontend",
    backend_task: Literal["test", "unittest"] = "test",
    frontend_task: Literal["test", "build", "lint", "typecheck"] = "build",
    backend_test_files: list[str] | None = None,
) -> ToolResult:
    """Execute both project tasks and report their results even if one fails."""
    root = workspace.resolve()
    backend = dev._project(root, backend_path)
    frontend = dev._project(root, frontend_path)
    dev._manager(backend, "python")
    node_manager = dev._manager(frontend, "node")
    if backend_task not in {"test", "unittest"}:
        raise ValueError("backend_task 只能是 test 或 unittest")
    if frontend_task not in {"test", "build", "lint", "typecheck"}:
        raise ValueError("frontend_task 必须是 test、build、lint 或 typecheck")
    python = dev._python_exe(backend)
    if not python.is_file():
        raise ValueError("后端项目未准备 Python .venv；请先使用 prepare_dev_environment")
    manifest = dev._manifest(frontend) or {}
    if frontend_task not in manifest.get("scripts", {}):
        raise ValueError("前端 package.json 中没有所选脚本，请先查看 inspect_dev_environment")
    targets = dev._test_file_targets(backend, backend_test_files or [])
    if backend_task == "test":
        backend_argv = [str(python), "-m", "pytest", "-q"]
        if targets:
            backend_argv += ["--", *targets]
    else:
        backend_argv = [str(python), "-m", "unittest", "discover"]
        if targets:
            backend_argv = [str(python), "-m", "unittest", "--", *targets]
    backend_result = dev._execute(backend_argv, backend, root, timeout=120)
    frontend_result = dev._execute(
        [node_manager, "run", frontend_task], frontend, root, timeout=120,
    )
    succeeded = backend_result.status == "success" and frontend_result.status == "success"
    reports: dict[str, Any] = {
        "backend": {
            "path": backend_path, "task": backend_task,
            "status": backend_result.status, "code": backend_result.code,
            "metadata": dict(backend_result.metadata),
        },
        "frontend": {
            "path": frontend_path, "task": frontend_task,
            "status": frontend_result.status, "code": frontend_result.code,
            "metadata": dict(frontend_result.metadata),
        },
        "both_passed": succeeded,
    }
    summary = json.dumps(reports, ensure_ascii=False)
    combined = (
        summary + "\n\n[BACKEND]\n" + backend_result.text
        + "\n\n[FRONTEND]\n" + frontend_result.text
    )
    if len(combined) > MAX_TOOL_RESULT_CHARS:
        # Preserve the structured summary and useful portions of both logs.
        remaining = max(0, (MAX_TOOL_RESULT_CHARS - len(summary) - 160) // 2)
        combined = (
            summary + "\n\n[BACKEND]\n" + backend_result.text[-remaining:]
            + "\n[BACKEND OUTPUT TRUNCATED]\n[FRONTEND]\n"
            + frontend_result.text[-remaining:] + "\n[FRONTEND OUTPUT TRUNCATED]"
        )
    if len(combined) > MAX_TOOL_RESULT_CHARS:
        combined = combined[:MAX_TOOL_RESULT_CHARS]
    status: ResultStatus = "success" if succeeded else "command_failed"
    return ToolResult(
        combined, status,
        "fullstack_passed" if succeeded else "fullstack_failed",
        reports,
    )


def build_fullstack_check_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool(response_format="content_and_artifact")
    def run_fullstack_checks_tool(
        backend_path: str = "backend",
        frontend_path: str = "frontend",
        backend_task: Literal["test", "unittest"] = "test",
        frontend_task: Literal["test", "build", "lint", "typecheck"] = "build",
        backend_test_files: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Run backend Python tests and frontend npm/pnpm task together.

        Both sides are checked even when the first fails. Report two statuses
        and the original outputs and diagnostics. Python needs its local .venv;
        Node task must be declared in package.json. Run the Contract's exact
        test command separately for final acceptance when required.
        """
        result = run_fullstack_checks(
            workspace=root, backend_path=backend_path,
            frontend_path=frontend_path, backend_task=backend_task,
            frontend_task=frontend_task, backend_test_files=backend_test_files,
        )
        return dev._outcome(result)

    run_fullstack_checks_tool.name = "run_fullstack_checks"
    run_fullstack_checks_tool.metadata = {
        "risk": "execute", "mutation": False,
        "workspace_arguments": ("backend_path", "frontend_path"),
    }
    return [run_fullstack_checks_tool]
