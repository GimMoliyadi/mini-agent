"""Workspace bounded filesystem tools.

The legacy top-level tools module remains a compatibility facade.
"""

import io
import os
import sys
from pathlib import Path

from mini_agent import sandbox as file_safety
from mini_agent.config import (
    MAX_READ_RESULT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    WORKSPACE_DIR,
)


def _active_workspace() -> Path:
    # Existing callers patch tools.WORKSPACE_DIR; keep that contract while
    # allowing package users to use the configured default.
    compat = sys.modules.get("tools")
    value = getattr(compat, "WORKSPACE_DIR", WORKSPACE_DIR) if compat is not None else WORKSPACE_DIR
    return Path(value)


def _active_constant(name: str, default: int) -> int:
    compat = sys.modules.get("tools")
    value = getattr(compat, name, default) if compat is not None else default
    return value


def resolve_inside_workspace(path: str, workspace: str | Path | None = None) -> Path:
    return file_safety.resolve_path(path, workspace if workspace is not None else _active_workspace())


def _validate_positive_line_argument(name: str, value: int) -> None:
    """Reject non-positive or non-integer line range arguments clearly."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是大于等于 1 的整数，当前是：{value!r}")


def _format_read_result(
    path: str, start_line: int, selected_lines: list[str], total_lines: int
) -> str:
    """Format a result whose metadata describes exactly ``selected_lines``."""
    if selected_lines:
        end_line = start_line + len(selected_lines) - 1
        current_range = f"{start_line}-{end_line}"
    else:
        end_line = start_line - 1
        current_range = "无"

    has_more = end_line < total_lines
    next_start_line = end_line + 1 if has_more else None
    content = "".join(selected_lines)

    return "\n".join(
        [
            f"文件：{path}",
            f"当前范围：{current_range} 行",
            f"总行数：{total_lines}",
            f"has_more：{'true' if has_more else 'false'}",
            f"next_start_line：{next_start_line if next_start_line is not None else 'null'}",
            "",
            "--- 内容开始 ---",
            content,
            "--- 内容结束 ---",
        ]
    )


def read_file(path: str, start_line: int = 1, max_lines: int = 100) -> str:
    """Read one line range from a UTF-8 file and report how to continue.

    纯函数：成功返回内容，失败抛原生异常
    （FileNotFoundError / PermissionError / UnicodeDecodeError）。

    不吞异常、也不返回错误字符串——「怎么把失败说给模型听」
    是 Phase 3 执行层的职责，工具本身不该知道模型的存在。
    """
    target = resolve_inside_workspace(path)
    return _read_file_range(target, path, start_line, max_lines)


def _read_file_range(target: Path, path: str, start_line: int, max_lines: int) -> str:
    _validate_positive_line_argument("start_line", start_line)
    _validate_positive_line_argument("max_lines", max_lines)

    selected_lines: list[str] = []
    end_exclusive = start_line + max_lines
    total_lines = 0
    selected_chars = 0
    selection_stopped = False

    # Scan to EOF so the model gets an accurate total line count, but retain
    # only the requested range that fits as complete lines in the safe budget.
    with io.StringIO(file_safety.read_bounded_bytes(target).decode("utf-8"), newline=None) as handle:
        for line_number, line in enumerate(handle, start=1):
            total_lines = line_number
            if not (start_line <= line_number < end_exclusive) or selection_stopped:
                continue

            line_chars = len(line)
            if not selected_lines and line_chars > _active_constant("MAX_READ_RESULT_CHARS", MAX_READ_RESULT_CHARS):
                raise ValueError(
                    f"第 {line_number} 行长度超过单次读取安全上限，"
                    "当前行无法用行分页完整返回"
                )

            if selected_chars + line_chars > _active_constant("MAX_READ_RESULT_CHARS", MAX_READ_RESULT_CHARS):
                selection_stopped = True
                continue

            selected_lines.append(line)
            selected_chars += line_chars

    result = file_safety.redact_text(_format_read_result(path, start_line, selected_lines, total_lines))
    while len(result) > _active_constant("MAX_TOOL_RESULT_CHARS", MAX_TOOL_RESULT_CHARS) and selected_lines:
        selected_lines.pop()
        result = file_safety.redact_text(_format_read_result(path, start_line, selected_lines, total_lines))
    if not selected_lines and start_line <= total_lines:
        raise ValueError(f"第 {start_line} 行脱敏后超过单次结果上限，无法用行分页完整返回")
    if len(result) > _active_constant("MAX_TOOL_RESULT_CHARS", MAX_TOOL_RESULT_CHARS):
        raise ValueError("读取结果元数据超过单次工具结果上限，无法返回")
    return result

def list_files(path: str | None = None) -> str:
    target = resolve_inside_workspace(path or ".")
    if not target.is_dir():
        raise NotADirectoryError(f"不是一个目录：{path}")
    entries = []
    truncated = False
    with os.scandir(target) as iterator:
        for index, entry in enumerate(iterator):
            if index >= MAX_DIRECTORY_ENTRIES:
                truncated = True
                break
            try:
                resolve_inside_workspace(str(Path(entry.path)))
            except (PermissionError, OSError):
                continue
            kind = "d" if entry.is_dir() else "f"
            size = "" if kind == "d" else f"  ({entry.stat().st_size} 字节)"
            entries.append((entry.name, f"[{kind}] {entry.name}{size}"))
    if not entries and not truncated:
        return f"目录为空或没有可访问条目：{path or '.'}"
    header = f"工作目录 {path or '.'} 的内容：\n"
    lines = []
    used = len(header)
    for _, line in sorted(entries):
        if used + len(line) + 1 > _active_constant("MAX_TOOL_RESULT_CHARS", MAX_TOOL_RESULT_CHARS) - len("\n[目录列表已截断]"):
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    result = header + "\n".join(lines)
    if truncated:
        result += "\n[目录列表已截断]"
    return file_safety.redact_text(result)


MAX_DIRECTORY_ENTRIES = 1000
MAX_SEARCH_ENTRIES = 5000
MAX_SEARCH_FILES = 1000
MAX_SEARCH_SCAN_BYTES = 16 * 1024 * 1024
_SEARCH_MAX_RESULTS = 100
_SEARCH_CONTEXT_LINES = 1
_SEARCH_SNIPPET_CHARS = 240
_SEARCH_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "sessions",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)
_SEARCH_IGNORED_PATHS = (("eval", "runs"),)
SearchMatch = tuple[str, int, list[tuple[int, str]]]


def _search_path_is_ignored(relative_path: Path) -> bool:
    parts = relative_path.parts
    if any(part in _SEARCH_IGNORED_DIRS for part in parts):
        return True
    return any(parts[: len(prefix)] == prefix for prefix in _SEARCH_IGNORED_PATHS)


def _shorten_search_line(line: str, query: str) -> str:
    if len(line) <= _SEARCH_SNIPPET_CHARS:
        return line

    match_start = line.find(query)
    if match_start < 0:
        return line[: _SEARCH_SNIPPET_CHARS - 3] + "..."

    half_window = (_SEARCH_SNIPPET_CHARS - len(query) - 6) // 2
    start = max(0, match_start - max(half_window, 0))
    end = min(len(line), start + _SEARCH_SNIPPET_CHARS - 6)
    start = max(0, end - (_SEARCH_SNIPPET_CHARS - 6))
    prefix = "..." if start else ""
    suffix = "..." if end < len(line) else ""
    return prefix + line[start:end] + suffix


def _format_search_result(
    query: str,
    path: str,
    matches: list[tuple[str, int, list[tuple[int, str]]]],
    total_matches: int,
    truncated: bool,
) -> str:
    header = [
        f'query: "{query}"',
        f"path: {path}",
        f"matches_shown: {len(matches)}",
        f"matches_total: {total_matches}",
        f"truncated: {'true' if truncated else 'false'}",
    ]
    if not matches:
        return "\n".join(header)

    blocks = []
    for relative_path, match_line_number, context in matches:
        lines = [f"\n{relative_path}:{match_line_number}"]
        lines.extend(f"{number} | {line}" for number, line in context)
        blocks.append("\n".join(lines))
    return "\n".join(header + blocks)


def _search_match_context(lines: list[str], line_number: int, query: str) -> list[tuple[int, str]]:
    start = max(1, line_number - _SEARCH_CONTEXT_LINES)
    end = min(len(lines), line_number + _SEARCH_CONTEXT_LINES)
    return [
        (number, _shorten_search_line(lines[number - 1].rstrip("\r\n"), query))
        for number in range(start, end + 1)
    ]


def _iter_search_files(root: Path, scan: dict):
    workspace = _active_workspace().resolve()
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                scan["entries"] += 1
                if scan["entries"] > _active_constant("MAX_SEARCH_ENTRIES", MAX_SEARCH_ENTRIES):
                    scan["truncated"] = True
                    return
                candidate = Path(entry.path)
                relative = candidate.relative_to(workspace)
                if _search_path_is_ignored(relative):
                    continue
                try:
                    safe = resolve_inside_workspace(str(candidate))
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(safe)
                    elif entry.is_file():
                        yield safe, relative
                except (OSError, PermissionError):
                    continue


def _search_file_lines(candidate: Path, scan: dict) -> list[str] | None:
    scan["files"] += 1
    if scan["files"] > _active_constant("MAX_SEARCH_FILES", MAX_SEARCH_FILES):
        scan["truncated"] = True
        return None
    try:
        size = candidate.stat().st_size
        if size > file_safety.MAX_FILE_BYTES:
            scan["truncated"] = True
            return None
        remaining = _active_constant("MAX_SEARCH_SCAN_BYTES", MAX_SEARCH_SCAN_BYTES) - scan["bytes"]
        if size > remaining:
            scan["truncated"] = True
            return None
        content = file_safety.read_bounded_bytes(candidate, min(remaining, file_safety.MAX_FILE_BYTES))
        scan["bytes"] += len(content)
        if b"\x00" in content:
            return None
        return content.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _render_search_matches(query: str, path: str, found: list[SearchMatch], total_matches: int, scan: dict) -> str:
    selected: list[SearchMatch] = []
    metadata = f"\nscan_truncated: {'true' if scan['truncated'] else 'false'}\nscanned_bytes: {scan['bytes']}"
    for match in found:
        candidate = selected + [match]
        rendered = _format_search_result(query, path, candidate, total_matches, len(candidate) < total_matches or scan["truncated"])
        if len(rendered) + len(metadata) > _active_constant("MAX_TOOL_RESULT_CHARS", MAX_TOOL_RESULT_CHARS):
            break
        selected.append(match)
    result = _format_search_result(query, path, selected, total_matches, len(selected) < total_matches or scan["truncated"]) + metadata
    if total_matches == 0:
        result = f'No matches found for "{query}"\n{result}'
    result = file_safety.redact_text(result)
    if len(result) > _active_constant("MAX_TOOL_RESULT_CHARS", MAX_TOOL_RESULT_CHARS):
        raise ValueError("搜索结果元数据超过单次工具结果上限，无法返回")
    return result


def search_text(query: str, path: str = ".", max_results: int = 20) -> str:
    if not isinstance(query, str) or not query:
        raise ValueError("query 不能为空字符串")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= _SEARCH_MAX_RESULTS:
        raise ValueError(f"max_results 必须是 1 到 {_SEARCH_MAX_RESULTS} 之间的整数")
    root = resolve_inside_workspace(path or ".")
    if not root.is_dir():
        raise NotADirectoryError(f"搜索路径不是目录：{path}")
    found: list[SearchMatch] = []
    total_matches = 0
    scan = {"entries": 0, "files": 0, "bytes": 0, "truncated": False}
    for candidate, relative in _iter_search_files(root, scan):
        lines = _search_file_lines(candidate, scan)
        if scan["files"] > MAX_SEARCH_FILES:
            scan["truncated"] = True
            break
        if lines is None:
            continue
        for line_number, line in enumerate(lines, start=1):
            if query in line:
                total_matches += 1
                if len(found) < max_results:
                    found.append((relative.as_posix(), line_number, _search_match_context(lines, line_number, query)))
    return _render_search_matches(query, path or ".", found, total_matches, scan)


def write_file(path: str, content: str) -> str:
    encoded = file_safety.encode_content(content)
    target = resolve_inside_workspace(path)
    existed = target.is_file()
    if target.exists():
        file_safety.read_bounded_bytes(target)
    with file_safety.file_change({target: encoded}, _active_workspace()):
        file_safety.atomic_write_bytes(target, encoded)
    state = "已覆盖已有文件" if existed else "已写入新文件"
    return f"已写入 {target.name}（{len(encoded)} 字节，{state}）"


def rename_file(source: str, destination: str) -> str:
    source_path = resolve_inside_workspace(source)
    destination_path = resolve_inside_workspace(destination)
    content = file_safety.read_bounded_bytes(source_path)
    if source_path == destination_path:
        raise ValueError("目标文件名与源文件名相同")
    if destination_path.exists():
        raise FileExistsError(f"目标已存在，拒绝覆盖：{destination}")
    if not destination_path.parent.is_dir():
        raise FileNotFoundError(f"目标目录不存在：{destination_path.parent}")
    with file_safety.file_change({source_path: None, destination_path: content}, _active_workspace()):
        file_safety.rename_no_replace(source_path, destination_path)
    return f"已重命名 {source} → {destination}"


def apply_patch(path: str, old_text: str, new_text: str) -> str:
    file_safety.encode_content(old_text, "old_text")
    file_safety.encode_content(new_text, "new_text")
    if not old_text:
        raise ValueError("old_text 不能为空")
    target = resolve_inside_workspace(path)
    updated = file_safety.patched_content(file_safety.read_bounded_bytes(target), old_text, new_text)
    with file_safety.file_change({target: updated}, _active_workspace()):
        file_safety.atomic_write_bytes(target, updated)
    return (
        f"已应用 patch 到 {path}（replaced occurrence count = 1；"
        f"old_text length = {len(old_text)}；new_text length = {len(new_text)}）"
    )


# 一个工具的 Schema、Handler 和风险等级在这里一起注册。
# 测试可以临时向这个字典注册假的工具；AVAILABLE_TOOLS 只包含正式注册的工具，
# 因此测试工具不会暴露给模型。
