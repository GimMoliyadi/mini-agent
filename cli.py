"""单任务 JSON 入口；运行时由 main 的共享 runner 负责。"""

import argparse
from contextlib import redirect_stdout
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from acceptance import CodingTaskContract


class CliInputError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliInputError(f"命令行参数错误：{message}")


def run_task(
    task: str | None,
    contract: "CodingTaskContract | None" = None,
    *,
    resume_id: str | None = None,
    record: dict | None = None,
    allow_legacy_workspace: bool = False,
) -> dict:
    with redirect_stdout(sys.stderr):
        import main

        options = {"contract": contract, "resume_id": resume_id, "record": record}
        if allow_legacy_workspace:
            options["allow_legacy_workspace"] = True
        return main.run_task_with_session(task, **options)


def exit_code_for_result(result: dict) -> int:
    status = result["status"]
    return 0 if status == "completed" else 130 if status == "cancelled" else 1


def configure_stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def redact_result(value):
    from file_safety import redact_text

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_result(item) for item in value]
    if isinstance(value, tuple):
        return [redact_result(item) for item in value]
    return value


def emit_result(result: dict) -> int:
    print(json.dumps(redact_result(result), ensure_ascii=False))
    return exit_code_for_result(result)


def error_result(message: str, *, cancelled: bool = False) -> dict:
    return {
        "status": "cancelled" if cancelled else "failed",
        "answer": None,
        "error": message,
        "trace": {},
    }


def parse_task_args(argv: list[str] | None) -> argparse.Namespace:
    parser = JsonArgumentParser(description="运行单个任务；stdout 只输出 JSON，日志写入 stderr")
    parser.add_argument("--task", help="任务内容")
    parser.add_argument("--contract", help="Coding Task Contract 的 JSON 文件")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复已保存的会话")
    parser.add_argument("--adopt-workspace", action="store_true", help="确认把缺少工作区绑定的旧会话绑定到当前目录")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--workspace", help="受信工作目录")
    location.add_argument("--desktop", action="store_true", help="使用系统桌面目录")
    args = parser.parse_args(argv)
    if not any((args.task, args.contract, args.resume)):
        parser.error("至少需要 --task、--contract 或 --resume")
    if args.task is not None and not args.task.strip():
        parser.error("--task 不能为空")
    if args.resume is not None and not args.resume.strip():
        parser.error("--resume 不能为空")
    return args


def expected_runtime_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, ValueError, UnicodeError, EOFError, ImportError)):
        return True
    from openai import APIError
    from session import SessionError
    import main

    return isinstance(exc, (APIError, SessionError, main.ApprovalUnavailableError))


def execute_task_args(args: argparse.Namespace) -> dict:
    from launcher import prepare_environment

    with redirect_stdout(sys.stderr):
        prepare_environment(workspace=args.workspace, desktop=args.desktop)
        from acceptance import load_contract

        contract = load_contract(args.contract) if args.contract else None
        options = {"contract": contract, "resume_id": args.resume}
        if args.adopt_workspace:
            options["allow_legacy_workspace"] = True
        return run_task(args.task, **options)


def cli(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    try:
        args = parse_task_args(argv)
    except CliInputError as exc:
        emit_result(error_result(str(exc)))
        return 2
    try:
        result = execute_task_args(args)
    except KeyboardInterrupt:
        result = error_result("已取消；已经完成的文件修改仍保留，可使用 undo 撤销。", cancelled=True)
    except SystemExit as exc:
        result = error_result(f"配置或启动失败：{exc}")
    except Exception as exc:
        if not expected_runtime_error(exc):
            raise
        result = error_result(f"任务执行失败（{type(exc).__name__}）：{exc}")
    return emit_result(result)


if __name__ == "__main__":
    raise SystemExit(cli())
