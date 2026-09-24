"""工具层：统一保存工具 Schema、Handler 和风险 metadata。

模型只看到 ``AVAILABLE_TOOLS`` 中的 OpenAI-compatible Schema；Runtime 通过
``TOOL_REGISTRY`` 找到同一个工具的 handler 和 risk level。这样新增工具时，
三个运行时要素来自同一个注册来源，不会出现只注册了 Schema 或 handler 的漂移。
"""

import os
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from config import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_READ_RESULT_CHARS,
    MAX_TOOL_RESULT_CHARS,
    PROJECT_ROOT,
    WORKSPACE_DIR,
)


class RiskLevel(str, Enum):
    """Tool 的副作用风险等级；后两个值只为未来工具预留。"""

    READ_ONLY = "READ_ONLY"
    SIDE_EFFECT = "SIDE_EFFECT"
    EXECUTION = "EXECUTION"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"


class ToolKind(str, Enum):
    """How a tool participates in the Agent loop, independent of risk."""

    NORMAL = "NORMAL"
    CONTROL_FLOW = "CONTROL_FLOW"


@dataclass(frozen=True)
class ToolDefinition:
    """一个工具的完整 Runtime 定义。"""

    name: str
    schema: dict
    handler: Callable[..., str]
    risk_level: RiskLevel
    workspace_arguments: tuple[str, ...] = ("path",)
    operation_path_argument: str | None = "path"
    preflight: Callable[[dict], None] | None = None
    tool_kind: ToolKind = ToolKind.NORMAL
    workspace_mutation: bool = False
    uses_runtime_context: bool = False

# 工具的「说明书」。发给模型的不是函数本身，而是这份 JSON 描述；
# 模型照着它生成一次工具调用请求。
# required 里必须列上 path，否则模型可能给出空参数。
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取工作目录内一个文件的文本内容。"
            "当用户想了解某个文件里写了什么时，用这个工具。"
            "默认读取从第 1 行开始的最多 100 行；如果 has_more 为 true，"
            "根据 next_start_line 决定是否继续读取下一段。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对于工作目录的路径，例如 \"todo.txt\"",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "从第几行开始读取，行号从 1 开始",
                },
                "max_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 100,
                    "description": "本次最多读取多少行",
                }
            },
            "required": ["path"],
        },
    },
}

# 「不知道有什么文件」时的第一个工具。
# path 是可选的（没有 required 字段），省略就是列工作目录本身。
# 描述里那句「不要猜文件名」是刻意的：模型有凭空编文件名的倾向，
# 把这句写进工具说明书，比在系统提示词里写「必须先看目录」更贴切——
# 前者是模型自己读到工具后自然形成的用法，后者是硬命令。
LIST_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": (
            "列出工作目录里的文件和子目录，只看一层，不递归。"
            "如果你不知道有哪些文件、或者用户没有点名具体文件，"
            "先用这个工具，不要猜文件名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录，相对工作目录。省略表示工作目录本身。",
                },
            },
        },
    },
}

SEARCH_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_text",
        "description": (
            "在工作目录内递归搜索固定字符串，返回匹配文件、行号和少量上下文。"
            "默认搜索整个工作目录；可以用 path 限定子目录。"
            "只处理可按 UTF-8 读取的文本文件，自动忽略 .git、.venv、__pycache__、"
            "sessions、eval/runs 等临时目录。query 按字面量匹配，不支持正则。"
            "没有匹配时返回正常结果，不要把它当成运行时错误。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要查找的固定字符串，例如 calculate_total",
                },
                "path": {
                    "type": "string",
                    "default": ".",
                    "description": "限定搜索的目录，相对工作目录；省略表示整个工作目录",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "最多返回多少个匹配行，默认 20，最大 100",
                },
            },
            "required": ["query"],
        },
    },
}

# 把内容写回工作目录。
# 允许覆盖已有文件——这是刻意选择，README 里有说明：
# 沙盒已经把破坏范围锁死在一个专用目录里，而拒绝覆盖会让「重跑同一个任务」
# 直接失败，还得再加一个 force 参数让模型学习怎么绕过。那比覆盖本身更复杂。
WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "把文本内容写入工作目录内的一个文件（UTF-8）。"
            "父目录不存在会自动创建，只能创建在工作目录内。"
            "已经存在的同名文件会被覆盖。"
            "这是有副作用的操作，执行前可能需要用户批准。"
            "不能用 .. 或绝对路径写到工作目录之外。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作目录的文件路径，例如 \"notes/summary.md\"",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}

