"""mini-agent-lab 的程序入口。

Phase 21 将 Coding Task 的完成从普通文本收口升级为显式
finish_task(summary=...) 请求。Runtime 的确定性 Finish Gate 只根据
Contract、TaskState 和 workspace 快照决定能否进入 FINISHED；独立
Verifier 仍在循环结束后重新验证最终 artifact。

普通聊天仍由模型的普通 Final Answer 收口。Coding Task 则保留提示词、
重复调用检测和 MAX_AGENT_STEPS 作为辅助机制，但不能绕过 Finish Gate。
每次请求打印 finish_reason 与 token 用量，便于观察循环行为。
"""

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from openai import APIError, OpenAI
from openai.types.chat import ChatCompletionMessage

from acceptance import (
    CodingTaskContract,
    TaskState,
    TaskStatus,
    changed_files,
    evaluate_finish_request,
    snapshot_workspace,
)
from config import (
    CONTEXT_MODES,
    MAX_AGENT_STEPS,
    MAX_RECENT_TOOL_ROUNDS,
    MAX_TOOL_RESULT_CHARS,
    REQUEST_TIMEOUT_SECONDS,
    WORKSPACE_DIR,
    LLMConfig,
    get_approval_mode,
    get_context_mode,
    load_config,
)
from tools import (
    AVAILABLE_TOOLS,
    RiskLevel,
    TOOL_REGISTRY,
    ToolKind,
    finish_task,
    resolve_inside_workspace,
)
from session import (
    SessionError,
    create_session,
    load_session,
    load_task_state,
    save_session,
)
from recovery import HARD_CEILING, Recovery

BANNER = "Mini Agent Lab"

# 输入这些词（不区分大小写）就退出
EXIT_COMMANDS = {"exit", "quit", "q"}

# 系统提示词：给模型设定身份和回答风格。
# 它常驻在对话历史的第一条，但并不是你「说」出来的话。
# 最后一句必须留着：光给 tools 参数、不在提示词里点一句，
# 模型有时会无视工具，直接凭记忆瞎答。
#
# 这里只说「有什么工具、各干什么」，不规定调用顺序。
# 顺序是模型根据任务自己决定的——写死成「必须先 list_files」
# 就把它从 Agent 降级成了照着脚本走的函数调用。
#
# 最后两段是 Phase 6 加的唯一一条「收口原则」，刻意只写几句：
# 提示词能改变的是模型的判断习惯，不是执行权限。
# 所以这里不写「写完文件就结束」——那样会把「写完→读回来确认没写坏」
# 这种合理的验证也一起砍掉。真正拦住重复动作的是重复调用检测。
SYSTEM_PROMPT = (
    "你是一个运行在命令行里的助手。直接回答用户的问题，尽量简短，不要客套开场。\n"
    "工具清单由 Runtime 提供：list_files 看工作目录里有什么，search_text 按固定字符串递归搜索，"
    "read_file 读文件内容，"
    "write_file 写入完整文本，apply_patch 对已有文件做唯一的精确局部替换，"
    "run_command 执行受控的本地开发命令，finish_task 请求结束 Coding Task，"
    "inspect_capabilities 查看当前 Tool 与 Runtime 能力。\n"
    "run_command 只能使用 command + args 数组，允许 python -m pytest、"
    "python -m unittest、git status、git diff、git log；不要使用 shell 语法、"
    "python -c、pip、PowerShell、cmd 或网络命令。cwd 必须在工作目录内。\n"
    "用户问当前有哪些工具、能执行什么或是否具有某项 Runtime 能力时，先调用 inspect_capabilities；"
    "逐项核对 currently_available，不要把注册数量当成可用数量；"
    "不要搜索工作目录源码来猜 Runtime 能力。普通知识问题无需调用。\n"
    "需要文件内容时去读，不要凭记忆编造；新建文件或确实需要整文件覆盖时用 write_file，"
    "修改已有文件的一小段时可以用 apply_patch。apply_patch 的 old_text 必须恰好匹配一次，"
    "失败时先重新 read_file，不要猜测或模糊修改。\n"
    "用户要求列出工作目录中的文件时，应包含子目录中的文件；list_files 只列一层，"
    "遇到子目录需继续查看，最终列出相对路径。\n"
    "没有读取的文件只能根据名称介绍，不能断言其具体内容、与其他文件相同或哪个版本更精简。\n"
    "用哪个工具、用什么顺序，你自己决定。\n"
    "每次拿到工具结果后，先判断用户明确要求的目标是否已经满足："
    "所需操作已成功、没有新的错误、没有缺失的信息、没有未完成的要求时，普通对话直接给出最终回答；"
    "Coding Task 则调用 finish_task(summary=...) 请求结束。\n"
    "不要为了「再确认一下」反复调用工具；需要验证有副作用的操作可以验证，"
    "但验证成功后不要反复改写同一份内容；Coding Task 应调用 finish_task。"
)

# 工具失败时的统一前缀。既是失败话术的唯一出处，
# 也用来判断「这次调用算不算成功」——只有成功执行过的调用才会进重复检测表。
TOOL_FAILURE_PREFIX = "[工具失败]"

# 用户拒绝是一个合法的工具结果，但不是工具成功执行。
APPROVAL_DENIED_PREFIX = "[用户拒绝执行]"

# 无交互审批通道是环境失败，必须和「用户明确拒绝」区分开，不能伪装成后者。
NON_INTERACTIVE_APPROVAL_ERROR = (
    "Non-interactive approval unavailable. "
    "Use TOOL_APPROVAL_MODE=ALLOW or DENY, or run in an interactive terminal."
)


class ApprovalUnavailableError(RuntimeError):
    """ASK is selected but no interactive approval channel can answer."""

ApprovalCallback = Callable[[str, dict, str], bool]

# 重复调用被拦截时回喂给模型的结果。
# 它必须是一条看起来正常的工具结果：模型靠它自己判断该收口了，
# 而不在 Python 里强行替它决定 Final Answer。
DUPLICATE_NOTICE = (
    "[重复调用被拦截]\n"
    "这个工具和完全相同的参数刚刚已经成功执行过。\n"
    "这次调用没有新的信息。\n"
    "请重新判断用户目标是否已经完成；\n"
    "如果已经完成，请直接给出最终回答。"
)

CODING_DUPLICATE_NOTICE = (
    DUPLICATE_NOTICE
    + "\n该动作刚刚已经成功执行，没有新信息。"
    + "如果 Coding Task 目标已满足，请调用 finish_task(summary=...)。"
)

COMPLETION_HINT = (
    "[Completion status]\n"
    "Required test succeeded.\n"
    "If all contract requirements are satisfied, call finish_task(summary=...) "
    "instead of repeating reads/writes/tests."
)

