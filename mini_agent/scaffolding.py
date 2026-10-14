"""Multi-file project scaffolding for the existing LangGraph coding runtime.

Only creates NEW UTF-8 files. Existing-file edits continue to use apply_patch_batch.
The caller's standard boundary handles approval, contracts and task journaling.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any

from langchain.tools import tool

from . import sandbox
from .config import MAX_TOOL_RESULT_CHARS
from .tools.filesystem import _search_path_is_ignored

MAX_SCAFFOLD_FILES = 12
MAX_SCAFFOLD_PREVIEW = 6500


def prepare_file_creations(
    files: list[dict[str, str]], workspace: Path,
) -> dict[Path, bytes]:
    """Validate an entire collection before creating a single file."""
    if not isinstance(files, list) or not 2 <= len(files) <= MAX_SCAFFOLD_FILES:
        raise ValueError(f"一次批量创建须包含 2–{MAX_SCAFFOLD_FILES} 个文件")
    root = workspace.resolve()
    prepared: dict[Path, bytes] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ValueError("每项必须仅包含 path 和 content")
        path, content = item["path"], item["content"]
        if not isinstance(path, str) or not path.strip():
            raise ValueError("创建文件路径不能为空")
        target = sandbox.resolve_path(path, root)
        if _search_path_is_ignored(target.relative_to(root)):
            raise PermissionError("批量创建不能写入生成目录或依赖目录")
        if target in prepared:
            raise ValueError("同一批次不能包含重复路径")
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"目标文件已存在：{target.relative_to(root).as_posix()}")
        prepared[target] = sandbox.encode_content(content)
    for target in prepared:
        if any(other in target.parents or target in other.parents for other in prepared if other != target):
            raise ValueError("文件路径不能同时充当另一个文件的目录")
    return prepared


def creation_precondition(files: list[dict[str, str]], workspace: Path) -> dict[str, Any]:
    """Capture intended paths and arguments before a human-approval interrupt."""
    root = workspace.resolve()
    planned = prepare_file_creations(files, root)
    return {
        "workspace": str(root),
        "args_sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
        "new_paths": sorted(target.relative_to(root).as_posix() for target in planned),
    }


def preview_file_creations(files: list[dict[str, str]], workspace: Path) -> str:
    root = workspace.resolve()
    planned = prepare_file_creations(files, root)
    sections = []
    for target, content in planned.items():
        name = target.relative_to(root).as_posix()
        diff = difflib.unified_diff(
            [], content.decode("utf-8").splitlines(keepends=True),
            fromfile="/dev/null", tofile=f"b/{name}",
        )
        sections.append("".join(diff) or f"新建空文件：{name}")
    preview = sandbox.redact_text("\n".join(sections))
    return preview[:MAX_SCAFFOLD_PREVIEW] + (
        "\n[批量文件预览已截断]" if len(preview) > MAX_SCAFFOLD_PREVIEW else ""
    )


def create_files_batch(files: list[dict[str, str]], *, workspace: Path) -> str:
    """Create new files and any missing parent directories in one journaled batch.

    This is not a crash-atomic filesystem transaction. A normal runtime error
    removes created files and tries to clean up newly created empty directories.
    """
    root = workspace.resolve()
    planned = prepare_file_creations(files, root)
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    with sandbox.file_change(dict(planned), root):
        try:
            for target, content in planned.items():
                missing: list[Path] = []
                parent = target.parent
                while parent != root and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for folder in reversed(missing):
                    folder.mkdir()
                    created_dirs.append(folder)
                # 'xb' fails instead of overwriting a file created by another process.
                with target.open("xb") as handle:
                    created_files.append(target)
                    handle.write(content)
                    handle.flush()
        except BaseException as exc:
            errors: list[str] = []
            for target in reversed(created_files):
                try:
                    target.unlink()
                except OSError as error:
                    errors.append(str(error))
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    # If another process put files here, leave its directory alone.
                    pass
            if errors:
                raise ValueError("批量创建失败且未完全回滚，请核对新建文件") from exc
            raise
    names = [target.relative_to(root).as_posix() for target in planned]
    result = json.dumps({"created": names, "count": len(names)}, ensure_ascii=False)
    if len(result) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("创建结果超过工具输出上限")
    return result


def build_scaffold_tools(workspace: Path) -> list[Any]:
    root = workspace.resolve()

    @tool
    def create_files_batch_tool(files: list[dict[str, str]]) -> str:
        """Create 2–12 new UTF-8 files, including missing parent directories.

        Useful for implementing a new feature across routes, services and tests.
        Never overwrites existing files; use apply_patch_batch for edits.
        Requires one standalone approved tool call.
        """
        return create_files_batch(files, workspace=root)

    create_files_batch_tool.name = "create_files_batch"
    create_files_batch_tool.metadata = {
        "risk": "write", "mutation": True, "mutation_paths": ("files",),
    }
    return [create_files_batch_tool]
