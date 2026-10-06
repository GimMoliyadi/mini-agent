"""Minimal persistence for one canonical Agent conversation.

This module deliberately knows nothing about the model loop or tools.  It only
validates and stores the stable message history needed to continue one session.
"""

from contextlib import contextmanager
from importlib import import_module
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from acceptance import CodingTaskContract, TaskState


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = Path(os.environ.get("MINI_AGENT_STATE_DIR", PROJECT_ROOT)) / "sessions"
SESSION_VERSION = 3
_SUPPORTED_SESSION_VERSIONS = {1, 2, SESSION_VERSION}
MAX_SESSION_BYTES = 16 * 1024 * 1024
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
        if role not in {"system", "user", "assistant", "tool"}:
            raise SessionError(f"Session 中存在未知 role：{role!r}")
        calls = message.get("tool_calls")
        content = message.get("content")
        if not isinstance(content, str) and not (role == "assistant" and calls and content is None):
            raise SessionError(f"Session 第 {index} 条 message 的 content 类型不受支持")
        if role == "assistant" and calls:
            if pending or not isinstance(calls, list):
                raise SessionError("Session 中存在未完成或非法的 assistant tool_calls")
            for call in calls:
                if not isinstance(call, dict) or call.get("type", "function") != "function":
                    raise SessionError("Session 中的 tool call 格式不受支持")
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not call_id or call_id in pending:
                    raise SessionError("Session 中的 tool call id 缺失或重复")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
                    raise SessionError("Session 中的 tool call 缺少 function 名称")
                if not isinstance(function.get("arguments"), str):
                    raise SessionError(f"Session 中 tool call {call_id} 的 arguments 不是字符串")
                pending.add(call_id)
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise SessionError("Session 中存在无法配对的 tool result")
            pending.remove(call_id)
        elif pending:
            raise SessionError("Session 中 assistant tool_calls 与 tool result 不相邻")
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
    if type(record["version"]) is not int or record["version"] not in _SUPPORTED_SESSION_VERSIONS:
        raise SessionError(f"不支持的 Session version：{record['version']!r}")
    _validate_session_id(record["session_id"])
    if not isinstance(record["model"], str) or not record["model"]:
        raise SessionError("Session 的 model 必须是非空字符串")
    _validate_messages(record["messages"])
    workspace = record.get("workspace")
    if workspace is not None and (not isinstance(workspace, str) or not Path(workspace).is_absolute()):
        raise SessionError("Session 的 workspace 必须是绝对路径")
    if record["version"] == SESSION_VERSION and workspace is None:
        raise SessionError("新版 Session 缺少 workspace")
    revision = record.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise SessionError("Session revision 必须是非负整数")
    if "runs" in record and not isinstance(record["runs"], dict):
        raise SessionError("Session runs 必须是对象")
    if "run_ids" in record and (not isinstance(record["run_ids"], list) or any(not isinstance(item, str) for item in record["run_ids"])):
        raise SessionError("Session run_ids 必须是字符串数组")
    if "last_budget" in record and not isinstance(record["last_budget"], dict):
        raise SessionError("Session last_budget 必须是对象")
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
    workspace: str | Path | None = None,
) -> dict:
    """Create an unsaved session record containing canonical messages only."""
    import config

    now = _now()
    root = Path(workspace if workspace is not None else config.WORKSPACE_DIR).expanduser().resolve()
    record = {
        "session_id": _new_session_id(),
        "created_at": now,
        "updated_at": now,
        "model": model,
        "messages": messages,
        "version": SESSION_VERSION,
        "workspace": str(root),
        "revision": 0,
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
    root = SESSIONS_DIR.resolve()
    path = root / f"{session_id}.json"
    if path.is_symlink() or path.resolve().parent != root:
        raise SessionError("Session 文件不能指向存储目录以外。")
    if path.exists() and path.stat().st_nlink > 1:
        raise SessionError("Session 文件不能是共享硬链接。")
    return path


def bind_session_workspace(record: dict, workspace: str | Path, *, allow_legacy: bool = False) -> dict:
    _validate_record(record)
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise SessionError(f"恢复工作区不存在：{root}")
    saved = record.get("workspace")
    if saved is None:
        if not allow_legacy:
            raise SessionError("旧会话没有工作区绑定；确认原目录后使用 --adopt-workspace 显式绑定。")
    elif Path(saved).resolve() != root:
        raise SessionError(f"Session 工作区不匹配：原目录 {saved}，当前目录 {root}")
    record["workspace"] = str(root)
    record["version"] = SESSION_VERSION
    record.setdefault("revision", 0)
    return record


def _read_payload(path: Path) -> dict:
    if path.stat().st_size > MAX_SESSION_BYTES:
        raise SessionError(f"Session 超过大小上限 {MAX_SESSION_BYTES} 字节")
    with path.open("r", encoding="utf-8") as handle:
        return _validate_record(json.load(handle))


@contextmanager
def _storage_lock(session_id: str, kind: str):
    _validate_session_id(session_id)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f".{session_id}.{kind}.lock"
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise SessionError("会话锁文件不能是链接")
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(bytes([0]))
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SessionError("此会话正在被另一个进程使用，请稍后重试。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def task_lock(session_id: str):
    with _storage_lock(session_id, "run"):
        yield


def save_session(record: dict) -> Path:
    _validate_record(record)
    path = _path_for(record["session_id"])
    temporary_path = None
    with _storage_lock(record["session_id"], "write"):
        revision = record.get("revision", 0)
        if path.exists():
            try:
                current = _read_payload(path)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                raise SessionError("现有 Session 无法读取，拒绝覆盖。") from exc
            if current.get("revision", 0) != revision:
                raise SessionError("Session 已被其他进程更新，请重新加载后继续。")
        updated = {**record, "updated_at": _now(), "revision": revision + 1}
        try:
            encoded = (json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_SESSION_BYTES:
                raise SessionError(f"Session 超过大小上限 {MAX_SESSION_BYTES} 字节，未覆盖旧记录。")
            with tempfile.NamedTemporaryFile(mode="wb", dir=SESSIONS_DIR, prefix=f".{path.stem}.", suffix=".tmp", delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(path)
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
            raise SessionError(f"保存 Session 失败：{exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    record["storage_warning"] = f"临时文件未能清理：{temporary_path.name}"
        record.update(updated)
    return path


def load_session(
    session_id: str,
    *,
    workspace: str | Path | None = None,
    allow_legacy_workspace: bool = False,
) -> dict:
    path = _path_for(session_id)
    try:
        record = _read_payload(path)
    except FileNotFoundError as exc:
        raise SessionError(f"Session 不存在：{session_id}") from exc
    except (OSError, UnicodeError) as exc:
        raise SessionError(f"读取 Session 失败：{session_id}") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SessionError(f"Session 文件损坏：{session_id}") from exc
    if record["session_id"] != session_id:
        raise SessionError("Session ID 与文件名不一致，拒绝恢复。")
    if workspace is not None:
        bind_session_workspace(record, workspace, allow_legacy=allow_legacy_workspace)
    return record


def list_sessions() -> list[str]:
    """Return recognizable session IDs currently stored on disk."""
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted(
        path.stem
        for path in SESSIONS_DIR.glob("*.json")
        if _SESSION_ID_RE.fullmatch(path.stem)
    )