CODING_FINISH_PROTOCOL_NOTICE = (
    "Coding task is still RUNNING.\n"
    "To finish, call finish_task(summary=...)."
)

RequiredTest = tuple[str, tuple[str, ...], str]


def build_client(config: LLMConfig) -> OpenAI:
    """建立 OpenAI 兼容客户端。

    显式传入 api_key / base_url，而不是让 SDK 自己去读环境变量，
    是为了让「程序实际用的是什么配置」在这个文件里一眼可见，而不是藏在环境变量里。
    同一套代码换服务商，只需要改 .env 里的 OPENAI_BASE_URL。
    """
    return OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True)
class ModelReply:
    """一次模型请求的结果：回复消息 + 结束原因 + token 用量。

    Phase 5 的 ask() 只返回 message，把 finish_reason 和 usage 都丢掉了。
    后果是「模型为什么不收口」根本无从查起——只知道它在反复要工具，
    不知道它是自己没判完、还是被步数上限掐断的。

    三个统计字段允许是 None：部分兼容服务商不返回 usage，
    返回了 finish_reason 却也不是 "stop" / "tool_calls" 的字面量。
    缺字段必须能显示出来，不能让程序直接崩掉。
    """

    message: ChatCompletionMessage
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass
class CodingTaskTrace:
    """Small per-task trace for bounded coding-loop validation.

    The trace is deliberately in-memory and task-scoped.  It records enough
    evidence to explain a coding task without adding a second persistence or
    observability system to the Agent runtime.
    """

    model_calls: int = 0
    tool_calls: int = 0
    executed_tool_calls: int = 0
    list_files_calls: int = 0
    search_text_calls: int = 0
    read_file_calls: int = 0
    write_file_calls: int = 0
    apply_patch_calls: int = 0
    patch_successes: int = 0
    patch_failures: int = 0
    run_command_calls: int = 0
    productive_calls: int = 0
    duplicate_blocked: int = 0
    policy_rejected: int = 0
    failed_commands: int = 0
    successful_commands: int = 0
    finish_task_calls: int = 0
    finish_successes: int = 0
    finish_rejections: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    max_steps_reached: bool = False
    recovery_grace: str | None = None
    recovery_state: dict | None = None
    final_model_call_limit: int = MAX_AGENT_STEPS
    final_answer: str | None = None
    task_status: str | None = None
    finish_attempts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def record_model_turn(self, turn: int, reply: ModelReply) -> None:
        self.model_calls += 1
        self.prompt_tokens += reply.prompt_tokens or 0
        self.completion_tokens += reply.completion_tokens or 0
        self.total_tokens += reply.total_tokens or 0
        action = "tool_calls" if reply.message.tool_calls else "final_answer"
        event = {"turn": turn, "action": action}
        if not reply.message.tool_calls:
            self.final_answer = reply.message.content or ""
        self.events.append(event)

    def record_tool(
        self,
        turn: int,
        call,
        result: str,
        approval: str,
        executed: bool,
        classification: str | None = None,
        event_seq: int | None = None,
        finish_gate: dict | None = None,
    ) -> dict:
        tool_name = call.function.name
        arguments_summary, write_target = trace_arguments(call)
        exit_code = trace_exit_code(result)
        result_summary = result.splitlines()[0][:160] if result else "<empty>"
        classification = classification or classify_tool_call(call, result, approval)
        self.tool_calls += 1
        self.executed_tool_calls += int(executed)
        self.list_files_calls += int(tool_name == "list_files")
        self.search_text_calls += int(tool_name == "search_text")
        self.read_file_calls += int(tool_name == "read_file")
        self.write_file_calls += int(tool_name == "write_file")
        self.apply_patch_calls += int(tool_name == "apply_patch")
        if tool_name == "apply_patch":
            self.patch_successes += int(
                executed and not result.startswith(TOOL_FAILURE_PREFIX)
            )
            self.patch_failures += int(
                result.startswith(TOOL_FAILURE_PREFIX)
            )
        self.run_command_calls += int(tool_name == "run_command")
        self.productive_calls += int(classification == "PRODUCTIVE")
        self.duplicate_blocked += int(classification == "BLOCKED_DUPLICATE")
        self.policy_rejected += int(classification == "POLICY_REJECTED")
        self.failed_commands += int(classification == "FAILED_COMMAND")
        self.successful_commands += int(classification == "SUCCESSFUL_COMMAND")
        self.finish_task_calls += int(tool_name == "finish_task")
        self.finish_successes += int(classification == "FINISH_ACCEPTED")
        self.finish_rejections += int(classification == "FINISH_REJECTED")
        event = {
            "turn": turn,
            "action": "tool_call",
            "tool": tool_name,
            "arguments": arguments_summary,
            "approval": approval,
            "result": result_summary,
            "exit_code": exit_code,
            "write_target": write_target,
            "executed": executed,
            "classification": classification,
        }
        if event_seq is not None:
            event["event_seq"] = event_seq
        if finish_gate is not None:
            event["finish_gate"] = finish_gate
            self.finish_attempts.append(dict(finish_gate))
        self.events.append(event)
        return event

    def mark_max_steps(self) -> None:
        self.max_steps_reached = True

    def set_task_state(self, task_state: TaskState | None) -> None:
        if task_state is None:
            return
        self.task_status = task_state.status.value
        self.finish_attempts = list(task_state.finish_attempts)

    def summary(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "executed_tool_calls": self.executed_tool_calls,
            "list_files_calls": self.list_files_calls,
            "search_text_calls": self.search_text_calls,
            "read_file_calls": self.read_file_calls,
            "write_file_calls": self.write_file_calls,
            "apply_patch_calls": self.apply_patch_calls,
            "patch_successes": self.patch_successes,
            "patch_failures": self.patch_failures,
            "run_command_calls": self.run_command_calls,
            "productive_calls": self.productive_calls,
            "executed_tools": self.executed_tool_calls,
            "duplicate_blocked": self.duplicate_blocked,
            "policy_rejected": self.policy_rejected,
            "failed_commands": self.failed_commands,
            "successful_commands": self.successful_commands,
            "finish_task_calls": self.finish_task_calls,
            "finish_successes": self.finish_successes,
            "finish_rejections": self.finish_rejections,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "max_steps_reached": self.max_steps_reached,
            "recovery_grace": self.recovery_grace,
            "recovery_state": self.recovery_state,
            "final_model_call_limit": self.final_model_call_limit,
            "final_answer": self.final_answer,
            "task_status": self.task_status,
            "finish_attempts": list(self.finish_attempts),
            "events": list(self.events),
        }


