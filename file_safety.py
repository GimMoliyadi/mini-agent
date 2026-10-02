"""受信工作目录的路径、原子文件变更、审批快照与撤销记录。"""

import base64
import contextvars
import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_CHARS = 8_000
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_ORIGINAL_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_PATHS = 100
_SENSITIVE_DIRECTORIES = frozenset({"sessions", "changes", ".changes", ".ssh", ".gnupg", ".git", ".serena", ".mini-agent", ".mini_agent"})
_PRIVATE_KEY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".ppk"})
_DEVICE_NAME = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.IGNORECASE)
_MUTATION_PATHS = {"write_file": ("path",), "apply_patch": ("path",), "rename_file": ("source", "destination")}
_ACTIVE_JOURNAL = contextvars.ContextVar("file_change_journal", default=None)


def _configured_sensitive_exceptions() -> frozenset[str]:
    configured = os.environ.get("MINI_AGENT_ALLOW_SENSITIVE_PATHS", "")
    if not configured:
        return frozenset()
    values = json.loads(configured) if configured.lstrip().startswith("[") else configured.split(";")
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("MINI_AGENT_ALLOW_SENSITIVE_PATHS 必须是 JSON 字符串数组或分号分隔的精确相对路径")
    allowed = set()
    for value in values:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if not normalized or any(part in {"", ".", ".."} for part in parts) or ":" in normalized or "*" in normalized or "?" in normalized:
            raise ValueError("敏感路径例外必须是工作目录内的精确相对路径，不能包含通配符")
        allowed.add(normalized.casefold())
    return frozenset(allowed)


# 仅在用户启动进程时取配置；工具参数或子进程不能改变本轮授权。
_SENSITIVE_PATH_EXCEPTIONS = _configured_sensitive_exceptions()


def _validate_path_syntax(path: str) -> None:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("路径必须是非空字符串")
    windows = PureWindowsPath(path)
    if path.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\"):
        raise PermissionError("拒绝 Windows 设备路径或 UNC 路径")
    relative_text = path[len(windows.drive):] if windows.drive else path
    if ":" in relative_text or (windows.drive and not windows.root):
        raise PermissionError("拒绝 Windows ADS 或盘符相对路径")
    for component in relative_text.replace("\\", "/").split("/"):
        if component in {"", ".", ".."}:
            continue
        if _DEVICE_NAME.match(component) or component.endswith((" ", ".")):
            raise PermissionError("拒绝 Windows 保留设备名或歧义路径")


def is_sensitive_path(relative_path: Path | str) -> bool:
    normalized = str(relative_path).replace("\\", "/").casefold()
    if normalized in _SENSITIVE_PATH_EXCEPTIONS:
        return False
    parts = normalized.split("/")
    name = parts[-1]
    if any(part in _SENSITIVE_DIRECTORIES for part in parts):
        return True
    if name in {".env.example", ".env.sample"}:
        return False
    return (
        name == ".env" or name.startswith(".env.")
        or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        or Path(name).suffix in _PRIVATE_KEY_SUFFIXES
    )


def resolve_path(path: str, workspace: str | Path) -> Path:
    _validate_path_syntax(path)
    root = Path(workspace).resolve()
    lexical = Path(os.path.abspath(root / path))
    target = lexical.resolve()
    if not lexical.is_relative_to(root) or not target.is_relative_to(root):
        raise PermissionError(f"路径越出工作目录，拒绝访问：{path}")
    if is_sensitive_path(lexical.relative_to(root)) or is_sensitive_path(target.relative_to(root)):
        raise PermissionError("敏感路径默认禁止访问；请由用户在启动进程前配置精确路径例外")
    return target


def read_bounded_bytes(target: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    if not target.is_file():
        if not target.exists():
            raise FileNotFoundError(str(target))
        raise ValueError("只允许访问普通文件")
    if target.stat().st_size > limit:
        raise ValueError(f"文件超过安全大小上限（{limit} 字节）")
    with target.open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"文件超过安全大小上限（{limit} 字节）")
    return content


def encode_content(content: str, name: str = "content") -> bytes:
    if not isinstance(content, str):
        raise ValueError(f"{name} 必须是字符串")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError(f"文件超过安全大小上限（{MAX_FILE_BYTES} 字节）")
    return encoded