APPLY_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": (
            "修改工作目录内已有的 UTF-8 文本文件。用 old_text 精确匹配一段文本，"
            "且 old_text 必须在文件中恰好出现一次；匹配 0 次或多次都会失败，"
            "不会猜测位置、模糊匹配或自动修正空白。new_text 可以为空，用于删除局部文本。"
            "这是有副作用的操作，执行前可能需要用户批准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "相对工作目录的已有文本文件路径，例如 \"calculator.py\"",
                },
                "old_text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "文件中必须唯一出现的原始文本，区分空白和换行",
                },
                "new_text": {
                    "type": "string",
                    "description": "替换后的文本；传空字符串表示删除 old_text",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
}

RENAME_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "rename_file",
        "description": (
            "重命名工作目录内已有文件；source 和 destination 都相对工作目录。"
            "目标文件若已存在则拒绝，不会覆盖。Markdown 文件改名时保留 .md 扩展名。"
            "这是有副作用的操作，执行前需要批准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "已有文件路径，例如 notes/old.md"},
                "destination": {"type": "string", "description": "新文件路径，例如 notes/new.md"},
            },
            "required": ["source", "destination"],
        },
    },
}

RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "在工作目录内执行一个受控的本地开发命令。只允许 python -m pytest、"
            "python -m unittest，以及 git status、git diff、git log；command 和 args "
            "必须分开提供，不要传入 shell script。该工具需要用户批准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "程序名，只允许 python 或 git",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "传给程序的参数数组，不是 shell 命令字符串",
                },
                "cwd": {
                    "type": "string",
                    "default": ".",
                    "description": "工作目录，必须位于 Agent workspace 内",
                },
            },
            "required": ["command"],
        },
    },
}


FINISH_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_task",
        "description": (
            "Request completion of the current Coding Task with a short user-facing summary. "
            "A Coding Task is complete only when finish_task is accepted by the Runtime Finish Gate; "
            "ordinary Final Answer text does not complete it. After the final code change, make sure "
            "the required test is still valid before requesting finish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Short final summary shown to the user when finish is accepted.",
                },
            },
            "required": ["summary"],
        },
    },
}

INSPECT_CAPABILITIES_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_capabilities",
        "description": "只读查看当前可调用 Tool 与 Runtime 自身能力及状态；回答能力问题时先调用。",
        "parameters": {"type": "object", "properties": {}},
    },
}

INSPECT_PROJECT_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_project",
        "description": (
            "只读查看这个 Agent 自身项目的核心源码和说明。省略 path 列出可读文件；"
            "指定列表中的 path 可分页读取。工作目录文件仍用 read_file。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "列出的项目文件名，例如 tools.py；省略时列目录"},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "max_lines": {"type": "integer", "minimum": 1, "default": 100},
            },
        },
    },
}

_PROJECT_INSPECTION_FILES = frozenset({
    "README.md", "main.py", "tools.py", "config.py", "capabilities.py",
    "session.py", "recovery.py", "acceptance.py", "cli.py", "agent.cmd",
    "requirements.txt",
})


def resolve_inside_workspace(path: str, workspace: str | Path | None = None) -> Path:
    """把模型给的路径解析成绝对路径，并保证它落在工作目录内。

    这是整个项目最重要的一条不变量：Agent 不许碰沙盒之外的任何东西。

    参数名必须是 path，和上面工具声明里的 "path" 保持一致：
    Phase 3 的执行层把模型给的参数**按名字**当关键字参数传进来，
    两边名字对不上，工具就永远调不动（Phase 3 实测踩过）。

    必须先 resolve() 再比较。只靠字符串前缀判断会漏掉三种情况：
     ".." 跳级、符号链接指向外部、模型直接给一个绝对路径。
    resolve() 会把这三者都还原成真实路径，比较才有意义。

    越界时抛 PermissionError，绝不静默返回空值或改成沙盒内的路径：
    边界必须显式失败，静默降级等于把它写成装饰。
    """
    root = Path(workspace if workspace is not None else WORKSPACE_DIR).resolve()
    target = (root / path).resolve()

    if not target.is_relative_to(root):
        raise PermissionError(f"路径越出工作目录，拒绝访问：{path}")

    return target


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

    selected_lines = []
    end_exclusive = start_line + max_lines
    total_lines = 0
    selected_chars = 0
    selection_stopped = False

    # Scan to EOF so the model gets an accurate total line count, but retain
    # only the requested range that fits as complete lines in the safe budget.
    with target.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            total_lines = line_number
            if not (start_line <= line_number < end_exclusive) or selection_stopped:
                continue

            line_chars = len(line)
            if not selected_lines and line_chars > MAX_READ_RESULT_CHARS:
                raise ValueError(
                    f"第 {line_number} 行长度超过单次读取安全上限，"
                    "当前行无法用行分页完整返回"
                )

            if selected_chars + line_chars > MAX_READ_RESULT_CHARS:
                selection_stopped = True
                continue

            selected_lines.append(line)
            selected_chars += line_chars

    result = _format_read_result(path, start_line, selected_lines, total_lines)

    # The reserve above covers normal metadata, but a very long path can consume
    # that reserve. Keep the final guarantee explicit without splitting a line.
    while len(result) > MAX_TOOL_RESULT_CHARS and selected_lines:
        selected_lines.pop()
        result = _format_read_result(path, start_line, selected_lines, total_lines)

    if len(result) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("读取结果元数据超过单次工具结果上限，无法返回")

    return result


