"""Canonical tool registry and model-facing schemas."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .command import CommandPolicyError, run_command, validate_run_command_arguments
from .filesystem import apply_patch, list_files, read_file, rename_file, search_text, write_file
from .introspection import finish_task, inspect_capabilities, inspect_project

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