def patched_content(content: bytes, old_text: str, new_text: str) -> bytes:
    if not isinstance(old_text, str) or not old_text:
        raise ValueError("old_text 不能为空")
    encode_content(old_text, "old_text")
    encode_content(new_text, "new_text")
    raw = content.decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\r" if "\r" in raw else "\n"
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    old = old_text.replace("\r\n", "\n").replace("\r", "\n")
    new = new_text.replace("\r\n", "\n").replace("\r", "\n")
    occurrences = normalized.count(old)
    if not occurrences:
        raise ValueError("目标文本不存在，文件没有修改")
    if occurrences != 1:
        raise ValueError(f"目标文本不唯一，请提供更多上下文（匹配 {occurrences} 次）")
    return encode_content(normalized.replace(old, new, 1).replace("\n", newline))


def atomic_write_bytes(target: Path, content: bytes, *, mode: int | None = None) -> None:
    if not isinstance(content, bytes):
        raise ValueError("原子写入内容必须是 bytes")
    if target.exists() and not target.is_file():
        raise ValueError("只允许覆盖普通文件")
    if mode is None and target.is_file():
        mode = stat.S_IMODE(target.stat().st_mode)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".mini-agent-", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def rename_no_replace(source: Path, destination: Path) -> None:
    os.link(source, destination)
    try:
        source.unlink()
    except BaseException:
        destination.unlink()
        raise


MIN_LITERAL_SECRET_LENGTH = 8