def trace_arguments(call) -> tuple[str, str | None]:
    """Return bounded argument text and the write target for a task trace."""
    raw_arguments = call.function.arguments or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        return raw_arguments[:240], None

    if not isinstance(arguments, dict):
        return str(arguments)[:240], None

    summarized = dict(arguments)
    write_target = (
        summarized.get("path")
        if call.function.name in {"write_file", "apply_patch"}
        else None
    )
    for key in ("content", "old_text", "new_text"):
        value = summarized.get(key)
        if isinstance(value, str):
            summarized[key] = f"<{len(value)} characters>"
    return json.dumps(summarized, ensure_ascii=False, sort_keys=True)[:240], write_target


def trace_exit_code(result: str) -> str | None:
    """Extract the command exit code from a formatted run_command result."""
    for line in result.splitlines():
        if line.startswith("Exit code: "):
            return line.removeprefix("Exit code: ")
    return None


def is_duplicate_notice(result: str) -> bool:
    return result.startswith("[重复调用被拦截]")


def classify_tool_call(call, result: str, approval: str) -> str:
    """Classify one tool call using the existing Runtime result signals."""
    if approval == "DUPLICATE_BLOCKED" or is_duplicate_notice(result):
        return "BLOCKED_DUPLICATE"
    if approval in {"DENY", "BLOCKED"}:
        return "POLICY_REJECTED"
    if call.function.name == "run_command":
        return "SUCCESSFUL_COMMAND" if trace_exit_code(result) == "0" else "FAILED_COMMAND"
    return "PRODUCTIVE"


def call_matches_required_test(call, required_test: RequiredTest | None) -> bool:
    if required_test is None or call.function.name != "run_command":
        return False
    try:
        arguments = parse_tool_arguments(call)
    except ValueError:
        return False
    command, args, cwd = required_test
    return (
        arguments.get("command") == command
        and arguments.get("args", []) == list(args)
        and arguments.get("cwd", ".") == cwd
    )


def recovery_grace_limit(
    step: int,
    last_tool: tuple[object, str, bool] | None,
    required_test: RequiredTest | None,
    task_state: TaskState | None,
) -> tuple[str | None, int]:
    """Grant one terminal required-test observation a bounded continuation."""
    if MAX_AGENT_STEPS != 8 or step != 8 or last_tool is None or task_state is None:
        return None, MAX_AGENT_STEPS
    call, result, executed = last_tool
    if (
        not executed
        or not call_matches_required_test(call, required_test)
        or "Timed out: false" not in result.splitlines()
    ):
        return None, MAX_AGENT_STEPS
    exit_code = trace_exit_code(result)
    if exit_code is None:
        return None, MAX_AGENT_STEPS
    if exit_code == "0":
        test_seq = task_state.last_successful_exact_required_test_seq
        mutation_seq = task_state.last_mutation_event_seq
        if test_seq is not None and (mutation_seq is None or test_seq > mutation_seq):
            return "PASS", min(MAX_AGENT_STEPS + 1, 11)
    elif exit_code.lstrip("-").isdigit():
        return "FAIL", HARD_CEILING
    return None, MAX_AGENT_STEPS


def add_completion_hint(
    call,
    result: str,
    required_test: RequiredTest | None,
) -> str:
    if call_matches_required_test(call, required_test) and trace_exit_code(result) == "0":
        return f"{result}\n\n{COMPLETION_HINT}"
    return result


def ask(client: OpenAI, model: str, messages: list[dict]) -> ModelReply:
    """把完整对话历史发给模型，返回这一轮的结果（含结束原因和 token 用量）。

    每次调用都把 messages 整份发过去——因为 LLM 本身没有记忆，
    上下文全靠你每次重述。Agent 循环里这个列表会反复增长。

    保留 message 对象而不是它的文本 content：模型有两种回话方式——
    直接回答（content 有值），或只提工具调用（content 为 None，tool_calls 有值）。
    只返回字符串会把后一种情况静默吞掉，你会误以为模型答了个空话。

    usage 用 getattr 逐字段取，而不是 response.usage.prompt_tokens 直取：
    后者遇到不返回 usage 的服务商会 AttributeError，而这类兼容网关并不罕见。
    """
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        # tools 就是给模型的「能力清单」，由它决定用不用工具、用哪个
        tools=AVAILABLE_TOOLS,
    )
    usage = getattr(response, "usage", None)

    return ModelReply(
        message=response.choices[0].message,
        finish_reason=getattr(response.choices[0], "finish_reason", None),
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )


def format_field(value) -> str:
    """finish_reason / token 用量缺失时显示 unavailable，而不是让程序崩掉。"""
    return str(value) if value is not None else "unavailable"


def log_reply(turn: int, reply: ModelReply) -> None:
    """打印这一轮请求的可观测信息。

    注意这里**没有**任何凭据：模型名、base_url 在启动横幅里已经打过，
    这里只打请求结果本身。不把 API Key 和请求头打进日志。
    """
    print(f"\n── Turn {turn} ──")
    print(f"  finish_reason     : {format_field(reply.finish_reason)}")
    print(f"  prompt_tokens     : {format_field(reply.prompt_tokens)}")
    print(f"  completion_tokens : {format_field(reply.completion_tokens)}")
    print(f"  total_tokens      : {format_field(reply.total_tokens)}")


def parse_tool_arguments(call) -> dict:
    """把模型给的 arguments 字符串解析成参数字典。

    模型回传里 arguments 是一个 **JSON 字符串**，不是字典——这是协议规定。
    这一层不做解析，后面的 handler(**arguments) 拿到的会是一个字符串而不是参数。

    解析失败抛 ValueError：这是「模型的输出坏了」，
    和「文件读不出来」是两类完全不同的错，回喂给模型的话术也该不一样。
    """
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"工具参数不是合法 JSON：{exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return arguments


def limit_result_length(result: str) -> str:
    """把工具结果裁到 MAX_TOOL_RESULT_CHARS 以内，并在裁剪时明说。

    为什么裁在这里而不是写在每个工具里：这里是所有工具结果的唯一出口，
    放在这一处，现在的三个工具和以后任何新工具自动都有保护，不会漏。
    工具本身只管干活，不该知道模型上下文的预算。

    超限时保留**开头**——文件最有用的信息通常在前几行，而结尾往往是重复的。
    并且必须回一句「已截断、原始内容更长」：静默截断会让模型以为这就是全文，
    然后基于不完整的信息下结论。带上原始长度，模型才知道还差多少。
    """
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result

    shown = result[:MAX_TOOL_RESULT_CHARS]
    return (
        shown
        + f"\n\n[工具结果已截断：原始内容 {len(result)} 字符，"
        f"这里只显示了前 {MAX_TOOL_RESULT_CHARS} 字符，"
        f"省略 {len(result) - MAX_TOOL_RESULT_CHARS} 字符]"
    )


