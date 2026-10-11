"""Private v2 checkpoint store, separate from all legacy session JSON."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver


def validate_thread_id(thread_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", thread_id):
        raise ValueError("会话 ID 只能包含字母、数字、下划线和连字符，最多 100 字符")
    return thread_id


@contextmanager
def checkpoint_store(state_dir: Path):
    directory = state_dir / ".mini-agent" / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise PermissionError("Checkpoint 目录不能是符号链接")
    path = directory / "v2.sqlite3"
    if path.is_symlink():
        raise PermissionError("Checkpoint 文件不能是符号链接")
    connection = sqlite3.connect(path, check_same_thread=False)
    try:
        os.chmod(path, 0o600)
        connection.execute("PRAGMA journal_mode=WAL")
        saver = SqliteSaver(connection)
        saver.setup()
        yield saver
    finally:
        connection.close()