def inspect_project(
    path: str | None = None, start_line: int = 1, max_lines: int = 100
) -> str:
    """List or read a fixed set of this Agent's project files, never the workspace."""
    if path is None:
        return "可读的 Agent 项目文件：\n" + "\n".join(sorted(_PROJECT_INSPECTION_FILES))
    if not isinstance(path, str) or path not in _PROJECT_INSPECTION_FILES:
        raise PermissionError("只能读取 inspect_project 列出的项目文件")
    target = (PROJECT_ROOT / path).resolve()
    if not target.is_relative_to(PROJECT_ROOT.resolve()):
        raise PermissionError("项目文件指向项目目录之外，拒绝读取")
    return _read_file_range(target, path, start_line, max_lines)


def list_files(path: str | None = None) -> str:
    """列出一层目录内容，返回适合模型阅读的文字。

    只看一层、不递归：递归会把输出撑大，而且模型通常只需要知道
    「顶层有什么」就能决定下一步读哪个。想看子目录内容，自己再调一次。

    标 [f]/[d] 区分文件和目录，文件大小让模型能判断该不该读。
    空 path 表示工作目录本身。越界照常被 resolve_inside_workspace 拦下。
    """
    target = resolve_inside_workspace(path or ".")
    if not target.is_dir():
        raise NotADirectoryError(f"不是一个目录：{path}")

    entries = sorted(target.iterdir(), key=lambda entry: entry.name)
    if not entries:
        return f"目录是空的：{path or '.'}"

    lines = []
    for entry in entries:
        kind = "d" if entry.is_dir() else "f"
        size = "" if entry.is_dir() else f"  ({entry.stat().st_size} 字节)"
        lines.append(f"[{kind}] {entry.name}{size}")

    return f"工作目录 {path or '.'} 的内容：\n" + "\n".join(lines)


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


def search_text(query: str, path: str = ".", max_results: int = 20) -> str:
    """Recursively search UTF-8 text files for a literal string."""
    if not isinstance(query, str) or not query:
        raise ValueError("query 不能为空字符串")
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or max_results < 1
        or max_results > _SEARCH_MAX_RESULTS
    ):
        raise ValueError(
            f"max_results 必须是 1 到 {_SEARCH_MAX_RESULTS} 之间的整数，当前是：{max_results!r}"
        )

    root = resolve_inside_workspace(path or ".")
    if not root.is_dir():
        raise NotADirectoryError(f"搜索路径不是目录：{path}")

    found: list[tuple[str, int, list[tuple[int, str]]]] = []
    total_matches = 0
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(WORKSPACE_DIR.resolve())
        directory_names[:] = [
            name
            for name in directory_names
            if not _search_path_is_ignored(relative_current / name)
            and (current_path / name).resolve().is_relative_to(WORKSPACE_DIR.resolve())
        ]

        for file_name in file_names:
            candidate = current_path / file_name
            relative_candidate = candidate.relative_to(WORKSPACE_DIR.resolve())
            if _search_path_is_ignored(relative_candidate):
                continue
            try:
                if not candidate.resolve().is_relative_to(WORKSPACE_DIR.resolve()):
                    continue
                with candidate.open("r", encoding="utf-8") as handle:
                    lines = handle.readlines()
            except (OSError, UnicodeDecodeError):
                continue
            if any("\x00" in line for line in lines):
                continue

            for line_number, line in enumerate(lines, start=1):
                if query not in line:
                    continue
                total_matches += 1
                if len(found) < max_results:
                    found.append(
                        (
                            relative_candidate.as_posix(),
                            line_number,
                            _search_match_context(lines, line_number, query),
                        )
                    )

    # Build the complete metadata first, then fit whole match blocks into the
    # shared result budget. This keeps matches_shown/truncated truthful.
    selected = []
    for match in found:
        candidate = selected + [match]
        rendered = _format_search_result(
            query, path or ".", candidate, total_matches, len(candidate) < total_matches
        )
        if len(rendered) > MAX_TOOL_RESULT_CHARS:
            break
        selected.append(match)

    truncated = len(selected) < total_matches
    result = _format_search_result(query, path or ".", selected, total_matches, truncated)
    if len(result) > MAX_TOOL_RESULT_CHARS:
        raise ValueError("搜索结果元数据超过单次工具结果上限，无法返回")
    if total_matches == 0:
        return f'No matches found for "{query}"\n{result}'
    return result


