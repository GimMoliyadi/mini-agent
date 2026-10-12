"""Private v2 checkpoint store, separate from all legacy session JSON."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat

from langgraph.checkpoint.sqlite import SqliteSaver


def _is_link(path: Path) -> bool:
    """Recognize symlinks and Windows reparse points before creating state."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def validate_thread_id(thread_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", thread_id):
        raise ValueError("会话 ID 只能包含字母、数字、下划线和连字符，最多 100 字符")
    return thread_id


@contextmanager
def checkpoint_store(state_dir: Path):
    private_root = Path(state_dir).expanduser().resolve() / ".mini-agent"
    if _is_link(private_root):
        raise PermissionError("Checkpoint 私有目录不能是符号链接或重解析点")
    private_root.mkdir(parents=True, exist_ok=True)
    directory = private_root / "checkpoints"
    if _is_link(directory):
        raise PermissionError("Checkpoint 目录不能是符号链接或重解析点")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "v2.sqlite3"
    if _is_link(path):
        raise PermissionError("Checkpoint 文件不能是符号链接或重解析点")
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        os.chmod(path, 0o600)
        connection.execute("PRAGMA journal_mode=WAL")
        saver = SqliteSaver(connection)
        saver.setup()
        yield saver
    finally:
        connection.close()
