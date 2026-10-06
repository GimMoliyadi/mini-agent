"""Controlled command execution tools.

Commands run without a shell, inside the configured workspace, with bounded
environment, timeout, and output budgets.
"""

import subprocess
import sys
from pathlib import Path

from mini_agent import sandbox as file_safety
from mini_agent.config import COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_OUTPUT_CHARS, WORKSPACE_DIR
from mini_agent.result import ToolResult
from process_runner import MAX_COMMAND_OUTPUT_BYTES, run_process
from .filesystem import resolve_inside_workspace


def _active_workspace() -> Path:
    compat = sys.modules.get("tools")
    value = getattr(compat, "WORKSPACE_DIR", None) if compat is not None else None
    return Path(value) if value is not None else Path(WORKSPACE_DIR)


def _active_constant(name: str, default):
    compat = sys.modules.get("tools")
    return getattr(compat, name, default) if compat is not None else default


def _active_run_process():
    compat = sys.modules.get("tools")
    return getattr(compat, "run_process", run_process) if compat is not None else run_process


_COMMAND_SHELL_SYNTAX = frozenset("&|;><`\r\n")
_ALLOWED_PYTHON_MODULES = frozenset({"pytest", "unittest"})
_ALLOWED_GIT_COMMANDS = frozenset({"status", "diff", "log"})


class CommandPolicyError(ValueError):
    """A command is outside the deliberately small Phase 12 allowlist."""


def _reject_shell_syntax(tokens: list[str]) -> None:
    for token in tokens:
        if any(character in token for character in _COMMAND_SHELL_SYNTAX):
            raise CommandPolicyError(
                "命令参数不能包含 shell 操作符或换行；请使用 command + args 数组"
            )


def _reject_workspace_escape_tokens(
    tokens: list[str], workspace: str | Path | None = None
) -> None:
    for token in tokens:
        if token.startswith("-") and "=" not in token:
            continue
        candidate = token.split("=", 1)[1] if token.startswith("-") else token
        candidate = candidate.split("::", 1)[0]
        if not candidate:
            continue
        try:
            resolve_inside_workspace(candidate, workspace)
        except (PermissionError, ValueError) as exc:
            raise CommandPolicyError(f"命令参数包含禁止访问的路径：{token}") from exc


def validate_run_command_arguments(
    arguments: dict, workspace: str | Path | None = None
) -> None:
    """Validate the command policy without starting a process."""
    command = arguments.get("command")
    args = arguments.get("args", [])
    cwd = arguments.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd:
        raise CommandPolicyError("cwd 必须是非空字符串")
    resolve_inside_workspace(cwd, workspace)
    if not isinstance(command, str) or not command:
        raise CommandPolicyError("command 必须是非空字符串")
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise CommandPolicyError("args 必须是字符串数组")

    _reject_shell_syntax([command, *args])
    normalized_command = command.casefold()
    if normalized_command not in {"python", "git"} or "/" in command or "\\" in command:
        raise CommandPolicyError(f"不允许执行命令：{command}")

    if normalized_command == "python":
        if len(args) < 2 or args[0] != "-m" or args[1] not in _ALLOWED_PYTHON_MODULES:
            raise CommandPolicyError("python 只允许执行 python -m pytest 或 python -m unittest")
        if any(arg == "-c" or arg.startswith("-c") for arg in args):
            raise CommandPolicyError("禁止 python -c 任意执行代码")
        _reject_workspace_escape_tokens(args[2:], workspace)
        return

    if not args or args[0] not in _ALLOWED_GIT_COMMANDS:
        raise CommandPolicyError("git 只允许 status、diff、log 子命令")
    if any(
        arg in {"-c", "--exec-path", "--config", "--config-env", "--git-dir", "--work-tree", "--upload-pack", "--receive-pack", "--output", "-o", "--no-index", "--ext-diff", "--textconv", "-C"}
        or arg.startswith(("--output=", "--exec-path=", "--git-dir=", "--work-tree=", "--config=", "--config-env=", "--upload-pack=", "--receive-pack="))
        for arg in args[1:]
    ):
        raise CommandPolicyError("该 git 参数可能改变状态或访问未受控目标，已拒绝")
    _reject_workspace_escape_tokens(args[1:], workspace)