def execute_tool_call(call, runtime_context: dict | None = None) -> str:
    """执行一次工具调用，返回**模型能读懂的文本**结果。

    这一层做的事就三件：按名字查出 ToolDefinition → 调用 handler →
    出错就换成文字。Schema、handler 和风险等级来自同一个 TOOL_REGISTRY，
    所以这里没有 if/elif，也不认识任何具体工具。

    为什么出错要换成文字，而不是让异常继续往上抛：
    模型看不见 traceback，它只吃得下自然语言。而且工具失败不该中断整段会话——
    用户问错文件名时，正确答案是「告诉模型没有这个文件，让它换个问法」，
    而不是让程序崩掉。
    """
    try:
        definition = TOOL_REGISTRY.get(call.function.name)
        if definition is None:
            return f"{TOOL_FAILURE_PREFIX} 没有名为 {call.function.name} 的工具，无法执行"
        if definition.tool_kind is ToolKind.CONTROL_FLOW:
            return f"{TOOL_FAILURE_PREFIX} 控制流工具必须由 Runtime 专用分发器处理"
        arguments = parse_tool_arguments(call)
        if definition.uses_runtime_context:
            return limit_result_length(definition.handler(**arguments, runtime_context=runtime_context))
        return limit_result_length(definition.handler(**arguments))
    except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        # OSError 涵盖了 FileNotFoundError / PermissionError / IsADirectoryError，
        # 也就是沙盒拦截、文件不存在、路径指向目录这几类情况。
        return f"{TOOL_FAILURE_PREFIX} {type(exc).__name__}：{exc}"


def always_allow(tool_name: str, arguments: dict, operation: str) -> bool:
    """Non-interactive approval policy for tests and evals."""
    return True


def always_deny(tool_name: str, arguments: dict, operation: str) -> bool:
    """Non-interactive denial policy for tests and evals."""
    return False


def _is_terminal(stream) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def interactive_approval_available(stdin=None, stdout=None) -> bool:
    """ASK needs a human who can both read the prompt and type the answer.

    Windows 上 isatty() 对 NUL 等字符设备也返回 True，所以只看 stdin 会把
    stdin 指向 NUL 的 subprocess 误判成交互终端；两端都是终端才算有审批通道。
    """
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    return _is_terminal(input_stream) and _is_terminal(output_stream)


def ask_for_approval(tool_name: str, arguments: dict, operation: str) -> bool:
    """CLI approval callback; input stays at the CLI boundary, not in execution."""
    if not interactive_approval_available():
        raise ApprovalUnavailableError(NON_INTERACTIVE_APPROVAL_ERROR)
    print("\nAgent 请求执行有副作用的工具：")
    print(f"工具：{tool_name}")
    if "path" in arguments:
        print(f"文件：{arguments.get('path', '?')}")
    print(f"操作：{operation}")
    try:
        answer = input("是否允许？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n审批未确认，按拒绝处理。")
        return False
    return answer in {"y", "yes"}


def approval_callback_for_mode(mode: str, input_func=None) -> ApprovalCallback:
    """Build the small approval policy selected by the caller/environment."""
    selected_mode = mode.strip().upper()
    if selected_mode == "ALLOW":
        return always_allow
    if selected_mode == "DENY":
        return always_deny
    if selected_mode == "ASK":
        if input_func is None:
            return ask_for_approval

        def ask_with_injected_input(tool_name: str, arguments: dict, operation: str) -> bool:
            print("\nAgent 请求执行有副作用的工具：")
            print(f"工具：{tool_name}")
            if "path" in arguments:
                print(f"文件：{arguments.get('path', '?')}")
            print(f"操作：{operation}")
            answer = input_func("是否允许？[y/N] ").strip().lower()
            return answer in {"y", "yes"}

        return ask_with_injected_input
    raise ValueError(f"审批模式必须是 ASK, ALLOW, DENY 之一，当前是：{mode!r}")


def approval_needs_interactive_input(mode: str) -> bool:
    """Only ASK blocks a headless process; ALLOW and DENY decide by themselves."""
    return mode.strip().upper() == "ASK" and not interactive_approval_available()


def check_tool_permission(call, approval_callback: ApprovalCallback) -> tuple[bool, str | None]:
    """Check permission before execution; return (may_execute, immediate_result)."""
    tool_name = call.function.name
    definition = TOOL_REGISTRY.get(tool_name)
    if (
        definition is None
        or definition.tool_kind is ToolKind.CONTROL_FLOW
        or definition.risk_level is RiskLevel.READ_ONLY
    ):
        return True, None

    try:
        arguments = parse_tool_arguments(call)
        operation = definition.risk_level.value
        for argument_name in definition.workspace_arguments:
            value = arguments.get(argument_name)
            if value is None:
                continue
            if not isinstance(value, str):
                # Let the handler produce the normal missing/invalid-argument error.
                return True, None

            # Sandbox validation is deliberately before asking the user. Approval
            # cannot turn an invalid path into an allowed one.
            target = resolve_inside_workspace(value)
            if argument_name == definition.operation_path_argument:
                operation = "OVERWRITE" if target.is_file() else "CREATE"

        if definition.preflight is not None:
            definition.preflight(arguments)
        if approval_callback(tool_name, arguments, operation):
            return True, None
        return False, (
            f"{APPROVAL_DENIED_PREFIX}\n"
            f"{tool_name} 未执行。\n"
            "文件没有被修改。"
        )
    except (OSError, TypeError, ValueError) as exc:
        return False, f"{TOOL_FAILURE_PREFIX} {type(exc).__name__}：{exc}"


def assistant_tool_call_message(
    message: ChatCompletionMessage, calls=None
) -> dict:
    """把模型那条「提了工具调用」的回复原样写回对话历史。

    tool_calls 必须原样带上，尤其是 arguments 要保留模型当初给的**原始字符串**：
    重新序列化一遍可能改变字段顺序或转义方式，部分服务商会因此直接 400。
    """
    selected_calls = message.tool_calls if calls is None else calls
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in selected_calls
        ],
    }


def tool_result_message(call, content: str) -> dict:
    """把工具结果包成 role="tool" 的消息，准备回喂给模型。

    tool_call_id 必须和模型当初那条调用的 id 对得上——这是协议里
    「这次结果对应哪次调用」的唯一凭据。漏了它，模型和服务商都会困惑。
    """
    return {"role": "tool", "tool_call_id": call.id, "content": content}


def tool_calls_through_control_flow(message: ChatCompletionMessage) -> list:
    """Keep calls through the first control-flow request for canonical history."""
    selected = []
    for call in message.tool_calls or []:
        selected.append(call)
        definition = TOOL_REGISTRY.get(call.function.name)
        if definition is not None and definition.tool_kind is ToolKind.CONTROL_FLOW:
            break
    return selected


