"""本地会话管理；没有模型请求或自动数据上传。"""

import argparse
import json
from pathlib import Path

from file_safety import atomic_write_bytes, redact_text
from . import session

MAX_LABEL_CHARS = 80


def metadata(record: dict) -> dict:
    return {
        "session_id": record["session_id"],
        "name": record.get("display_name"),
        "workspace": record.get("workspace"),
        "model": record["model"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "status": record.get("run_status", "unknown"),
        "last_run_id": record.get("last_run_id"),
        "last_budget": record.get("last_budget"),
        "message_count": len(record["messages"]),
        "version": record["version"],
    }


def redacted_record(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redacted_record(item) for item in value]
    if isinstance(value, dict):
        return {key: redacted_record(item) for key, item in value.items()}
    return value


def export_session(session_id: str, destination: str | Path, *, overwrite: bool = False) -> dict:
    record = session.load_session(session_id)
    target = Path(destination).expanduser()
    if target.is_symlink():
        raise session.SessionError("导出目标不能是符号链接。")
    target = target.resolve()
    protected = (session.SESSIONS_DIR.resolve(), (session.SESSIONS_DIR.parent / "journals").resolve())
    if any(target.is_relative_to(root) for root in protected) or target == (session.SESSIONS_DIR.parent / ".env").resolve():
        raise session.SessionError("不能把导出结果覆盖到活动会话、撤销记录或配置文件。")
    if target.exists() and not overwrite:
        raise session.SessionError("导出目标已存在；确认覆盖后使用 --yes。")
    exported = redacted_record(record)
    exported["export_redacted"] = True
    encoded = (json.dumps(exported, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > session.MAX_SESSION_BYTES:
        raise session.SessionError("脱敏导出超过会话文件大小上限。")
    if not target.parent.is_dir():
        raise session.SessionError("导出目标的父目录不存在。")
    atomic_write_bytes(target, encoded)
    return {"status": "exported", "path": str(target), "redacted": True,
            "notice": "已去除可识别的凭据；源代码和用户文本仍可能敏感，分享前请自行检查。"}


def delete_session(session_id: str, *, confirmed: bool = False) -> dict:
    if not confirmed:
        raise session.SessionError("删除会话需要显式 --yes；工作区文件和撤销备份不会被删除。")
    with session.task_lock(session_id):
        session.load_session(session_id)
        with session._storage_lock(session_id, "write"):
            session._path_for(session_id).unlink()
    return {"status": "deleted", "session_id": session_id,
            "notice": "只删除了会话记录；工作区文件和 journals 撤销备份仍保留。"}


def list_metadata() -> list[dict]:
    records = []
    for session_id in session.list_sessions():
        try:
            records.append(metadata(session.load_session(session_id)))
        except session.SessionError as error:
            records.append({"session_id": session_id, "status": "corrupt", "error": redact_text(str(error))})
    return records


def _parser():
    parser = argparse.ArgumentParser(description="管理本地会话，不调用模型")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出会话元数据")
    show = commands.add_parser("show", help="查看单个会话的元数据")
    show.add_argument("session_id")
    export = commands.add_parser("export", help="导出脱敏会话")
    export.add_argument("session_id")
    export.add_argument("destination")
    export.add_argument("--yes", action="store_true", help="确认覆盖已有导出文件")
    delete = commands.add_parser("delete", help="删除会话记录，保留工作区及撤销备份")
    delete.add_argument("session_id")
    delete.add_argument("--yes", action="store_true")
    rename = commands.add_parser("rename", help="修改会话显示名称，不修改会话 ID")
    rename.add_argument("session_id")
    rename.add_argument("name")
    return parser


def _dispatch(args):
    if args.command == "list":
        return {"sessions": list_metadata()}
    if args.command == "show":
        return metadata(session.load_session(args.session_id))
    if args.command == "export":
        return export_session(args.session_id, args.destination, overwrite=args.yes)
    if args.command == "delete":
        return delete_session(args.session_id, confirmed=args.yes)
    if not args.name.strip() or len(args.name) > MAX_LABEL_CHARS:
        raise session.SessionError(f"会话名称需要 1 到 {MAX_LABEL_CHARS} 个字符。")
    with session.task_lock(args.session_id):
        record = session.load_session(args.session_id)
        record["display_name"] = args.name.strip()
        session.save_session(record)
    return metadata(record)


def main(argv: list[str] | None = None) -> int:
    from cli import configure_stdout_utf8

    configure_stdout_utf8()
    args = _parser().parse_args(argv)
    try:
        result = _dispatch(args)
        print(json.dumps(redacted_record(result), ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "error": "已取消。"}, ensure_ascii=False))
        return 130
    except (OSError, ValueError, session.SessionError) as error:
        print(json.dumps({"status": "failed", "error": redact_text(str(error))}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