def _decode_process_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact_process_output(value: object) -> str:
    return file_safety.redact_text(_decode_process_output(value))


def _limit_command_output(value: object) -> str:
    text = _redact_process_output(value)
    if len(text) <= _active_constant("MAX_COMMAND_OUTPUT_CHARS", MAX_COMMAND_OUTPUT_CHARS):
        return text
    limit = _active_constant("MAX_COMMAND_OUTPUT_CHARS", MAX_COMMAND_OUTPUT_CHARS)
    head_chars = limit // 2
    tail_chars = limit - head_chars
    omitted = len(text) - limit
    return text[:head_chars] + f"\n[output truncated: omitted {omitted} chars]\n" + text[-tail_chars:]


def _format_command_result(
    command: str,
    cwd: Path,
    exit_code: int | None,
    timed_out: bool,
    stdout: object,
    stderr: object,
    *,
    output_limit_exceeded: bool = False,
    captured_bytes: int = 0,
) -> str:
    return file_safety.redact_text("\n".join([
        f"Command: {command}", f"CWD: {cwd}", f"Exit code: {exit_code}",
        f"Timed out: {'true' if timed_out else 'false'}",
        f"Output limit exceeded: {'true' if output_limit_exceeded else 'false'}",
        f"Captured bytes: {captured_bytes}", "STDOUT:",
        _limit_command_output(stdout) or "<empty>", "STDERR:",
        _limit_command_output(stderr) or "<empty>",
    ]))


def run_command(
    command: str,
    args: list[str] | None = None,
    cwd: str = ".",
    *,
    workspace: str | Path | None = None,
) -> ToolResult:
    args = [] if args is None else args
    validate_run_command_arguments({"command": command, "args": args, "cwd": cwd}, workspace)
    root = Path(workspace if workspace is not None else _active_workspace()).resolve()
    target = resolve_inside_workspace(cwd, root)
    if not target.is_dir():
        raise NotADirectoryError(f"cwd 不是一个目录：{cwd}")
    command_line = subprocess.list2cmdline([command, *args])
    executable = sys.executable if command.casefold() == "python" else command
    controlled_args = args
    if command.casefold() == "git":
        controlled_args = ["--no-pager", "-c", "core.fsmonitor=false", *args]
        if args[0] in {"diff", "log"}:
            controlled_args += ["--no-ext-diff", "--no-textconv"]
    output_limit = _active_constant("MAX_COMMAND_OUTPUT_BYTES", MAX_COMMAND_OUTPUT_BYTES)
    try:
        result = _active_run_process()(
            [executable, *controlled_args],
            target,
            workspace=root,
            timeout_seconds=COMMAND_TIMEOUT_SECONDS,
            output_limit_bytes=output_limit,
        )
    except OSError as exc:
        text = _format_command_result(command_line, target, None, False, "", f"[命令启动失败] {exc}")
        return ToolResult(
            text,
            "tool_error",
            "launch_failed",
            {"returncode": None, "timed_out": False, "output_limit_exceeded": False},
        )
    stderr = result.stderr
    if result.timed_out:
        stderr = b"[command timed out]\n" + stderr
    if result.output_limit_exceeded:
        stderr = b"[output byte limit exceeded]\n" + stderr
    formatted = _format_command_result(
        command_line,
        target,
        result.returncode,
        result.timed_out,
        result.stdout,
        stderr,
        output_limit_exceeded=result.output_limit_exceeded,
        captured_bytes=result.captured_bytes,
    )
    if result.timed_out:
        formatted += "\n[命令执行超时]"
    failed = result.timed_out or result.output_limit_exceeded or result.returncode != 0
    if result.timed_out:
        code = "timeout"
    elif result.output_limit_exceeded:
        code = "output_limit"
    elif result.returncode != 0:
        code = "exit_nonzero"
    else:
        code = "command_succeeded"
    return ToolResult(
        formatted,
        "command_failed" if failed else "success",
        code,
        {
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "output_limit_exceeded": result.output_limit_exceeded,
            "captured_bytes": result.captured_bytes,
        },
    )