def _compact_write_arguments(arguments: str) -> str | None:
    """Return a valid, smaller historical view of a completed write call."""
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict) or not isinstance(parsed.get("content"), str):
        return None

    compact = dict(parsed)
    compact["content"] = (
        f"[previous write content omitted; {len(parsed['content'])} characters]"
    )
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _compact_write_round(message: dict, results: list[dict]) -> dict | None:
    """Make a protocol-preserving view of successful write calls in a tool round."""
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    if len(results) != len(calls):
        return None

    compact_calls = []
    changed = False
    for call, result in zip(calls, results):
        if call["function"]["name"] != "write_file":
            compact_calls.append(call)
            continue

        if (
            result.get("role") != "tool"
            or result.get("tool_call_id") != call.get("id")
            or not isinstance(result.get("content"), str)
            or result["content"].startswith(TOOL_FAILURE_PREFIX)
            or result["content"].startswith(APPROVAL_DENIED_PREFIX)
            or is_duplicate_notice(result["content"])
        ):
            return None

        compact_arguments = _compact_write_arguments(
            call["function"].get("arguments", "")
        )
        if compact_arguments is None:
            return None

        compact_call = dict(call)
        compact_function = dict(call["function"])
        compact_function["arguments"] = compact_arguments
        compact_call["function"] = compact_function
        compact_calls.append(compact_call)
        changed = True

    if not changed:
        return None

    compact_message = dict(message)
    compact_message["tool_calls"] = compact_calls
    return compact_message


def _tool_round(messages: list[dict], index: int) -> tuple[dict, list[dict]] | None:
    """Return one complete assistant-tool round, or leave malformed history alone."""
    message = messages[index]
    if message.get("role") != "assistant":
        return None

    calls = message.get("tool_calls") or []
    if not calls:
        return None

    results = messages[index + 1 : index + 1 + len(calls)]
    if len(results) != len(calls):
        return None
    if any(
        result.get("role") != "tool"
        or result.get("tool_call_id") != call.get("id")
        for call, result in zip(calls, results)
    ):
        return None

    return message, results


def _read_reference(call: dict, result: dict) -> str | None:
    """Replace an old successful read with a small instruction to read it again."""
    content = result.get("content")
    if (
        call["function"]["name"] != "read_file"
        or not isinstance(content, str)
        or content.startswith(TOOL_FAILURE_PREFIX)
        or is_duplicate_notice(content)
    ):
        return None

    try:
        arguments = json.loads(call["function"].get("arguments") or "{}")
        path = arguments.get("path", "?")
    except (json.JSONDecodeError, AttributeError):
        path = "?"

    return (
        "[historical read compacted]\n"
        f"path: {path}\n"
        f"characters: {len(content)}\n"
        "The full content is no longer in this context; call read_file again if needed."
    )


def _compact_old_round(message: dict, results: list[dict]) -> tuple[dict, list[dict]]:
    """Compact only recoverable payloads in an old, otherwise valid tool round."""
    compact_message = _compact_write_round(message, results)
    if compact_message is None:
        compact_message = message

    compact_results = []
    for call, result in zip(message["tool_calls"], results):
        compact_result = _read_reference(call, result)
        compact_results.append(
            {**result, "content": compact_result}
            if compact_result is not None
            else result
        )

    return compact_message, compact_results


def build_model_context(messages: list[dict], mode: str | None = None) -> list[dict]:
    """Build the outbound model view without changing canonical history.

    OFF sends the original history unchanged. WRITE_ONLY compacts successful
    write contents in every completed tool round but leaves read results and
    round recency untouched. FULL keeps the existing write/read compaction and
    recent-round policy.
    """
    selected_mode = get_context_mode() if mode is None else mode.strip().upper()
    if selected_mode not in CONTEXT_MODES:
        allowed = ", ".join(CONTEXT_MODES)
        raise ValueError(f"Context mode 必须是 {allowed} 之一，当前是：{selected_mode!r}")
    if selected_mode == "OFF":
        return list(messages)

    rounds: list[tuple[int, int, dict, list[dict]]] = []
    index = 0
    while index < len(messages):
        found = _tool_round(messages, index)
        if found is None:
            index += 1
            continue
        message, results = found
        rounds.append((index, index + 1 + len(message["tool_calls"]), message, results))
        index += 1 + len(message["tool_calls"])

    recent_rounds = (
        {start for start, _, _, _ in rounds[-MAX_RECENT_TOOL_ROUNDS:]}
        if selected_mode == "FULL"
        else set()
    )
    context: list[dict] = []
    index = 0

    while index < len(messages):
        round_at_index = next(
            (item for item in rounds if item[0] == index),
            None,
        )
        if round_at_index is not None:
            _, end, message, results = round_at_index
            if index in recent_rounds:
                context.append(message)
                context.extend(results)
            else:
                if selected_mode == "WRITE_ONLY":
                    compact_message = _compact_write_round(message, results) or message
                    compact_results = results
                else:
                    compact_message, compact_results = _compact_old_round(message, results)
                context.append(compact_message)
                context.extend(compact_results)
            index = end
            continue

        context.append(messages[index])
        index += 1

    return context