def redact_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("待脱敏内容必须是字符串")
    credential_suffixes = ("API_KEY", "ACCESS_KEY", "ACCESS_KEY_ID", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")
    credentials = {
        value for key, value in os.environ.items()
        if len(value) >= MIN_LITERAL_SECRET_LENGTH
        and (key.upper().endswith(credential_suffixes) or key.upper() in {"TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"})
    }
    for value in sorted(credentials, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", "[PRIVATE KEY REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)((?:[\w-]*(?:api[ _-]?key|token|secret|password|credential)|authorization)\s*[\"']?\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)


def _change_contents(tool_name: str, arguments: dict, workspace: Path) -> dict[Path, bytes | None]:
    if tool_name == "write_file":
        return {resolve_path(arguments["path"], workspace): encode_content(arguments["content"])}
    if tool_name == "apply_patch":
        target = resolve_path(arguments["path"], workspace)
        return {target: patched_content(read_bounded_bytes(target), arguments["old_text"], arguments["new_text"])}
    source = resolve_path(arguments["source"], workspace)
    destination = resolve_path(arguments["destination"], workspace)
    if source == destination:
        raise ValueError("目标文件名与源文件名相同")
    if destination.exists():
        raise FileExistsError("目标已存在，拒绝覆盖")
    if not destination.parent.is_dir():
        raise FileNotFoundError("目标目录不存在")
    return {source: None, destination: read_bounded_bytes(source)}


def preview_tool_change(tool_name: str, arguments: dict, workspace: str | Path) -> str:
    if tool_name not in _MUTATION_PATHS:
        return "该工具不提供文件差异预览（不会为预览执行工具）。"
    root = Path(workspace).resolve()
    changes = _change_contents(tool_name, arguments, root)
    blocks = []
    for target, after in changes.items():
        before = read_bounded_bytes(target) if target.exists() else b""
        relative = target.relative_to(root).as_posix()
        difference = difflib.unified_diff(before.decode("utf-8").splitlines(keepends=True), (after or b"").decode("utf-8").splitlines(keepends=True), fromfile=f"a/{relative}", tofile=f"b/{relative}")
        blocks.append("".join(difference) or f"{relative}：内容不变")
    preview = redact_text("\n".join(blocks))
    if len(preview) > MAX_PREVIEW_CHARS:
        preview = preview[:MAX_PREVIEW_CHARS] + "\n[差异预览已截断]"
    return preview


def _content_hash(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _path_snapshot(path: str, root: Path) -> dict:
    target = resolve_path(path, root)
    if not target.exists():
        return {"resolved": str(target), "exists": False}
    content = read_bounded_bytes(target)
    metadata = target.stat()
    return {"resolved": str(target), "exists": True, "hash": _content_hash(content), "identity": [metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_mode]}


def capture_precondition(tool_name: str, arguments: dict, workspace: str | Path) -> dict:
    names = _MUTATION_PATHS.get(tool_name)
    if not names:
        return {}
    root = Path(workspace).resolve()
    return {
        "tool": tool_name,
        "workspace": str(root),
        "arguments_hash": hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest(),
        "paths": {name: _path_snapshot(arguments[name], root) for name in names},
    }


def validate_precondition(tool_name: str, arguments: dict, workspace: str | Path, before: dict) -> None:
    if tool_name not in _MUTATION_PATHS:
        return
    if not before or before != capture_precondition(tool_name, arguments, workspace):
        raise PermissionError("审批后文件或路径已变化，拒绝使用旧审批；请重新预览并审批")


def _journal_path(run_id: str, workspace: str | Path, store_dir: str | Path) -> tuple[Path, Path]:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("run_id 必须是安全的字母、数字、下划线或连字符")
    root = Path(workspace).resolve()
    store = Path(store_dir).resolve()
    if store.is_relative_to(root):
        raise PermissionError("变更日志必须存储在工作目录之外")
    return root, store / f"{run_id}.json"


def _save_journal_data(path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True).encode("utf-8")
    if len(payload) > MAX_JOURNAL_BYTES:
        raise ValueError("变更日志超过安全大小上限")
    atomic_write_bytes(path, payload)


class _Journal:
    def __init__(self, run_id: str, workspace: str | Path, store_dir: str | Path):
        self.root, self.path = _journal_path(run_id, workspace, store_dir)
        if self.path.exists():
            raise FileExistsError("本轮变更日志已存在，不能覆盖")
        self.data = {"version": 1, "run_id": run_id, "workspace": str(self.root), "status": "running", "entries": {}}
        self.had_failure = False
        self.save()

    def save(self) -> None:
        _save_journal_data(self.path, self.data)

    def prepare(self, changes: dict[Path, bytes | None]) -> dict:
        previous = json.loads(json.dumps(self.data["entries"]))
        entries = self.data["entries"]
        for target, after in changes.items():
            name = target.relative_to(self.root).as_posix()
            if name not in entries:
                before = read_bounded_bytes(target) if target.exists() else None
                entries[name] = {"before": base64.b64encode(before).decode("ascii") if before is not None else None, "before_hash": _content_hash(before), "mode": stat.S_IMODE(target.stat().st_mode) if before is not None else None}
            entries[name].update(after_hash=_content_hash(after), status="pending")
        original_bytes = sum(len(entry["before"] or "") for entry in entries.values()) * 3 // 4
        if len(entries) > MAX_JOURNAL_PATHS or original_bytes > MAX_JOURNAL_ORIGINAL_BYTES:
            self.data["entries"] = previous
            raise ValueError("本轮变更记录超过安全上限")
        try:
            self.save()
        except BaseException:
            self.data["entries"] = previous
            raise
        return previous

    def complete(self, changes: dict[Path, bytes | None]) -> None:
        for target in changes:
            self.data["entries"][target.relative_to(self.root).as_posix()]["status"] = "complete"
        self.save()


@contextmanager
def journal_context(run_id: str, workspace: str | Path, store_dir: str | Path):
    if _ACTIVE_JOURNAL.get() is not None:
        raise RuntimeError("不能嵌套变更日志上下文")
    journal = _Journal(run_id, workspace, store_dir)
    token = _ACTIVE_JOURNAL.set(journal)
    try:
        yield journal
    except BaseException:
        journal.had_failure = True
        raise
    finally:
        _ACTIVE_JOURNAL.reset(token)
        journal.data["status"] = "partial" if journal.had_failure else "complete"
        journal.save()


@contextmanager
def file_change(changes: dict[Path, bytes | None], workspace: str | Path):
    journal = _ACTIVE_JOURNAL.get()
    if journal is None:
        yield
        return
    if journal.root != Path(workspace).resolve():
        raise PermissionError("变更日志与当前工作目录不一致")
    previous = journal.prepare(changes)
    try:
        yield
    except BaseException:
        journal.had_failure = True
        journal.data["entries"] = previous
        journal.save()
        raise
    else:
        try:
            journal.complete(changes)
        except BaseException:
            journal.had_failure = True
            raise


def _validate_journal_entry(entry: dict) -> None:
    required = {"before", "before_hash", "after_hash", "mode", "status"}
    if not isinstance(entry, dict) or not required.issubset(entry):
        raise ValueError("变更日志条目格式无效")
    if entry["status"] not in ("pending", "complete", "restoring", "restored"):
        raise ValueError("变更日志条目状态无效")
    encoded = entry["before"]
    if encoded is not None and not isinstance(encoded, str):
        raise ValueError("变更日志原内容格式无效")
    before = base64.b64decode(encoded, validate=True) if encoded is not None else None
    if _content_hash(before) != entry["before_hash"] or (before is not None and len(before) > MAX_FILE_BYTES):
        raise ValueError("变更日志原内容校验失败")
    after_hash = entry["after_hash"]
    if after_hash is not None and (not isinstance(after_hash, str) or len(after_hash) != hashlib.sha256().digest_size * 2 or not re.fullmatch(r"[0-9a-f]+", after_hash)):
        raise ValueError("变更日志目标校验值无效")
    mode = entry["mode"]
    if before is None:
        if mode is not None:
            raise ValueError("变更日志文件权限无效")
    elif isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or stat.S_IMODE(mode) != mode:
        raise ValueError("变更日志文件权限无效")


def _load_journal(run_id: str, workspace: str | Path, store_dir: str | Path) -> tuple[Path, Path, dict]:
    root, path = _journal_path(run_id, workspace, store_dir)
    data = json.loads(read_bounded_bytes(path, MAX_JOURNAL_BYTES))
    if not isinstance(data, dict):
        raise ValueError("变更日志格式无效")
    if data.get("version") != 1 or data.get("run_id") != run_id or data.get("workspace") != str(root):
        raise PermissionError("变更日志不属于当前工作目录或任务")
    entries = data.get("entries")
    if not isinstance(entries, dict) or len(entries) > MAX_JOURNAL_PATHS:
        raise ValueError("变更日志格式无效")
    for name, entry in entries.items():
        target = resolve_path(name, root)
        if target.relative_to(root).as_posix() != name:
            raise ValueError("变更日志路径无效")
        _validate_journal_entry(entry)
    return root, path, data


def _undo_conflicts(root: Path, entries: dict) -> list[str]:
    conflicts = []
    for name, entry in entries.items():
        if entry.get("status") == "restored":
            continue
        try:
            target = resolve_path(name, root)
            current = _content_hash(read_bounded_bytes(target)) if target.exists() else None
        except (OSError, ValueError):
            conflicts.append(name)
            continue
        recoverable = entry.get("status") in {"pending", "restoring"} and current == entry["before_hash"]
        if current != entry["after_hash"] and not recoverable:
            conflicts.append(name)
    return conflicts


def _restore_journal_entry(root: Path, path: Path, data: dict, name: str, restored: list[str]) -> bool:
    entry = data["entries"][name]
    if _undo_conflicts(root, {name: entry}):
        return False
    entry["status"] = "restoring"
    data["status"] = "undo_partial"
    # 先记恢复意图，防止文件已恢复而进度日志写入失败后无法续撤销。
    _save_journal_data(path, data)
    if _undo_conflicts(root, {name: entry}):
        return False
    target = resolve_path(name, root)
    current = _content_hash(read_bounded_bytes(target)) if target.exists() else None
    if current != entry["before_hash"]:
        before = base64.b64decode(entry["before"]) if entry["before"] is not None else None
        if before is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write_bytes(target, before, mode=entry["mode"])
    entry["status"] = "restored"
    restored.append(name)
    _save_journal_data(path, data)
    return True


def undo_task(run_id: str, workspace: str | Path, store_dir: str | Path) -> dict:
    root, path, data = _load_journal(run_id, workspace, store_dir)
    entries = data["entries"]
    conflicts = _undo_conflicts(root, entries)
    result = {"status": "conflict", "run_id": run_id, "restored": [], "conflicts": conflicts}
    if conflicts:
        return {**result, "message": "文件已被同期编辑，拒绝覆盖；没有撤销任何文件"}
    restored = result["restored"]
    for name, entry in entries.items():
        if entry["status"] == "restored":
            continue
        try:
            if not _restore_journal_entry(root, path, data, name, restored):
                return {**result, "status": "partial", "conflicts": [name], "message": "撤销期间文件变化，已停止；可检查后续撤销"}
        except (OSError, ValueError) as exc:
            return {**result, "status": "partial", "message": redact_text(f"撤销未全部完成：{type(exc).__name__}：{exc}；已恢复列表保留，可重试续撤销")}
    data["status"] = "undone"
    try:
        _save_journal_data(path, data)
    except (OSError, ValueError) as exc:
        return {**result, "status": "partial", "message": redact_text(f"文件已恢复，但最终日志保存失败：{exc}；可重试续撤销")}
    return {**result, "status": "undone", "message": "已撤销本轮记录的文件变更"}