def write_file(path: str, content: str) -> str:
    """把文本写进工作目录内的一个文件，允许覆盖。

    父目录不存在就创建，但校验在创建之前已经做完，所以只会建在沙盒里面。
    返回文字里带上「已覆盖」还是「新文件」，让覆盖这件事显式可见。

    为什么手写 open 而不是 write_text：Path.write_text 走默认文本模式，
    在 Windows 上会把 \\n 静默翻译成 \\r\\n——落盘内容比模型给的多了 1 字节/行，
    返回值里的字节数也就说错了。指定 newline="" 让换行原样落盘，
    这样「模型写了什么 = 磁盘上有什么 = 我报告的字节数」三者一致。
    """
    target = resolve_inside_workspace(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.is_file()
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)

    size = len(content.encode("utf-8"))
    state = "已覆盖已有文件" if existed else "已写入新文件"
    return f"已写入 {target.name}（{size} 字节，{state}）"


def rename_file(source: str, destination: str) -> str:
    """Rename one file within the active workspace without replacing a target."""
    if not source or not destination:
        raise ValueError("source 和 destination 不能为空")
    source_path = resolve_inside_workspace(source)
    destination_path = resolve_inside_workspace(destination)
    if not source_path.is_file():
        raise FileNotFoundError(f"源文件不存在：{source}")
    if source_path == destination_path:
        raise ValueError("目标文件名与源文件名相同")
    if destination_path.exists():
        raise FileExistsError(f"目标已存在，拒绝覆盖：{destination}")
    if not destination_path.parent.is_dir():
        raise FileNotFoundError(f"目标目录不存在：{destination_path.parent}")
    source_path.rename(destination_path)
    return f"已重命名 {source} → {destination}"


def apply_patch(path: str, old_text: str, new_text: str) -> str:
    """Replace exactly one literal text fragment in an existing UTF-8 file."""
    if not isinstance(old_text, str) or not old_text:
        raise ValueError("old_text 不能为空")
    if not isinstance(new_text, str):
        raise ValueError("new_text 必须是字符串")
    old_text_length = len(old_text)
    new_text_length = len(new_text)

    target = resolve_inside_workspace(path)
    with target.open("r", encoding="utf-8", newline="") as handle:
        raw_content = handle.read()

    newline = "\r\n" if "\r\n" in raw_content else "\r" if "\r" in raw_content else "\n"
    content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
    old_text = old_text.replace("\r\n", "\n").replace("\r", "\n")
    new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")

    occurrences = content.count(old_text)
    if occurrences == 0:
        raise ValueError("目标文本不存在，文件没有修改")
    if occurrences > 1:
        raise ValueError(
            f"目标文本不唯一，请提供更多上下文（匹配 {occurrences} 次）"
        )

    updated = content.replace(old_text, new_text, 1)
    if newline != "\n":
        updated = updated.replace("\n", newline)
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)

    return (
        f"已应用 patch 到 {path}（replaced occurrence count = 1；"
        f"old_text length = {old_text_length}；new_text length = {new_text_length}）"
    )


# 一个工具的 Schema、Handler 和风险等级在这里一起注册。
# 测试可以临时向这个字典注册假的工具；AVAILABLE_TOOLS 只包含正式注册的工具，
# 因此测试工具不会暴露给模型。
_COMMAND_SHELL_SYNTAX = frozenset("&|;><`\r\n")
_ALLOWED_PYTHON_MODULES = frozenset({"pytest", "unittest"})
_ALLOWED_GIT_COMMANDS = frozenset({"status", "diff", "log"})


