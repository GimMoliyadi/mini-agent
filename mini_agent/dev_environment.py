"""Project-aware Python and JavaScript development tools.

Operate on an explicitly selected trusted project inside the workspace.
Python installs only inside that project's .venv; JS uses npm/pnpm manifests.
All commands use the application's bounded no-shell process runner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Literal

from langchain.tools import tool

from .process_runner import MAX_COMMAND_OUTPUT_BYTES, run_process
from .result import ResultStatus, ToolResult
from .sandbox import redact_text, resolve_path

_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_DISPLAY_CHARS = 6500
_RUN_TIMEOUT = 120.0
_INSTALL_TIMEOUT = 240.0
_MAX_TARGET_TEST_FILES = 16


def _project(workspace: Path, path: str) -> Path:
    root = resolve_path(path, workspace)
    if not root.is_dir():
        raise NotADirectoryError("项目路径必须是工作区内已经存在的目录")
    return root


def _manifest(project: Path) -> dict[str, Any] | None:
    file = project / "package.json"
    if not file.is_file():
        return None
    if file.is_symlink() or file.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("package.json 不得是符号链接或超大文件")
    value = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("package.json 必须是 JSON 对象")
    scripts = value.get("scripts", {})
    if not isinstance(scripts, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in scripts.items()
    ):
        raise ValueError("package.json scripts 必须是字符串映射")
    return value


def _detect(project: Path) -> dict[str, Any]:
    package = _manifest(project)
    python_files = [name for name in ("pyproject.toml", "requirements.txt", "setup.py") if (project / name).is_file()]
    node_locks = [name for name in ("pnpm-lock.yaml", "package-lock.json", "npm-shrinkwrap.json") if (project / name).is_file()]
    manager: str | None = None
    if package is not None:
        manager = "pnpm" if "pnpm-lock.yaml" in node_locks else "npm"
    scripts = (package or {}).get("scripts", {})
    return {
        "python": bool(python_files),
        "python_manifests": python_files,
        "python_env_ready": _python_exe(project).is_file(),
        "node": package is not None,
        "node_manager": manager,
        "node_lockfiles": node_locks,
        "node_dependencies_installed": (project / "node_modules").is_dir(),
        "node_scripts": sorted(scripts),
    }


def _python_exe(project: Path) -> Path:
    return project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _manager(project: Path, language: str) -> str:
    found = _detect(project)
    if language == "python":
        if not found["python"]:
            raise ValueError("该目录未找到 Python 项目清单")
        return language
    if language == "node":
        if not found["node"]:
            raise ValueError("该目录未找到 package.json")
        return str(found["node_manager"])
    if language != "auto":
        raise ValueError("language 仅支持 auto、python 或 node")
    if found["python"] and found["node"]:
        raise ValueError("混合语言仓库请明确指定 language=python 或 node")
    if found["python"]:
        return "python"
    if found["node"]:
        return str(found["node_manager"])
    raise ValueError("该目录没有可识别的 Python/Node 项目清单")


def _execute(argv: list[str], project: Path, workspace: Path, *, timeout: float) -> ToolResult:
    try:
        completed = run_process(
            argv, project, workspace=workspace, timeout_seconds=timeout,
            output_limit_bytes=MAX_COMMAND_OUTPUT_BYTES,
        )
    except OSError as exc:
        return ToolResult(
            redact_text(f"开发命令无法启动：{type(exc).__name__}: {exc}"),
            "tool_error", "launch_failed",
        )
    stdout = redact_text(completed.stdout.decode("utf-8", errors="replace"))
    stderr = redact_text(completed.stderr.decode("utf-8", errors="replace"))
    output = f"exit_code={completed.returncode}; timed_out={completed.timed_out}; output_limit={completed.output_limit_exceeded}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    if len(output) > _MAX_DISPLAY_CHARS:
        output = output[:_MAX_DISPLAY_CHARS // 2] + "\n[output truncated]\n" + output[-_MAX_DISPLAY_CHARS // 2:]
    status: ResultStatus
    if completed.timed_out:
        status, code = "command_failed", "timeout"
    elif completed.output_limit_exceeded:
        status, code = "command_failed", "output_limit"
    elif completed.returncode != 0:
        status, code = "command_failed", "exit_nonzero"
    else:
        status, code = "success", "command_succeeded"
    return ToolResult(
        output, status, code,
        {"returncode": completed.returncode, "timed_out": completed.timed_out,
         "output_limit_exceeded": completed.output_limit_exceeded},
    )


def _prepare_python(project: Path, workspace: Path) -> ToolResult:
    venv = project / ".venv"
    if venv.is_symlink() or venv.resolve() != venv.absolute():
        raise PermissionError("Python 虚拟环境目录不得通过符号链接跳转")
    interpreter = _python_exe(project)
    if not interpreter.is_file():
        created = _execute([sys.executable, "-m", "venv", str(venv)], project, workspace, timeout=_INSTALL_TIMEOUT)
        if created.status != "success":
            return created
    # POSIX venv interpreters are normally symlinks to their system Python;
    # the project .venv directory itself was checked against redirection.
    if not interpreter.is_file():
        raise RuntimeError("虚拟环境创建失败或解释器不可用")
    if (project / "requirements.txt").is_file():
        argv = [str(interpreter), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", "requirements.txt"]
    elif (project / "pyproject.toml").is_file() or (project / "setup.py").is_file():
        argv = [str(interpreter), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-e", "."]
    else:
        return ToolResult("已创建独立 Python .venv；无需安装依赖。")
    return _execute(argv, project, workspace, timeout=_INSTALL_TIMEOUT)


def _prepare_node(project: Path, workspace: Path, manager: str) -> ToolResult:
    if manager == "pnpm":
        argv = ["pnpm", "install", "--frozen-lockfile"] if (project / "pnpm-lock.yaml").is_file() else ["pnpm", "install"]
    elif manager == "npm":
        if (project / "package-lock.json").is_file() or (project / "npm-shrinkwrap.json").is_file():
            argv = ["npm", "ci", "--no-audit", "--no-fund"]
        else:
            argv = ["npm", "install", "--no-audit", "--no-fund"]
    else:
        raise ValueError("暂不支持该 JavaScript 包管理器")
    return _execute(argv, project, workspace, timeout=_INSTALL_TIMEOUT)


def _test_file_targets(project: Path, test_files: list[str]) -> list[str]:
    """Validate explicit local Python test paths before building any command."""
    if not isinstance(test_files, list) or len(test_files) > _MAX_TARGET_TEST_FILES:
        raise ValueError(f"测试文件最多指定 {_MAX_TARGET_TEST_FILES} 个")
    selected: list[str] = []
    seen: set[str] = set()
    for raw in test_files:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 260:
            raise ValueError("测试文件路径必须为长度不超过 260 的非空字符串")
        # resolve_path rejects traversal, sensitive files and out-of-project links.
        resolved = resolve_path(raw, project)
        lexical = project / raw.replace("\\", "/")
        if lexical.is_symlink():
            raise PermissionError("测试文件不得是符号链接")
        if not resolved.is_file() or resolved.suffix.lower() != ".py":
            raise ValueError("测试目标必须为项目内已有的 Python .py 文件")
        name = resolved.relative_to(project).as_posix()
        if name in seen:
            raise ValueError("不能重复指定同一个测试文件")
        seen.add(name)
        selected.append(name)
    return selected


def _outcome(result: ToolResult) -> tuple[str, dict[str, Any]]:
    return result.text, {"status": result.status, "code": result.code,
                         "metadata": dict(result.metadata)}


def build_dev_environment_tools(workspace: Path) -> list[Any]:
    """Development tools bound to the trusted workspace, not the Agent Python env."""
    root = workspace.resolve()

    @tool
    def inspect_dev_environment(path: str = ".") -> str:
        """Detect nested Python/Node projects, lockfiles, installed env and npm/pnpm scripts.

        Use before preparing dependencies or running project tests/builds.
        No commands are executed.
        """
        directory = _project(root, path)
        return json.dumps(_detect(directory), ensure_ascii=False)

    @tool(response_format="content_and_artifact")
    def prepare_dev_environment(
        path: str = ".",
        language: Literal["auto", "python", "node"] = "auto",
    ) -> tuple[str, dict[str, Any]]:
        """Create project-local Python .venv and install declared dependencies, or npm/pnpm install.

        Runs package installers only for trusted projects with human approval
        in ASK mode. Python never installs into the Agent's interpreter.
        """
        directory = _project(root, path)
        manager = _manager(directory, language)
        result = _prepare_python(directory, root) if manager == "python" else _prepare_node(directory, root, manager)
        return _outcome(result)

    @tool(response_format="content_and_artifact")
    def run_dev_task(
        task: Literal["test", "lint", "typecheck", "build", "script", "unittest"] = "test",
        path: str = ".",
        language: Literal["auto", "python", "node"] = "auto",
        script: str = "",
        test_files: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Run project tests, including a bounded list of Python test files, or build/lint.

        For Python script mode, script is an existing relative .py entrypoint.
        For Node, task maps to package.json scripts; custom script names must
        already appear in package.json. For Python test/unittest only,
        test_files selects up to 16 existing project-relative .py files.
        Omitting test_files keeps the existing whole-suite behavior.
        No arbitrary shell strings accepted.
        """
        directory = _project(root, path)
        manager = _manager(directory, language)
        if test_files and (manager != "python" or task not in {"test", "unittest"}):
            raise ValueError("test_files 仅可用于 Python 的 test 或 unittest 任务")
        targets = _test_file_targets(directory, test_files or [])
        if manager == "python":
            interpreter = _python_exe(directory)
            if not interpreter.is_file():
                raise ValueError("项目虚拟环境不存在，请先使用 prepare_dev_environment")
            modules = {
                "test": ["-m", "pytest", "-q"],
                "unittest": ["-m", "unittest", "discover"],
                "lint": ["-m", "ruff", "check", "."],
                "typecheck": ["-m", "mypy", "."],
                "build": ["-m", "build"],
            }
            if task == "script":
                if not script.endswith(".py") or not script.strip():
                    raise ValueError("Python script 必须是工作区项目中的 .py 文件")
                entry = resolve_path(script, directory)
                if not entry.is_file() or entry.is_symlink():
                    raise ValueError("Python 脚本路径必须对应项目内的普通 .py 文件")
                argv = [str(interpreter), str(entry)]
            else:
                args = list(modules[task])
                if targets:
                    args = (
                        ["-m", "pytest", "-q", "--", *targets]
                        if task == "test"
                        else ["-m", "unittest", "--", *targets]
                    )
                argv = [str(interpreter), *args]
        else:
            package = _manifest(directory)
            scripts = (package or {}).get("scripts", {})
            selected = script if task == "script" else task
            if not selected or selected not in scripts:
                raise ValueError("package.json 中没有该脚本；请先查看 inspect_dev_environment 的 node_scripts")
            argv = [manager, "run", selected]
        return _outcome(_execute(argv, directory, root, timeout=_RUN_TIMEOUT))

    inspect_dev_environment.metadata = {"risk": "read", "mutation": False, "workspace_arguments": ("path",)}
    prepare_dev_environment.metadata = {"risk": "execute", "mutation": False, "workspace_arguments": ("path",), "dependency_install": True}
    run_dev_task.metadata = {"risk": "execute", "mutation": False, "workspace_arguments": ("path",)}
    return [inspect_dev_environment, prepare_dev_environment, run_dev_task]
