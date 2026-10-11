"""JSON tasks, interactive chat, configuration, checkpoint inspection and undo."""

import argparse
import json
from pathlib import Path
import sys

from .launcher import prepare_environment
from .sandbox import redact_text


def redact_result(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_result(item) for item in value]
    return value


def exit_code_for_result(result: dict) -> int:
    return {"completed": 0, "undone": 0, "cancelled": 130, "interrupted": 3}.get(result["status"], 1)


def emit_result(result: dict) -> int:
    print(json.dumps(redact_result(result), ensure_ascii=False))
    return exit_code_for_result(result)


def build_parser():
    parser = argparse.ArgumentParser(description="LangChain + LangGraph 本地任务助手")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "task"):
        command = commands.add_parser(name)
        location = command.add_mutually_exclusive_group()
        location.add_argument("--workspace")
        location.add_argument("--desktop", action="store_true")
        command.add_argument("--resume", metavar="SESSION_ID")
        command.add_argument("--contract", help="JSON Coding Task Contract")
        if name == "task":
            command.add_argument("--task")
            decision = command.add_mutually_exclusive_group()
            decision.add_argument("--approve", action="store_true", help="明确批准恢复时所有当前待审批操作")
            decision.add_argument("--reject", action="store_true")
    commands.add_parser("config")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--workspace")
    sessions = commands.add_parser("sessions")
    sessions.add_argument("session_id", nargs="?")
    sessions.add_argument("--legacy", action="store_true", help="只列出原 JSON 文件，不转换或执行")
    undo = commands.add_parser("undo")
    undo.add_argument("task_id")
    undo.add_argument("--workspace")
    undo.add_argument("--yes", required=True, action="store_true")
    commands.add_parser("version")
    return parser


def show_sessions(state_dir: Path, session_id: str | None, legacy: bool) -> int:
    if legacy:
        files = sorted(path.name for path in (state_dir / "sessions").glob("*.json"))
        print(json.dumps({"format": "legacy-read-only", "files": files}, ensure_ascii=False))
        return 0
    from .persistence import checkpoint_store, validate_thread_id
    config = {"configurable": {"thread_id": validate_thread_id(session_id)}} if session_id else None
    seen = set()
    entries = []
    with checkpoint_store(state_dir) as saver:
        for checkpoint in saver.list(config):
            identity = checkpoint.config["configurable"]["thread_id"]
            if identity in seen:
                continue
            seen.add(identity)
            values = checkpoint.checkpoint["channel_values"]
            entries.append({"session_id": identity, "workspace": values.get("workspace"),
                            "status": values.get("status"), "messages": len(values.get("messages", []))})
    print(json.dumps(entries, ensure_ascii=False))
    return 0


def interactive_decisions(result: dict) -> list[dict]:
    decisions = []
    for interrupt in result["interrupts"]:
        for action in interrupt.get("action_requests", []):
            print(redact_text(action.get("description", action["name"])))
            approved = input(f"批准 {action['name']}? [y/N] ").strip().casefold() in {"y", "yes"}
            decisions.append({"type": "approve" if approved else "reject"})
    return decisions


def run_agent(args, state_dir, workspace):
    from .runtime import AgentApplication
    from .settings import Settings
    from .verifier import load_contract

    settings = Settings.load(workspace, state_dir)
    contract = load_contract(args.contract) if args.contract else None
    def display_event(event):
        if event["event"] == "tool":
            print(f"工具 · {event['name']} · {event['status']}", file=sys.stderr)
        elif event["event"] == "verification":
            print(f"验收 · {'通过' if event['accepted'] else '未通过'}", file=sys.stderr)
    with AgentApplication(settings, event_callback=display_event) as app:
        thread_id = args.resume
        if args.command == "task":
            task = args.task or (contract.instruction if contract and not thread_id else None)
            if task is None and thread_id is None:
                raise ValueError("task 需要 --task、--contract 或 --resume")
            if (args.approve or args.reject) and not thread_id:
                raise ValueError("审批只能用于 --resume 的待审批操作")
            result = app.run(task, thread_id=thread_id, contract=contract)
            if result["interrupts"] and (args.approve or args.reject):
                decisions = [{"type": "approve" if args.approve else "reject"}
                             for item in result["interrupts"] for _ in item.get("action_requests", [])]
                result = app.run(None, thread_id=result["session_id"], decisions=decisions)
            return emit_result(result)
        print(f"Mini Agent · LangGraph · 工作目录 {workspace}\n退出：exit")
        if thread_id:
            result = app.run(None, thread_id=thread_id)
        elif contract:
            result = app.run(contract.instruction, contract=contract)
        else:
            result = None
        while True:
            if result:
                thread_id = result["session_id"]
                while result["interrupts"]:
                    result = app.run(None, thread_id=thread_id, decisions=interactive_decisions(result))
                print(f"会话 {thread_id} · {result['status']}")
                print(result.get("answer") or result.get("error") or "尚未完成")
            task = input("You > ").strip()
            if task.casefold() in {"exit", "quit", "q"}:
                return 0
            if task:
                result = app.run(task, thread_id=thread_id)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        state, workspace = prepare_environment(
            workspace=getattr(args, "workspace", None), desktop=getattr(args, "desktop", False),
            create=args.command != "doctor",
        )
        if args.command in {"start", "task"}:
            return run_agent(args, state, workspace)
        if args.command == "config":
            from .configure import configure
            return configure(state / ".env")
        if args.command == "sessions":
            return show_sessions(state, args.session_id, args.legacy)
        if args.command == "undo":
            from .sandbox import undo_task
            return emit_result(undo_task(args.task_id, workspace, state / ".mini-agent" / "journals"))
        if args.command == "doctor":
            from .settings import Settings
            settings = Settings.load(workspace, state, require_model=False)
            print(json.dumps({"workspace": str(workspace), "state_dir": str(state),
                              "model_configured": bool(settings.api_key and settings.model and settings.base_url),
                              "approval_mode": settings.approval_mode, "runtime": "LangChain create_agent / LangGraph"}))
            return 0
        if args.command == "version":
            from . import __version__
            print(__version__)
            return 0
    except (KeyboardInterrupt, EOFError):
        return 130
    except (OSError, ValueError, ImportError) as exc:
        result = {"status": "failed", "error": redact_text(str(exc))}
        if args.command == "task":
            return emit_result(result)
        print(result["error"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