class CommandPolicyError(ValueError):
    """A command is outside the deliberately small Phase 12 allowlist."""


def _reject_shell_syntax(tokens: list[str]) -> None:
    for token in tokens:
        if any(character in token for character in _COMMAND_SHELL_SYNTAX):
            raise CommandPolicyError(
                "命令参数不能包含 shell 操作符或换行；请使用 command + args 数组"
            )


def _reject_workspace_escape_tokens(
    tokens: list[str], workspace: str | Path | None = None
) -> None:
    """Reject path-like command arguments that resolve outside the workspace."""
    for token in tokens:
        candidate = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if (
            candidate in {".", ".."}
            or "/" in candidate
            or "\\" in candidate
            or Path(candidate).is_absolute()
        ):
            try:
                resolve_inside_workspace(candidate, workspace)
            except PermissionError as exc:
                raise CommandPolicyError(f"命令参数越出工作目录：{token}") from exc


def validate_run_command_arguments(
    arguments: dict, workspace: str | Path | None = None
) -> None:
    """Validate the command policy without starting a process."""
    command = arguments.get("command")
    args = arguments.get("args", [])
    if not isinstance(command, str) or not command:
        raise CommandPolicyError("command 必须是非空字符串")
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise CommandPolicyError("args 必须是字符串数组")

    _reject_shell_syntax([command, *args])
    normalized_command = command.casefold()
    if normalized_command not in {"python", "git"} or "/" in command or "\\" in command:
        raise CommandPolicyError(f"不允许执行命令：{command}")

    if normalized_command == "python":
        if len(args) < 2 or args[0] != "-m" or args[1] not in _ALLOWED_PYTHON_MODULES:
            raise CommandPolicyError("python 只允许执行 python -m pytest 或 python -m unittest")
        if any(arg == "-c" or arg.startswith("-c") for arg in args):
            raise CommandPolicyError("禁止 python -c 任意执行代码")
        _reject_workspace_escape_tokens(args[2:], workspace)
        return

    if not args or args[0] not in _ALLOWED_GIT_COMMANDS:
        raise CommandPolicyError("git 只允许 status、diff、log 子命令")
    if any(
        arg in {"-c", "--exec-path", "--config-env", "--output", "-o", "--no-index"}
        or arg.startswith("--output=")
        for arg in args[1:]
    ):
        raise CommandPolicyError("该 git 参数可能改变状态或访问未受控目标，已拒绝")
    _reject_workspace_escape_tokens(args[1:], workspace)


def _decode_process_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact_process_output(value: object) -> str:
    text = _decode_process_output(value)
    sensitive_values = {
        value
        for key, value in os.environ.items()
        if value
        and any(
            marker in key.upper()
            for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
        )
    }
    for sensitive_value in sorted(sensitive_values, key=len, reverse=True):
        text = text.replace(sensitive_value, "[REDACTED]")

    redacted_lines = []
    for line in text.splitlines(keepends=True):
        upper = line.upper()
        if any(marker in upper for marker in ("OPENAI_API_KEY", "AUTHORIZATION", ".ENV")):
            redacted_lines.append("[sensitive output redacted]\n")
        else:
            redacted_lines.append(line)
    return "".join(redacted_lines)


def _limit_command_output(value: object) -> str:
    text = _redact_process_output(value)
    if len(text) <= MAX_COMMAND_OUTPUT_CHARS:
        return text
    omitted = len(text) - MAX_COMMAND_OUTPUT_CHARS
    return text[:MAX_COMMAND_OUTPUT_CHARS] + f"\n[output truncated: omitted {omitted} chars]"


def _format_command_result(
    command: str,
    cwd: Path,
    exit_code: int | None,
    timed_out: bool,
    stdout: object,
    stderr: object,
) -> str:
    return "\n".join(
        [
            f"Command: {command}",
            f"CWD: {cwd}",
            f"Exit code: {exit_code}",
            f"Timed out: {'true' if timed_out else 'false'}",
            "STDOUT:",
            _limit_command_output(stdout) or "<empty>",
            "STDERR:",
            _limit_command_output(stderr) or "<empty>",
        ]
    )


