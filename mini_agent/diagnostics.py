"""离线诊断：只检查本机配置和能力，不调用模型或自动安装依赖。"""

from contextlib import redirect_stdout
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys

REQUIRED_SETTINGS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")


def installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def path_permissions(path: Path) -> dict:
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return {
        "exists": path.exists(),
        "directory": path.is_dir(),
        "readable": path.is_dir() and os.access(path, os.R_OK),
        "writable": path.is_dir() and os.access(path, os.W_OK),
        "can_create": os.access(parent, os.W_OK),
    }


def inspect_local_configuration() -> tuple[dict, list[str]]:
    issues = []
    with redirect_stdout(sys.stderr):
        import config

        try:
            config.load_env_file()
        except (OSError, UnicodeError, ValueError, SystemExit) as exc:
            issues.append(f"配置文件无法读取：{exc}")
        modes = {}
        for name, getter in (("approval", config.get_approval_mode), ("context", config.get_context_mode)):
            try:
                modes[name] = getter()
            except (ValueError, SystemExit) as exc:
                issues.append(f"{name} 配置无效：{exc}")
        settings = {
            name: bool(os.environ.get(name)) and os.environ.get(name) != "sk-your-key-here"
            for name in REQUIRED_SETTINGS
        }
        limits = {
            "max_agent_steps": config.MAX_AGENT_STEPS,
            "request_timeout_seconds": config.REQUEST_TIMEOUT_SECONDS,
            "command_timeout_seconds": config.COMMAND_TIMEOUT_SECONDS,
            "max_tool_result_chars": config.MAX_TOOL_RESULT_CHARS,
        }
    return {"settings_present": settings, "modes": modes, "limits": limits}, issues


def inspect_tools() -> tuple[list[str], list[str]]:
    try:
        with redirect_stdout(sys.stderr):
            import tools

            return sorted(tools.TOOL_REGISTRY), []
    except ImportError as exc:
        return [], [f"工具模块不可用：{exc}"]


def build_report() -> dict:
    from launcher import package_version, prepare_environment

    state, workspace = prepare_environment(create=False)
    configuration, issues = inspect_local_configuration()
    tools, tool_issues = inspect_tools()
    sdk_version = installed_version("openai")
    python_supported = sys.version_info >= (3, 11)
    if not python_supported:
        issues.append("需要 Python 3.11 或更新版本。")
    if sdk_version is None:
        issues.append("OpenAI 兼容 SDK 未安装；doctor 不会自动安装。")
    issues.extend(tool_issues)
    return {
        "version": package_version(),
        "python": {"version": platform.python_version(), "executable": sys.executable, "supported": python_supported},
        "sdk": {"name": "openai", "version": sdk_version, "installed": sdk_version is not None},
        "paths": {"workspace": str(workspace), "state": str(state), "config": str(state / ".env"), "sessions": str(state / "sessions")},
        "configuration": configuration,
        "tools": {"available": bool(tools), "names": tools},
        "permissions": {"workspace": path_permissions(workspace), "state": path_permissions(state)},
        "support_boundary": "单用户本地 CLI，仅支持受信项目；路径、环境和资源限制不构成操作系统沙盒。不自动联网，不发送模型请求。",
        "issues": issues,
    }


def doctor() -> int:
    from cli import configure_stdout_utf8, redact_result

    configure_stdout_utf8()
    report = build_report()
    print(json.dumps(redact_result(report), ensure_ascii=False))
    return 1 if report["issues"] else 0
