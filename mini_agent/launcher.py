"""统一启动入口；在导入运行时之前确定用户工作目录和状态目录。"""

import argparse
import ctypes
from importlib import metadata
import os
from pathlib import Path
import sys
import uuid
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_FOLDER_ID = "b4bfcc3a-db2c-424c-b029-7fe99a87c641"


def get_desktop_path() -> Path:
    if sys.platform != "win32":
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            raise FileNotFoundError("桌面目录不存在，请使用 --workspace 指定目录。")
        return desktop
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(DESKTOP_FOLDER_ID).bytes_le)
    folder_path = ctypes.c_wchar_p()
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    result = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(folder_path))
    try:
        if result < 0 or not folder_path.value:
            raise OSError("无法读取系统桌面位置，请使用 --workspace 指定目录。")
        return Path(folder_path.value)
    finally:
        if folder_path:
            ole.CoTaskMemFree(ctypes.cast(folder_path, ctypes.c_void_p))


def prepare_environment(
    *, workspace: str | Path | None = None, desktop: bool = False, create: bool = True
) -> tuple[Path, Path]:
    if desktop and workspace is not None:
        raise ValueError("--desktop 和 --workspace 不能同时使用。")
    source_demo = PROJECT_ROOT / "demo_workspace"
    source_checkout = source_demo.is_dir()
    default_state = PROJECT_ROOT if source_checkout else Path.home() / ".mini-agent"
    state = Path(os.environ.get("MINI_AGENT_STATE_DIR", default_state)).expanduser().resolve()
    default_workspace = source_demo if source_checkout else state / "workspace"
    requested = get_desktop_path() if desktop else workspace
    selected: str | Path = requested if requested is not None else os.environ.get("AGENT_WORKSPACE", default_workspace)
    work = Path(selected).expanduser().resolve()
    if create:
        state.mkdir(parents=True, exist_ok=True)
        if work == default_workspace.resolve():
            work.mkdir(parents=True, exist_ok=True)
    if state.exists() and not state.is_dir():
        raise NotADirectoryError(f"状态目录不是文件夹：{state}")
    if not work.is_dir() and (create or work.exists()):
        raise NotADirectoryError(f"工作目录不存在或不是文件夹：{work}")
    os.environ["AGENT_WORKSPACE"] = str(work)
    os.environ["MINI_AGENT_STATE_DIR"] = str(state)
    return state, work


def package_version() -> str:
    try:
        return metadata.version("mini-agent-lab")
    except metadata.PackageNotFoundError:
        return "0+source"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mini Agent 单用户本地命令行；仅支持受信项目。")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="开始交互式对话")
    location = start.add_mutually_exclusive_group()
    location.add_argument("--desktop", action="store_true", help="使用系统桌面目录")
    location.add_argument("--workspace", help="受信工作目录")
    start.add_argument("--resume", metavar="SESSION_ID", help="恢复会话")
    start.add_argument("--contract", help="Coding Task Contract 的 JSON 文件")
    start.add_argument("--adopt-workspace", action="store_true", help="明确绑定旧会话的原工作区")
    commands.add_parser("task", help="单任务 JSON 入口；mini task --help 查看参数")
    commands.add_parser("awareness", help="终端持续觉察对话；上下文仅在内存，不提供工具或保存会话")
    commands.add_parser("config", help="配置 API 地址、模型和 Key")
    doctor = commands.add_parser("doctor", help="离线诊断，不发送模型请求")
    doctor.add_argument("--workspace", help="检查指定受信工作目录")
    commands.add_parser("sessions", help="管理会话；mini sessions --help 查看参数")
    undo = commands.add_parser("undo", help="撤销指定任务的文件修改")
    undo.add_argument("task_id", help="任务或运行 ID")
    undo.add_argument("--workspace", help="任务使用的受信工作目录")
    undo.add_argument("--yes", action="store_true", required=True, help="确认执行撤销")
    commands.add_parser("version", help="显示版本")
    return parser


def start_agent(args: argparse.Namespace, workspace: Path) -> int:
    import main

    main.configure_workspace(workspace)
    if args.contract:
        from acceptance import load_contract
        from cli import exit_code_for_result, redact_result

        options: dict[str, Any] = {"contract": load_contract(args.contract), "resume_id": args.resume}
        if args.adopt_workspace:
            options["allow_legacy_workspace"] = True
        result = main.run_task_with_session(None, **options)
        result = redact_result(result)
        if result.get("answer"):
            print(result["answer"])
        if result.get("error"):
            print(result["error"], file=sys.stderr)
        return exit_code_for_result(result)
    arguments = ["--resume", args.resume] if args.resume else []
    if args.adopt_workspace:
        arguments.append("--adopt-workspace")
    return main.main(arguments)


def dispatch_command(args: argparse.Namespace) -> int:
    if args.command == "version":
        print(package_version())
        return 0
    state, work = prepare_environment(
        workspace=getattr(args, "workspace", None),
        desktop=getattr(args, "desktop", False),
        create=args.command != "doctor",
    )
    if args.command == "start":
        return start_agent(args, work)
    if args.command == "config":
        from configure import configure

        return configure(state / ".env")
    if args.command == "doctor":
        from diagnostics import doctor

        return doctor()
    if args.command == "undo":
        from file_safety import undo_task
        from cli import emit_result

        result = undo_task(args.task_id, work, state / "journals")
        emit_result(result)
        return 0 if result["status"] == "undone" else 1
    raise ValueError(f"未知命令：{args.command}")


def prepare_state_directory() -> Path:
    default = PROJECT_ROOT if (PROJECT_ROOT / "demo_workspace").is_dir() else Path.home() / ".mini-agent"
    state = Path(os.environ.get("MINI_AGENT_STATE_DIR", default)).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ["MINI_AGENT_STATE_DIR"] = str(state)
    return state


def run_session_command(arguments: list[str]) -> int:
    from cli import emit_result, error_result

    try:
        prepare_state_directory()
        import session_cli
        return session_cli.main(arguments)
    except KeyboardInterrupt:
        emit_result(error_result("会话操作已取消。", cancelled=True))
        return 130
    except (OSError, ValueError, ImportError) as exc:
        emit_result(error_result(f"会话管理无法启动：{exc}"))
        return 1


def main(argv: list[str] | None = None) -> int:
    from cli import configure_stdout_utf8

    configure_stdout_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "task":
        from cli import cli

        return cli(argv[1:])
    if argv and argv[0] == "sessions":
        return run_session_command(argv[1:])
    if argv and argv[0] == "awareness":
        from awareness_cli import main as awareness_main

        return awareness_main(argv[1:])
    if argv == ["help"] or not argv:
        argv = ["--help"]
    args = build_parser().parse_args(argv)
    try:
        return dispatch_command(args)
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except (OSError, ValueError, UnicodeError, EOFError, ImportError) as exc:
        from file_safety import redact_text

        print(redact_text(f"启动失败（{type(exc).__name__}）：{exc}"), file=sys.stderr)
        return 130 if isinstance(exc, EOFError) else 1
    except SystemExit as exc:
        from file_safety import redact_text

        print(redact_text(f"配置或启动失败：{exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