def run_command(
    command: str,
    args: list[str] = [],
    cwd: str = ".",
    *,
    workspace: str | Path | None = None,
) -> str:
    """Run one allowlisted local development command without a shell."""
    arguments = {"command": command, "args": args, "cwd": cwd}
    validate_run_command_arguments(arguments, workspace)
    if not isinstance(cwd, str):
        raise ValueError("cwd 必须是字符串")

    target = resolve_inside_workspace(cwd, workspace)
    if not target.is_dir():
        raise NotADirectoryError(f"cwd 不是目录：{cwd}")

    command_line = subprocess.list2cmdline([command, *args])
    executable = sys.executable if command.casefold() == "python" else command
    try:
        completed = subprocess.run(
            [executable, *args],
            cwd=str(target),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_stderr = "[命令执行超时]"
        captured_stderr = _decode_process_output(exc.stderr)
        if captured_stderr:
            timeout_stderr += f"\n{captured_stderr}"
        return _format_command_result(
            command_line,
            target,
            None,
            True,
            exc.stdout,
            timeout_stderr,
        )
    except OSError as exc:
        return _format_command_result(
            command_line,
            target,
            None,
            False,
            "",
            f"[命令启动失败] {exc}",
        )

    return _format_command_result(
        command_line,
        target,
        completed.returncode,
        False,
        completed.stdout,
        completed.stderr,
    )


def finish_task(summary: str) -> str:
    """Validate the model-facing finish summary for the control-flow dispatcher."""
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 必须是非空字符串")
    return summary.strip()


def inspect_capabilities(*, runtime_context: dict | None = None) -> str:
    from capabilities import capability_snapshot

    return json.dumps(capability_snapshot(runtime_context or {}), ensure_ascii=False, separators=(",", ":"))


TOOL_REGISTRY = {
    "list_files": ToolDefinition(
        name="list_files",
        schema=LIST_FILES_TOOL,
        handler=list_files,
        risk_level=RiskLevel.READ_ONLY,
    ),
    "read_file": ToolDefinition(
        name="read_file",
        schema=READ_FILE_TOOL,
        handler=read_file,
        risk_level=RiskLevel.READ_ONLY,
    ),
    "search_text": ToolDefinition(
        name="search_text",
        schema=SEARCH_TEXT_TOOL,
        handler=search_text,
        risk_level=RiskLevel.READ_ONLY,
    ),
    "write_file": ToolDefinition(
        name="write_file",
        schema=WRITE_FILE_TOOL,
        handler=write_file,
        risk_level=RiskLevel.SIDE_EFFECT,
        workspace_mutation=True,
    ),
    "apply_patch": ToolDefinition(
        name="apply_patch",
        schema=APPLY_PATCH_TOOL,
        handler=apply_patch,
        risk_level=RiskLevel.SIDE_EFFECT,
        workspace_mutation=True,
    ),
    "rename_file": ToolDefinition(
        name="rename_file",
        schema=RENAME_FILE_TOOL,
        handler=rename_file,
        risk_level=RiskLevel.SIDE_EFFECT,
        workspace_arguments=("source", "destination"),
        operation_path_argument=None,
        workspace_mutation=True,
    ),
    "run_command": ToolDefinition(
        name="run_command",
        schema=RUN_COMMAND_TOOL,
        handler=run_command,
        risk_level=RiskLevel.EXECUTION,
        workspace_arguments=("cwd",),
        operation_path_argument=None,
        preflight=validate_run_command_arguments,
    ),
    "finish_task": ToolDefinition(
        name="finish_task",
        schema=FINISH_TASK_TOOL,
        handler=finish_task,
        risk_level=RiskLevel.READ_ONLY,
        workspace_arguments=(),
        operation_path_argument=None,
        tool_kind=ToolKind.CONTROL_FLOW,
    ),
    "inspect_capabilities": ToolDefinition(
        name="inspect_capabilities",
        schema=INSPECT_CAPABILITIES_TOOL,
        handler=inspect_capabilities,
        risk_level=RiskLevel.READ_ONLY,
        workspace_arguments=(),
        operation_path_argument=None,
        uses_runtime_context=True,
    ),
    "inspect_project": ToolDefinition(
        name="inspect_project",
        schema=INSPECT_PROJECT_TOOL,
        handler=inspect_project,
        risk_level=RiskLevel.READ_ONLY,
        workspace_arguments=(),
        operation_path_argument=None,
    ),
}

# 这是给模型的公开 Schema 视图，不是另一份注册表。
AVAILABLE_TOOLS = [definition.schema for definition in TOOL_REGISTRY.values()]
