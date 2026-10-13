"""Coordinated patches spanning multiple source files.

All changes are precomputed and validated before any file is written. The
LangGraph boundary supplies approval, contract scoping and change journaling.
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

MAX_PATCH_FILES = 12
MAX_PATCH_PREVIEW = 6500


def prepare_patch_batch(
    patches: list[dict[str, str]], workspace: Path,
) -> tuple[dict[Path, bytes], dict[Path, bytes]]:
    """Validate entire batch; return source and output bytes for each path."""
    root = Path(workspace).resolve()
    if not isinstance(patches, list) or not 2 <= len(patches) <= MAX_PATCH_FILES:
        raise ValueError(f"一次跨文件补丁须包含 2–{MAX_PATCH_FILES} 个文件")
    before: dict[Path, bytes] = {}
    after: dict[Path, bytes] = {}
    for item in patches:
        if not isinstance(item, dict) or set(item) != {"path", "old_text", "new_text"}:
            raise ValueError("每个补丁必须包含 path、old_text、new_text")
        path, old, new = item["path"], item["old_text"], item["new_text"]
        if not isinstance(path, str) or not path or not isinstance(old, str) or not old:
            raise ValueError("补丁路径和原文本必须为非空字符串")
        if not isinstance(new, str):
            raise ValueError("补丁新文本必须为字符串")
        sandbox.encode_content(old, "old_text")
        sandbox.encode_content(new, "new_text")
        target = sandbox.resolve_path(path, root)
        if _search_path_is_ignored(target.relative_to(root)):
            raise PermissionError("跨文件补丁禁止写入缓存、虚拟环境和依赖目录")
        if target in before:
            raise ValueError("同一批次不能重复修改同一个文件")
        if not target.is_file() or target.is_symlink():
            raise ValueError("跨文件补丁只支持已有的普通源码文件")
        old_bytes = sandbox.read_bounded_bytes(target)
        before[target] = old_bytes
        after[target] = sandbox.patched_content(old_bytes, old, new)
        if before[target] == after[target]:
            raise ValueError("替换后内容无变化")
    return before, after


def patch_batch_precondition(patches: list[dict[str, str]], workspace: Path) -> dict[str, Any]:
    """Capture immutable content digests for HITL resumes without leaking source."""
    root = Path(workspace).resolve()
    before, _ = prepare_patch_batch(patches, root)
    return {
        "workspace": str(root),
        "args_sha256": hashlib.sha256(
            json.dumps(patches, sort_keys=True, ensure_ascii=True).encode("utf-8"),
        ).hexdigest(),
        "files": {
            p.relative_to(root).as_posix(): hashlib.sha256(b).hexdigest()
            for p, b in before.items()
        },
    }


def preview_patch_batch(patches: list[dict[str, str]], workspace: Path) -> str:
    root = Path(workspace).resolve()
    before, after = prepare_patch_batch(patches, root)
    sections: list[str] = []
    for path in before:
        label = path.relative_to(root).as_posix()
        change = difflib.unified_diff(
            before[path].decode("utf-8").splitlines(keepends=True),
            after[path].decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{label}", tofile=f"b/{label}",
        )
        sections.append("".join(change))
    text = sandbox.redact_text("\n".join(sections))
    return text[:MAX_PATCH_PREVIEW] + ("\n[差异预览截断]" if len(text) > MAX_PATCH_PREVIEW else "")


def apply_patch_batch(patches: list[dict[str, str]], *, workspace: Path) -> str:
    """Write a validated multi-file change, restoring earlier files on an error."""
    root = Path(workspace).resolve()
    before, after = prepare_patch_batch(patches, root)
    applied: list[Path] = []
    recorded: dict[Path, bytes | None] = dict(after)
    with sandbox.file_change(recorded, root):
        try:
            for target, updated in after.items():
                # Fail closed on external modifications during the batch.
                if sandbox.read_bounded_bytes(target) != before[target]:
                    raise PermissionError("提交期间文件内容发生变化，停止跨文件写入")
                # The atomic replace may succeed before a later fsync/close error.
                # Track the in-flight target first so rollback covers it too.
                applied.append(target)
                sandbox.atomic_write_bytes(target, updated)
        except BaseException as exc:
            rollback_errors = []
            for target in reversed(applied):
                try:
                    sandbox.atomic_write_bytes(target, before[target])
                except OSError as error:
                    rollback_errors.append(str(error))
            if rollback_errors:
                raise ValueError("跨文件补丁失败且部分回滚未完成，请人工核对全部相关文件") from exc
            raise
    names = [p.relative_to(root).as_posix() for p in after]
    result = json.dumps({"modified": names, "count": len(names), "rollback_required": False}, ensure_ascii=False)
    if len(result) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("跨文件补丁结果超过输出上限")
    return result


def build_multi_file_tools(workspace: Path) -> list[Any]:
    root = Path(workspace).resolve()

    @tool
    def apply_patch_batch_tool(patches: list[dict[str, str]]) -> str:
        """Apply exact unique text replacements to 2-12 different existing files.

        Use after checking code dependencies. Prevalidate every patch before
        writing; if an individual write fails, restore prior files. For new
        files use write_file separately. Requires one standalone approved call.
        """
        return apply_patch_batch(patches, workspace=root)

    apply_patch_batch_tool.name = "apply_patch_batch"
    apply_patch_batch_tool.metadata = {
        "risk": "write",
        "mutation": True,
        "mutation_paths": ("patches",),
    }
    return [apply_patch_batch_tool]