def call_fingerprint(call) -> tuple[str, str]:
    """把一次工具调用压成可比较的指纹：(工具名, 规范化后的参数)。

    为什么要规范化，而不是直接拿 arguments 字符串比：
    协议规定 arguments 是 JSON 字符串，而 JSON 字符串「写法不同、含义相同」
    的情况非常常见——键序不同、多余空格、转义不同。
    Phase 5.5 真模型实测就出现过这种：两次 write_file 内容逐字节相同，
    只是 "path" 和 "content" 的先后换了个位置，字符串对比会漏掉它。
    解析成字典再按 sort_keys 重新序列化，这类等价调用才能被识别成同一次。

    解析失败时退回原始字符串、不做拦截：拿不准的时候宁可放行。
    错杀一次正常调用（比如模型正在改参数重试）比放过一次重复更糟。
    """
    try:
        parsed = json.loads(call.function.arguments or "{}")
        canonical = json.dumps(
            parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except json.JSONDecodeError:
        canonical = call.function.arguments or ""

    return (call.function.name, canonical)


def counts_as_successful_duplicate(call, result: str) -> bool:
    """Decide whether a result should lock an identical call for this task.

    A non-zero process exit is a real execution result, but it is not a
    successful coding action.  Keeping it out of the duplicate set lets the
    model repair the code and rerun the same test command.
    """
    if (
        result.startswith(TOOL_FAILURE_PREFIX)
        or result.startswith(APPROVAL_DENIED_PREFIX)
        or is_duplicate_notice(result)
    ):
        return False
    if call.function.name == "inspect_capabilities":
        return False  # Its answer may change during the same task.
    if call.function.name == "run_command":
        return trace_exit_code(result) == "0"
    return True


def _finish_control_flow_result(
    call,
    contract: CodingTaskContract | None,
    task_state: TaskState | None,
) -> tuple[str, str, int | None, dict | None]:
    """Dispatch finish_task without using permission or duplicate flow."""
    try:
        arguments = parse_tool_arguments(call)
        if set(arguments) != {"summary"}:
            raise ValueError("finish_task 只接受 summary 参数")
        summary = finish_task(arguments["summary"])
    except ValueError as exc:
        return f"{TOOL_FAILURE_PREFIX} {type(exc).__name__}：{exc}", "PRODUCTIVE", None, None

    event_seq = task_state.next_event() if task_state is not None else None
    gate_state = task_state or TaskState()
    decision = evaluate_finish_request(contract, gate_state, WORKSPACE_DIR)
    gate_result = decision.as_dict()
    if event_seq is not None:
        gate_result["event_seq"] = event_seq
        task_state.record_finish_attempt(summary, decision.accepted, list(decision.reasons))
    result = json.dumps(gate_result, ensure_ascii=False, separators=(",", ":"))
    return (
        result,
        "FINISH_ACCEPTED" if decision.accepted else "FINISH_REJECTED",
        event_seq,
        gate_result,
    )


def _update_task_state_after_normal_tool(
    task_state: TaskState | None,
    definition,
    call,
    result: str,
    may_execute: bool,
    required_test: RequiredTest | None,
    event_seq: int | None,
    pre_mutation_snapshot: dict[str, str] | None,
) -> bool:
    """Record only real mutations and successful exact required tests."""
    if task_state is None or event_seq is None:
        return False

    if (
        pre_mutation_snapshot is not None
        and may_execute
        and not result.startswith(TOOL_FAILURE_PREFIX)
        and not result.startswith(APPROVAL_DENIED_PREFIX)
    ):
        post_mutation_snapshot = snapshot_workspace(WORKSPACE_DIR)
        if changed_files(pre_mutation_snapshot, post_mutation_snapshot):
            task_state.last_mutation_event_seq = event_seq
            mutated = True
        else:
            mutated = False
    else:
        mutated = False

    if (
        may_execute
        and not result.startswith(TOOL_FAILURE_PREFIX)
        and call_matches_required_test(call, required_test)
        and trace_exit_code(result) == "0"
    ):
        task_state.last_successful_exact_required_test_seq = event_seq
    return mutated


def _record_runtime_error(
    task_state: TaskState | None,
    trace: CodingTaskTrace | None,
    exc: Exception,
) -> None:
    """Persist a fatal Runtime failure without treating normal Tool Results as errors."""
    if task_state is None:
        return
    task_state.status = TaskStatus.ERROR
    task_state.unresolved_runtime_error = f"{type(exc).__name__}: {exc}"
    if trace is not None:
        trace.set_task_state(task_state)


def _print_trace_event(event: dict) -> None:
    print(
        "Trace > "
        f"Turn {event['turn']} | tool={event['tool']} | "
        f"args={event['arguments']} | approval={event['approval']} | "
        f"result={event['result']} | exit_code={event['exit_code']} | "
        f"write_target={event['write_target']}"
    )


def run_tool_round(
    messages: list[dict],
    message: ChatCompletionMessage,
    executed: set[tuple[str, str]],
    approval_callback: ApprovalCallback,
    trace: CodingTaskTrace | None = None,
    turn: int | None = None,
    required_test: RequiredTest | None = None,
    contract: CodingTaskContract | None = None,
    task_state: TaskState | None = None,
    round_events: list[str] | None = None,
    recovery: Recovery | None = None,
    session_active: bool = False,
    verifier_enabled: bool = False,
) -> tuple[object, str, bool] | None:
    """执行这一批工具调用，把「模型提了调用」和「调用结果」都写进历史。

    做完之后历史长这样：…assistant(tool_calls), tool(result)。
    这两条必须成对出现，而且顺序要对，否则下一次请求会被服务商拒绝。

    executed 是「本任务里已成功执行过的调用指纹」集合。
    命中指纹时**不执行**，但照样按协议回喂一条正常工具结果——
    那条 assistant tool_call 还是要进历史，配对关系不能断。

    注意这里**没有** ask。执行完要不要再问一次模型、问了几次就够，
    是外层循环的事——把「执行」和「决定要不要继续」分开，
    循环才能只写在它该出现的那一处。
    返回最后一条工具结果及其实际执行状态，供轮次边界判断使用。
    """
    calls = tool_calls_through_control_flow(message)
    messages.append(assistant_tool_call_message(message, calls))
    last_tool = None

    for call in calls:
        print(f"\nAgent 想调用工具：{call.function.name}({call.function.arguments})")
        definition = TOOL_REGISTRY.get(call.function.name)

        if definition is not None and definition.tool_kind is ToolKind.CONTROL_FLOW:
            result, classification, event_seq, gate_result = _finish_control_flow_result(
                call, contract, task_state
            )
            print(f"Tool 结果 > {result}")
            messages.append(tool_result_message(call, result))
            last_tool = (call, result, True)
            if round_events is not None:
                round_events.append(classification)
            if trace is not None:
                event = trace.record_tool(
                    turn or 0,
                    call,
                    result,
                    "CONTROL_FLOW",
                    True,
                    classification,
                    event_seq,
                    gate_result,
                )
                trace.set_task_state(task_state)
                _print_trace_event(event)
            break

        event_seq = task_state.next_event() if task_state is not None else None

        fingerprint = call_fingerprint(call)
        if fingerprint in executed:
            # 不重复干活，把「这次没新信息」作为一条普通工具结果回喂。
            # 是否收口仍然由模型自己决定。
            duplicate_notice = (
                CODING_DUPLICATE_NOTICE if required_test is not None else DUPLICATE_NOTICE
            )
            print("（重复调用被拦截，未真正执行）")
            print(f"Tool 结果 > {duplicate_notice}")
            messages.append(tool_result_message(call, duplicate_notice))
            last_tool = (call, duplicate_notice, False)
            if trace is not None:
                event = trace.record_tool(
                    turn or 0,
                    call,
                    duplicate_notice,
                    "DUPLICATE_BLOCKED",
                    False,
                    event_seq=event_seq,
                )
                trace.set_task_state(task_state)
                _print_trace_event(event)
            continue

        may_execute, permission_result = check_tool_permission(call, approval_callback)
        pre_mutation_snapshot = (
            snapshot_workspace(WORKSPACE_DIR)
            if (
                task_state is not None
                and may_execute
                and definition is not None
                and definition.workspace_mutation
            )
            else None
        )
        result = permission_result if not may_execute else execute_tool_call(
            call,
            {"contract": contract, "task_state": task_state,
             "recovery": recovery, "session_active": session_active,
             "verifier_enabled": verifier_enabled},
        )
        if may_execute:
            result = add_completion_hint(call, result, required_test)
        mutated = _update_task_state_after_normal_tool(
            task_state,
            definition,
            call,
            result,
            may_execute,
            required_test,
            event_seq,
            pre_mutation_snapshot,
        )
        if round_events is not None:
            if mutated:
                round_events.append("MUTATION")
            elif (
                may_execute
                and call_matches_required_test(call, required_test)
                and "Timed out: false" in result.splitlines()
            ):
                exit_code = trace_exit_code(result)
                if exit_code is not None and exit_code.lstrip("-").isdigit():
                    round_events.append("TEST_PASS" if exit_code == "0" else "TEST_FAIL")
        if may_execute and counts_as_successful_duplicate(call, result):
            # 只有成功执行过的调用才记下来。失败的那次不该被锁定——
            # 模型换个参数重试是合理行为，拦它才是帮倒忙。
            executed.add(fingerprint)

        print(f"Tool 结果 > {result}")
        messages.append(tool_result_message(call, result))
        last_tool = (call, result, may_execute and definition is not None)
        if trace is not None:
            if definition is None:
                approval = "N/A"
            elif definition.risk_level is RiskLevel.READ_ONLY:
                approval = "AUTO"
            elif not may_execute:
                approval = (
                    "DENY"
                    if result.startswith(APPROVAL_DENIED_PREFIX)
                    else "BLOCKED"
                )
            else:
                approval = "ALLOW"
            tool_executed = may_execute and definition is not None
            event = trace.record_tool(
                turn or 0,
                call,
                result,
                approval,
                tool_executed,
                event_seq=event_seq,
            )
            trace.set_task_state(task_state)
            _print_trace_event(event)

    return last_tool


def finalize(messages: list[dict], message: ChatCompletionMessage) -> None:
    """模型这一轮没有要调工具——这就是最终回答，记进历史并打印出来。"""
    messages.append({"role": "assistant", "content": message.content or ""})
    print(f"\nAgent > {message.content or ''}")


def run_agent_loop(
    client: OpenAI,
    model: str,
    messages: list[dict],
    first_reply: ModelReply,
    executed: set[tuple[str, str]],
    approval_callback: ApprovalCallback,
    trace: CodingTaskTrace | None = None,
    required_test: RequiredTest | None = None,
    contract: CodingTaskContract | None = None,
    task_state: TaskState | None = None,
    session_active: bool = False,
    verifier_enabled: bool = False,
) -> None:
    """把「问模型 → 执行工具 → 回喂 → 再问」装进循环。

    它不认识普通工具的具体名字；CONTROL_FLOW 工具由专用分发器处理。

    循环体只做一件事：问一次模型。然后分岔——
      有 tool_calls → 执行并回喂 → 回到循环开头再问一次
      没有 tool_calls → 普通聊天直接收口；Coding Task 提醒模型调用 finish_task
      finish_task 被 Gate 接受 → Coding Task 进入 FINISHED

    为什么 while 写在这一处、而不是散在几个地方：
    模型每次回话都可能要求继续调工具，谁来决定「够了，停」只能是这个循环。

    MAX_AGENT_STEPS 是最后一层保险丝，数的是「问了几次模型」。
    每个允许的模型响应都会先完整处理到第一个 CONTROL_FLOW 调用为止，
    因而最后一步合法的 finish_task 可以优先进入 FINISHED。若该步没有
    成功完成，所有已处理的工具结果仍保留在 canonical history，随后状态才
    进入 LIMIT_REACHED。
    """
    if contract is not None:
        if task_state is None:
            task_state = TaskState(initial_snapshot=snapshot_workspace(WORKSPACE_DIR))
        if required_test is None:
            required_test = (
                contract.test_command.command,
                contract.test_command.args,
                contract.test_command.cwd,
            )
        if trace is not None:
            trace.set_task_state(task_state)

    reply = first_reply
    final_limit = MAX_AGENT_STEPS
    grace_trigger = None
    recovery: Recovery | None = None
    if trace is not None:
        trace.final_model_call_limit = final_limit

    for step in range(1, HARD_CEILING + 1):
        if trace is not None:
            trace.record_model_turn(step, reply)
        if not reply.message.tool_calls:
            finalize(messages, reply.message)
            if trace is not None:
                print(f"Trace > Turn {step} | action=Final Answer")
            if contract is None:
                return
            stop_recovery = recovery.observe([], step) if recovery is not None else False
            if recovery is not None and trace is not None:
                trace.recovery_state = vars(recovery).copy()
            if stop_recovery or step == final_limit:
                task_state.status = TaskStatus.LIMIT_REACHED
                if trace is not None:
                    trace.mark_max_steps()
                    trace.set_task_state(task_state)
                print(f"\n[已达到最大步骤数 {final_limit}，Coding Task 未调用 finish_task]")
                return

            messages.append({"role": "user", "content": CODING_FINISH_PROTOCOL_NOTICE})
            print(f"Runtime > {CODING_FINISH_PROTOCOL_NOTICE}")
            try:
                reply = ask(client, model, build_model_context(messages))
            except Exception as exc:
                _record_runtime_error(task_state, trace, exc)
                raise
            log_reply(step + 1, reply)
            continue

        try:
            round_events: list[str] = []
            last_tool = run_tool_round(
                messages,
                reply.message,
                executed,
                approval_callback,
                trace=trace,
                turn=step,
                required_test=required_test,
                contract=contract,
                task_state=task_state,
                round_events=round_events,
                recovery=recovery,
                session_active=session_active,
                verifier_enabled=verifier_enabled,
            )
        except Exception as exc:
            _record_runtime_error(task_state, trace, exc)
            raise
        if task_state is not None and task_state.status is TaskStatus.FINISHED:
            if recovery is not None:
                recovery.observe(["FINISH_ACCEPTED"], step)
            if trace is not None:
                if recovery is not None:
                    trace.recovery_state = vars(recovery).copy()
                trace.set_task_state(task_state)
            return

        if step == MAX_AGENT_STEPS and grace_trigger is None and contract is not None:
            grace_trigger, final_limit = recovery_grace_limit(
                step, last_tool, required_test, task_state
            )
            if trace is not None:
                trace.recovery_grace = grace_trigger
                trace.final_model_call_limit = final_limit
            if grace_trigger == "FAIL":
                recovery = Recovery()
                if trace is not None:
                    trace.recovery_state = vars(recovery).copy()

        stop_recovery = recovery.observe(round_events, step) if recovery is not None and step > MAX_AGENT_STEPS else False
        if recovery is not None and trace is not None:
            trace.recovery_state = vars(recovery).copy()

        if stop_recovery or step == final_limit:
            if task_state is not None:
                task_state.status = TaskStatus.LIMIT_REACHED
            if trace is not None:
                trace.mark_max_steps()
                trace.set_task_state(task_state)
            print(f"\n[已达到最大步骤数 {final_limit}，停止当前任务]")
            return

        # Keep canonical history intact; only shrink the outbound model view.
        try:
            reply = ask(client, model, build_model_context(messages))
        except Exception as exc:
            _record_runtime_error(task_state, trace, exc)
            raise
        log_reply(step + 1, reply)


def print_environment(config: LLMConfig) -> None:
    """启动时打印本次运行的关键信息：用的是哪个模型、有哪些工具、沙盒在不在。"""
    tool_names = ", ".join(tool["function"]["name"] for tool in AVAILABLE_TOOLS)
    workspace_state = "就绪" if WORKSPACE_DIR.is_dir() else "缺失（Agent 将无法读写文件）"

    print(BANNER)
    print(f"模型：{config.model} @ {config.base_url}")
    print(f"工作目录：{WORKSPACE_DIR}（{workspace_state}）")
    print(f"可用工具：{tool_names}")
    print(f"单个任务最多 {MAX_AGENT_STEPS} 步（模型问了几轮就停，防止无限循环）")
    print(f"工具结果上限 {MAX_TOOL_RESULT_CHARS} 字符（超过会截断并告知模型）")
    print(f"Context 模式：{get_context_mode()}")
    print(f"审批模式：{get_approval_mode()}")
    print("重复调用保护：同一工具 + 完全相同参数已成功执行过，就不会重复执行")
    print("每次请求会打印 finish_reason 和 token 用量（服务商没返回就显示 unavailable）")


def restore_coding_session(
    record: dict,
) -> tuple[CodingTaskContract | None, TaskState | None]:
    """Restore the paired Coding Contract and TaskState from one Session."""
    encoded_contract = record.get("coding_contract")
    if encoded_contract is None:
        return None, None
    try:
        contract = CodingTaskContract.from_dict(encoded_contract)
    except ValueError as exc:
        raise SessionError(f"Session 的 coding_contract 无效：{exc}") from exc
    task_state = load_task_state(record)
    if task_state.status is not TaskStatus.RUNNING:
        raise SessionError(
            f"Coding Session 状态为 {task_state.status.value}，不能继续恢复执行"
        )
    return contract, task_state


def update_coding_session_record(
    record: dict,
    contract: CodingTaskContract,
    task_state: TaskState,
    messages: list[dict],
    model: str,
    context_mode: str,
) -> None:
    """Keep a resumable Coding Session limited to canonical runtime facts."""
    record["messages"] = messages
    record["model"] = model
    record["context_mode"] = context_mode
    record["coding_contract"] = contract.as_dict()
    record["task_state"] = task_state.as_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the interactive Mini Agent")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复一个已保存的 Session")
    args = parser.parse_args(argv)

    config = load_config()
    print_environment(config)

    coding_contract: CodingTaskContract | None = None
    task_state: TaskState | None = None
    if args.resume:
        try:
            record = load_session(args.resume)
            coding_contract, task_state = restore_coding_session(record)
        except SessionError as exc:
            print(f"[Session错误] {exc}", file=sys.stderr)
            return 2
        messages = record["messages"]
        session_id = record["session_id"]
        print(f"Session: {session_id}（已恢复）")
    else:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        record = create_session(config.model, messages, get_context_mode())
        session_id = record["session_id"]
        save_session(record)
        print(f"Session: {session_id}")

    client = build_client(config)
    approval_callback = approval_callback_for_mode(get_approval_mode())
    print("输入一句话开始对话；输入 exit 退出。")

    try:
        while True:
            try:
                user_input = input("\n你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C / Ctrl+D 应当体面退出，而不是甩一屏 traceback
                print("\n再见。")
                return 0

            if not user_input:
                continue
            if user_input.lower() in EXIT_COMMANDS:
                print("再见。")
                return 0

            # 记下这条提问在历史里的位置。失败时要退回到它之前，
            # 把悬空提问和它后面产生的半截记录（工具调用、工具结果）一起清掉。
            messages.append({"role": "user", "content": user_input})
            position = len(messages) - 1

            # 每个任务一份「已成功执行过的调用」指纹表，任务结束就丢。
            # 跨任务不清的话，上一轮的正常调用会被这一轮误判成重复。
            executed: set[tuple[str, str]] = set()
            trace = CodingTaskTrace()

            try:
                # canonical history remains the source; only the outbound view is compressed.
                first_reply = ask(client, config.model, build_model_context(messages))
                log_reply(1, first_reply)
                run_agent_loop(
                    client,
                    config.model,
                    messages,
                    first_reply,
                    executed,
                    approval_callback,
                    trace=trace,
                    contract=coding_contract,
                    task_state=task_state,
                    session_active=True,
                )
            except (APIError, ConnectionError, TimeoutError, ApprovalUnavailableError) as exc:
                # 只捕获「跟外界通信」相关的失败：鉴权、限流、超时、网络不通、审批通道缺失。
                # 代码自身的 bug 不在此列，应当让它正常抛出来，方便你发现真问题。
                if coding_contract is not None and task_state is not None:
                    _record_runtime_error(task_state, trace, exc)
                    update_coding_session_record(
                        record,
                        coding_contract,
                        task_state,
                        messages,
                        config.model,
                        get_context_mode(),
                    )
                    save_session(record)
                    print(f"\n[请求失败] {exc}")
                    return 1
                del messages[position:]
                print(f"\n[请求失败] {exc}")
                continue

            if coding_contract is not None and task_state is not None:
                update_coding_session_record(
                    record,
                    coding_contract,
                    task_state,
                    messages,
                    config.model,
                    get_context_mode(),
                )
                save_session(record)
                print(f"Session 已保存：{session_id}")
                if task_state.status is TaskStatus.FINISHED:
                    return 0
                if task_state.status in {TaskStatus.LIMIT_REACHED, TaskStatus.ERROR}:
                    return 1
                continue

            last = messages[-1] if messages else {}
            completed = last.get("role") == "assistant" and not last.get("tool_calls")
            if completed:
                record["messages"] = messages
                record["model"] = config.model
                record["context_mode"] = get_context_mode()
                save_session(record)
                print(f"Session 已保存：{session_id}")
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
