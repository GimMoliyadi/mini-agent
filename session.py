"""Minimal persistence for one canonical Agent conversation.

This module deliberately knows nothing about the model loop or tools.  It only
validates and stores the stable message history needed to continue one session.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from acceptance import CodingTaskContract, TaskState


PROJECT_ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = PROJECT_ROOT / "sessions"
SESSION_VERSION = 2
_SUPPORTED_SESSION_VERSIONS = {1, SESSION_VERSION}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_FORBIDDEN_TOP_LEVEL_KEYS = {
    "api_key",
    "openai_api_key",
    "authorization",
    "auth_header",
    "headers",
    "env",
    "secret",
    "token",
}


class SessionError(Exception):
    """A user-facing error while creating, saving, or restoring a session."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}"


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise SessionError("Session ID 非法，只允许字母、数字、短横线和下划线")


def _validate_messages(messages: object) -> None:
    if not isinstance(messages, list):
        raise SessionError("Session 的 messages 必须是数组")

    pending: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise SessionError(f"Session 第 {index} 条 message 不是对象")
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            if pending:
                raise SessionError("Session 中存在未完成的 assistant tool_calls")
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    raise SessionError("Session 中的 tool call 不是对象")
                call_id = call.get("id")
                function = call.get("function")
                arguments = function.get("arguments") if isinstance(function, dict) else None
                if not isinstance(call_id, str) or not call_id:
                    raise SessionError("Session 中的 tool call 缺少 id")
                if call_id in pending:
                    raise SessionError(f"Session 中重复的 tool call id：{call_id}")
                if not isinstance(arguments, str):
                    raise SessionError(f"Session 中 tool call {call_id} 的 arguments 不是字符串")
                try:
                    json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise SessionError(f"Session 中 tool call {call_id} 的 arguments 不是合法 JSON") from exc
                pending.add(call_id)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise SessionError("Session 中存在无法配对的 tool result")
            pending.remove(call_id)
        elif pending:
            raise SessionError("Session 中 assistant tool_calls 与 tool result 不相邻")
        elif role not in {"system", "user", "assistant"}:
            raise SessionError(f"Session 中存在未知 role：{role!r}")

    if pending:
        raise SessionError("Session 中存在没有 tool result 的 tool call")


def _validate_record(record: object) -> dict:
    if not isinstance(record, dict):
        raise SessionError("Session JSON 顶层必须是对象")

    forbidden = _FORBIDDEN_TOP_LEVEL_KEYS.intersection(record)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise SessionError(f"Session 不允许保存认证或密钥字段：{names}")

    required = {"session_id", "created_at", "updated_at", "model", "messages", "version"}
    missing = required.difference(record)
    if missing:
        raise SessionError(f"Session 缺少字段：{', '.join(sorted(missing))}")
    if record["version"] not in _SUPPORTED_SESSION_VERSIONS:
        raise SessionError(f"不支持的 Session version：{record['version']!r}")
    _validate_session_id(record["session_id"])
    if not isinstance(record["model"], str) or not record["model"]:
        raise SessionError("Session 的 model 必须是非空字符串")
    _validate_messages(record["messages"])
    if "context_mode" in record and not isinstance(record["context_mode"], str):
        raise SessionError("Session 的 context_mode 必须是字符串")
    if record["version"] == 1 and (
        "task_state" in record or "coding_contract" in record
    ):
        raise SessionError("旧版 Session 不能包含 Coding Task 状态")
    if "coding_contract" in record and "task_state" not in record:
        raise SessionError("Coding Session 缺少 task_state，不能恢复 Coding Task")
    if "task_state" in record and "coding_contract" not in record:
        raise SessionError("Coding Session 缺少 coding_contract，不能恢复 Coding Task")
    if "task_state" in record:
        try:
            record["task_state"] = TaskState.from_dict(record["task_state"]).as_dict()
        except ValueError as exc:
            raise SessionError(f"Session 的 task_state 无效：{exc}") from exc
    if "coding_contract" in record:
        try:
            record["coding_contract"] = CodingTaskContract.from_dict(
                record["coding_contract"]
            ).as_dict()
        except ValueError as exc:
            raise SessionError(f"Session 的 coding_contract 无效：{exc}") from exc
    return record


def create_session(
    model: str,
    messages: list[dict],
    context_mode: str | None = None,
    *,
    task_state: TaskState | dict | None = None,
    coding_contract: CodingTaskContract | dict | None = None,
) -> dict:
    """Create an unsaved session record containing canonical messages only."""
    now = _now()
    record = {
        "session_id": _new_session_id(),
        "created_at": now,
        "updated_at": now,
        "model": model,
        "messages": messages,
        "version": SESSION_VERSION,
    }
    if context_mode is not None:
        record["context_mode"] = context_mode
    if task_state is not None:
        record["task_state"] = (
            task_state.as_dict()
            if isinstance(task_state, TaskState)
            else task_state
        )
    if coding_contract is not None:
        record["coding_contract"] = (
            coding_contract.as_dict()
            if isinstance(coding_contract, CodingTaskContract)
            else coding_contract
        )
    return _validate_record(record)


def load_task_state(record: dict) -> TaskState:
    """Return persisted Coding Task state or fail clearly for a legacy session."""
    if "task_state" not in record:
        raise SessionError("Coding Session 缺少 task_state，不能恢复 Coding Task")
    try:
        return TaskState.from_dict(record["task_state"])
    except ValueError as exc:
        raise SessionError(f"Session 的 task_state 无效：{exc}") from exc


def _path_for(session_id: str) -> Path:
    _validate_session_id(session_id)
    return SESSIONS_DIR / f"{session_id}.json"


def save_session(record: dict) -> Path:
    """Atomically save one stable canonical session and return its path."""
    _validate_record(record)
    record["updated_at"] = _now()
    path = _path_for(record["session_id"])
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=SESSIONS_DIR,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_path).replace(path)
    except OSError as exc:
        raise SessionError(f"保存 Session 失败：{exc}") from exc
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)
    return path


def load_session(session_id: str) -> dict:
    """Load and validate one session without changing its stored history."""
    path = _path_for(session_id)
    try:
        with path.open("r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError as exc:
        raise SessionError(f"Session 不存在：{session_id}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise SessionError(f"读取 Session 失败：{session_id}") from exc
    except json.JSONDecodeError as exc:
        raise SessionError(f"Session 文件损坏：{session_id}") from exc
    return _validate_record(record)


def list_sessions() -> list[str]:
    """Return recognizable session IDs currently stored on disk."""
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted(
        path.stem
        for path in SESSIONS_DIR.glob("*.json")
        if _SESSION_ID_RE.fullmatch(path.stem)
    )
