"""Create reviewable Alembic revision scripts from structured model changes.

This authoring helper produces valid Alembic + SQLAlchemy op directives with
reversible operations for supported changes. It never runs upgrade/downgrade
against a database. Alembic itself owns revision execution and history.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Literal

from langchain.tools import tool

from . import dev_environment as dev
from . import sandbox
from .result import ToolResult

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]{0,79}$")
_REVISION = re.compile(r"^[a-z0-9]{6,32}$")
_TYPES = frozenset({
    "Integer", "BigInteger", "SmallInteger", "String", "Text",
    "Boolean", "DateTime", "Date", "Float", "Numeric", "JSON",
    "LargeBinary",
})
_MAX_OPERATIONS = 12
_MAX_COLUMNS = 24


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} 必须是简单的 SQL 标识符")
    return value


def _column(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict) or set(entry) - {
        "name", "type", "nullable", "primary_key", "length",
    }:
        raise ValueError("Column 只允许 name、type、nullable、primary_key、length")
    name = _identifier(entry.get("name"), "column.name")
    kind = entry.get("type")
    if not isinstance(kind, str) or kind not in _TYPES:
        raise ValueError(f"不支持 SQLAlchemy Column 类型：{kind}")
    length = entry.get("length")
    if length is not None:
        if kind != "String" or isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 65535:
            raise ValueError("length 只允许 String 类型使用 1–65535")
    nullable = entry.get("nullable", True)
    primary = entry.get("primary_key", False)
    if not isinstance(nullable, bool) or not isinstance(primary, bool):
        raise ValueError("nullable/primary_key 必须是布尔值")
    col_type = f"sa.{kind}({length})" if length is not None else f"sa.{kind}()"
    kwargs = f", nullable={nullable}"
    if primary:
        kwargs += ", primary_key=True"
    return f"sa.Column({name!r}, {col_type}{kwargs})"


def _statements(changes: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    if not isinstance(changes, list) or not 1 <= len(changes) <= _MAX_OPERATIONS:
        raise ValueError(f"数据库变更操作必须为 1–{_MAX_OPERATIONS} 条")
    up: list[str] = []
    downs: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("迁移操作必须是 JSON 对象")
        action = change.get("action")
        if action == "create_table":
            if set(change) != {"action", "table", "columns"}:
                raise ValueError("create_table 只接受 table 和 columns")
            table = _identifier(change["table"], "table")
            columns = change["columns"]
            if not isinstance(columns, list) or not 1 <= len(columns) <= _MAX_COLUMNS:
                raise ValueError("每张表须定义 1–24 个字段")
            if len({_identifier(c.get("name"), "column.name") for c in columns if isinstance(c, dict)}) != len(columns):
                raise ValueError("字段名重复或字段定义不合法")
            generated = ",\n        ".join(_column(c) for c in columns)
            up.append(f"op.create_table({table!r},\n        {generated},\n    )")
            downs.insert(0, f"op.drop_table({table!r})")
        elif action == "add_column":
            if set(change) != {"action", "table", "column"}:
                raise ValueError("add_column 必须提供 table 和 column")
            table = _identifier(change["table"], "table")
            column = change["column"]
            generated = _column(column)
            up.append(f"op.add_column({table!r}, {generated})")
            downs.insert(0, f"op.drop_column({table!r}, {column['name']!r})")
        elif action == "create_index":
            if set(change) - {"action", "name", "table", "columns", "unique"}:
                raise ValueError("create_index 字段不合法")
            index = _identifier(change.get("name"), "index")
            table = _identifier(change.get("table"), "table")
            cols = change.get("columns")
            unique = change.get("unique", False)
            if not isinstance(unique, bool) or not isinstance(cols, list) or not 1 <= len(cols) <= 8:
                raise ValueError("create_index columns 为非空列表，unique 为布尔值")
            columns = [_identifier(col, "index.column") for col in cols]
            up.append(f"op.create_index({index!r}, {table!r}, {columns!r}, unique={unique})")
            downs.insert(0, f"op.drop_index({index!r}, table_name={table!r})")
        else:
            raise ValueError("当前迁移生成器仅支持 create_table、add_column、create_index")
    return up, downs


def generate_alembic_migration(
    *, workspace: Path, backend_path: str,
    output_path: str, revision: str,
    down_revision: str | None,
    message: str, changes: list[dict[str, Any]],
) -> ToolResult:
    """Produce an Alembic revision with explicit upgrade/downgrade operations."""
    root = workspace.resolve()
    backend = dev._project(root, backend_path)
    dev._manager(backend, "python")
    if not (backend / "alembic.ini").is_file():
        raise FileNotFoundError("后端缺少 alembic.ini；请先初始化 Alembic 项目")
    target = sandbox.resolve_path(output_path, root)
    if not target.is_relative_to(backend) or target.suffix != ".py":
        raise ValueError("迁移文件必须位于后端项目的 .py 文件路径")
    if target.parent not in {backend / "alembic" / "versions", backend / "migrations" / "versions"}:
        raise ValueError("迁移文件只能存入项目 alembic/versions 或 migrations/versions")
    if not target.parent.is_dir():
        raise FileNotFoundError("迁移目录不存在，请先初始化 Alembic")
    if target.exists() or target.is_symlink():
        raise FileExistsError("迁移文件已存在；请选择新 revision 文件名")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ValueError("revision 必须由 6–32 位小写字母或数字组成")
    if down_revision is not None and (
        not isinstance(down_revision, str) or not _REVISION.fullmatch(down_revision)
        or down_revision == revision
    ):
        raise ValueError("down_revision 应为不同的已存在 Alembic revision 或 null")
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 120 or "\n" in message:
        raise ValueError("message 必须是 1–120 字的单行说明")
    for existing in target.parent.glob("*.py"):
        try:
            content = sandbox.read_bounded_bytes(existing, 128 * 1024).decode("utf-8-sig")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if re.search(rf"(?m)^revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]{re.escape(revision)}['\"]", content):
            raise ValueError("Alembic revision 已存在，请使用不同 ID")
    upgrade, downgrade = _statements(changes)
    text = (
        f'"""{message.strip()}\n\nGenerated by mini-agent using Alembic directives.\n"""\n\n'
        "from alembic import op\nimport sqlalchemy as sa\n\n"
        f"revision = {revision!r}\n"
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade() -> None:\n"
        + "".join(f"    {statement}\n" for statement in upgrade)
        + "\n\ndef downgrade() -> None:\n"
        + "".join(f"    {statement}\n" for statement in downgrade)
    )
    payload = sandbox.encode_content(text)
    with sandbox.file_change({target: payload}, root):
        if target.exists():
            raise FileExistsError("Alembic 迁移文件已被创建，请重新选择目标")
        sandbox.atomic_write_bytes(target, payload)
    return ToolResult(
        sandbox.redact_text(json.dumps({
            "file": target.relative_to(root).as_posix(),
            "revision": revision, "down_revision": down_revision,
            "operations": len(changes),
            "applied_to_database": False,
            "next_step": "Review generated migration; run Alembic checks in a prepared backend before applying manually.",
        }, ensure_ascii=False)),
        "success", "alembic_revision_created",
        {"output": target.relative_to(root).as_posix(), "revision": revision},
    )


def inspect_alembic_status(
    *, workspace: Path, backend_path: str = "backend",
    action: Literal["heads", "history", "current", "check"] = "heads",
) -> ToolResult:
    root = workspace.resolve()
    backend = dev._project(root, backend_path)
    dev._manager(backend, "python")
    if not (backend / "alembic.ini").is_file():
        raise FileNotFoundError("缺少 alembic.ini，请先初始化 Alembic")
    python = dev._python_exe(backend)
    if not python.is_file():
        raise ValueError("后端 Python .venv 未准备，请先使用 prepare_dev_environment")
    if action not in {"heads", "history", "current", "check"}:
        raise ValueError("Alembic 状态操作仅支持 heads、history、current、check")
    return dev._execute(
        [str(python), "-m", "alembic", "-c", "alembic.ini", action],
        backend, root, timeout=120,
    )


def build_migration_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool(response_format="content_and_artifact")
    def inspect_alembic_status_tool(
        backend_path: str = "backend",
        action: Literal["heads", "history", "current", "check"] = "heads",
    ) -> tuple[str, dict[str, Any]]:
        """Read Alembic revision heads/history/current or check model changes.

        Uses the backend's existing Alembic CLI in its own .venv. Does not run
        upgrade/downgrade; current/check may initialize the project's env.py
        and connect to its configured database.
        """
        return dev._outcome(inspect_alembic_status(
            workspace=root, backend_path=backend_path, action=action,
        ))

    inspect_alembic_status_tool.name = "inspect_alembic_status"
    inspect_alembic_status_tool.metadata = {
        "risk": "execute", "mutation": False, "workspace_arguments": ("backend_path",),
    }

    @tool(response_format="content_and_artifact")
    def generate_alembic_migration_tool(
        backend_path: str, output_path: str,
        revision: str, down_revision: str | None,
        message: str, changes: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        """Generate a reviewable Alembic revision script from explicit schema changes.

        1–12 structured create_table/add_column/create_index changes, with
        inverse downgrade operations. Reads alembic.ini, never runs migrations
        against a database. Inspect the current Alembic heads before choosing
        down_revision; generated scripts require review before application.
        """
        return dev._outcome(generate_alembic_migration(
            workspace=root, backend_path=backend_path,
            output_path=output_path, revision=revision,
            down_revision=down_revision, message=message, changes=changes,
        ))

    generate_alembic_migration_tool.name = "generate_alembic_migration"
    generate_alembic_migration_tool.metadata = {
        "risk": "write", "mutation": True,
        "workspace_arguments": ("backend_path", "output_path"),
        "mutation_paths": ("output_path",),
    }
    return [inspect_alembic_status_tool, generate_alembic_migration_tool]
