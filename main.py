"""mini-agent-lab 的程序入口（Phase 10：工具权限与副作用审批）。

这一阶段做的事不是加能力，而是解决 Phase 5.5 真模型实测暴露的问题：
任务其实已经做完了，Agent 却不知道自己该停。

三条独立的防线，互不替代：
  1. 系统提示词里的收口原则 —— 让模型自己判断「目标满足了就回答」
  2. 重复调用检测 —— 同一工具 + 完全相同参数已成功执行过，就不重复执行，
     而是把「这次没新信息」作为一条正常工具结果回喂，让模型自己收口
  3. MAX_AGENT_STEPS —— 最后的保险丝，仍然数「问了几次模型」

外加可观测性：每次请求都会打印 finish_reason 和 token 用量。
Phase 5 里 ask() 只返回 message，这两项被直接丢掉，
结果「模型为什么不收口」这类问题只能靠肉眼数工具调用次数。

仍然刻意不做的事：不禁止「写完再读回来验证」、不写死任何工具顺序、
不在 Python 里强行替模型做 Final Answer 的决定、不记 Memory。
"""

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from openai import APIError, OpenAI
from openai.types.chat import ChatCompletionMessage

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
from tools import AVAILABLE_TOOLS, RiskLevel, TOOL_REGISTRY, resolve_inside_workspace
from session import SessionError, create_session, load_session, save_session

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
    "你有四个工具：list_files 看工作目录里有什么，read_file 读文件内容，"
    "write_file 把内容写进工作目录，run_command 执行受控的本地开发命令。\n"
    "run_command 只能使用 command + args 数组，允许 python -m pytest、"
    "python -m unittest、git status、git diff、git log；不要使用 shell 语法、"
    "python -c、pip、PowerShell、cmd 或网络命令。cwd 必须在工作目录内。\n"
    "需要文件内容时去读，不要凭记忆编造；用户希望结果被保存下来时用 write_file。\n"
    "用户要求列出工作目录中的文件时，应包含子目录中的文件；list_files 只列一层，"
    "遇到子目录需继续查看，最终列出相对路径。\n"
    "没有读取的文件只能根据名称介绍，不能断言其具体内容、与其他文件相同或哪个版本更精简。\n"
    "用哪个工具、用什么顺序，你自己决定。\n"
    "每次拿到工具结果后，先判断用户明确要求的目标是否已经满足："
    "所需操作已成功、没有新的错误、没有缺失的信息、没有未完成的要求，就直接给出最终回答。\n"
    "不要为了「再确认一下」反复调用工具；需要验证有副作用的操作可以验证，"
    "但验证成功后要收口，不要反复改写同一份内容。"
)

# 工具失败时的统一前缀。既是失败话术的唯一出处，
# 也用来判断「这次调用算不算成功」——只有成功执行过的调用才会进重复检测表。
TOOL_FAILURE_PREFIX = "[工具失败]"

# 用户拒绝是一个合法的工具结果，但不是工具成功执行。
APPROVAL_DENIED_PREFIX = "[用户拒绝执行]"

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
    + "如果 Coding Task 目标已满足，请直接 Final Answer。"
)

COMPLETION_HINT = (
    "[Completion status]\n"
    "Command succeeded.\n"
    "If this satisfies the requested coding task and no work remains, "
    "return the final answer instead of repeating reads/writes/tests."
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
    write_file_calls: int = 0
    run_command_calls: int = 0
    productive_calls: int = 0
    duplicate_blocked: int = 0
    policy_rejected: int = 0
    failed_commands: int = 0
    successful_commands: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    max_steps_reached: bool = False
    final_answer: str | None = None
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
    ) -> dict:
        tool_name = call.function.name
        arguments_summary, write_target = trace_arguments(call)
        exit_code = trace_exit_code(result)
        result_summary = result.splitlines()[0][:160] if result else "<empty>"
        classification = classification or classify_tool_call(call, result, approval)
        self.tool_calls += 1
        self.executed_tool_calls += int(executed)
        self.write_file_calls += int(tool_name == "write_file")
        self.run_command_calls += int(tool_name == "run_command")
        self.productive_calls += int(classification == "PRODUCTIVE")
        self.duplicate_blocked += int(classification == "BLOCKED_DUPLICATE")
        self.policy_rejected += int(classification == "POLICY_REJECTED")
        self.failed_commands += int(classification == "FAILED_COMMAND")
        self.successful_commands += int(classification == "SUCCESSFUL_COMMAND")
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
        self.events.append(event)
        return event

    def mark_max_steps(self) -> None:
        self.max_steps_reached = True

    def summary(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "executed_tool_calls": self.executed_tool_calls,
            "write_file_calls": self.write_file_calls,
            "run_command_calls": self.run_command_calls,
            "productive_calls": self.productive_calls,
            "executed_tools": self.executed_tool_calls,
            "duplicate_blocked": self.duplicate_blocked,
            "policy_rejected": self.policy_rejected,
            "failed_commands": self.failed_commands,
            "successful_commands": self.successful_commands,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "max_steps_reached": self.max_steps_reached,
            "final_answer": self.final_answer,
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
    write_target = summarized.get("path") if call.function.name == "write_file" else None
    content = summarized.get("content")
    if isinstance(content, str):
        summarized["content"] = f"<{len(content)} characters>"
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


def execute_tool_call(call) -> str:
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
        arguments = parse_tool_arguments(call)
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


def ask_for_approval(tool_name: str, arguments: dict, operation: str) -> bool:
    """CLI approval callback; input stays at the CLI boundary, not in execution."""
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


def check_tool_permission(call, approval_callback: ApprovalCallback) -> tuple[bool, str | None]:
    """Check permission before execution; return (may_execute, immediate_result)."""
    tool_name = call.function.name
    definition = TOOL_REGISTRY.get(tool_name)
    if definition is None or definition.risk_level is RiskLevel.READ_ONLY:
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


def assistant_tool_call_message(message: ChatCompletionMessage) -> dict:
    """把模型那条「提了工具调用」的回复原样写回对话历史。

    tool_calls 必须原样带上，尤其是 arguments 要保留模型当初给的**原始字符串**：
    重新序列化一遍可能改变字段顺序或转义方式，部分服务商会因此直接 400。
    """
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
            for call in message.tool_calls
        ],
    }


def tool_result_message(call, content: str) -> dict:
    """把工具结果包成 role="tool" 的消息，准备回喂给模型。

    tool_call_id 必须和模型当初那条调用的 id 对得上——这是协议里
    「这次结果对应哪次调用」的唯一凭据。漏了它，模型和服务商都会困惑。
    """
    return {"role": "tool", "tool_call_id": call.id, "content": content}


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
    if call.function.name == "run_command":
        return trace_exit_code(result) == "0"
    return True


def run_tool_round(
    messages: list[dict],
    message: ChatCompletionMessage,
    executed: set[tuple[str, str]],
    approval_callback: ApprovalCallback,
    trace: CodingTaskTrace | None = None,
    turn: int | None = None,
    required_test: RequiredTest | None = None,
) -> None:
    """执行这一批工具调用，把「模型提了调用」和「调用结果」都写进历史。

    做完之后历史长这样：…assistant(tool_calls), tool(result)。
    这两条必须成对出现，而且顺序要对，否则下一次请求会被服务商拒绝。

    executed 是「本任务里已成功执行过的调用指纹」集合。
    命中指纹时**不执行**，但照样按协议回喂一条正常工具结果——
    那条 assistant tool_call 还是要进历史，配对关系不能断。

    注意这里**没有** ask。执行完要不要再问一次模型、问了几次就够，
    是外层循环的事——把「执行」和「决定要不要继续」分开，
    循环才能只写在它该出现的那一处。
    """
    messages.append(assistant_tool_call_message(message))

    for call in message.tool_calls:
        print(f"\nAgent 想调用工具：{call.function.name}({call.function.arguments})")

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
            if trace is not None:
                event = trace.record_tool(
                    turn or 0, call, duplicate_notice, "DUPLICATE_BLOCKED", False
                )
                print(
                    "Trace > "
                    f"Turn {event['turn']} | tool={event['tool']} | "
                    f"args={event['arguments']} | approval={event['approval']} | "
                    f"result={event['result']} | exit_code={event['exit_code']} | "
                    f"write_target={event['write_target']}"
                )
            continue

        may_execute, permission_result = check_tool_permission(call, approval_callback)
        result = permission_result if not may_execute else execute_tool_call(call)
        if may_execute:
            result = add_completion_hint(call, result, required_test)
        if may_execute and counts_as_successful_duplicate(call, result):
            # 只有成功执行过的调用才记下来。失败的那次不该被锁定——
            # 模型换个参数重试是合理行为，拦它才是帮倒忙。
            executed.add(fingerprint)

        print(f"Tool 结果 > {result}")
        messages.append(tool_result_message(call, result))
        if trace is not None:
            definition = TOOL_REGISTRY.get(call.function.name)
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
            )
            print(
                "Trace > "
                f"Turn {event['turn']} | tool={event['tool']} | "
                f"args={event['arguments']} | approval={event['approval']} | "
                f"result={event['result']} | exit_code={event['exit_code']} | "
                f"write_target={event['write_target']}"
            )


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
) -> None:
    """把「问模型 → 执行工具 → 回喂 → 再问」装进循环。

    它不认识任何工具名字，所以工具从 1 个变成 3 个，它照样工作。

    循环体只做一件事：问一次模型。然后分岔——
      有 tool_calls → 执行并回喂 → 回到循环开头再问一次
      没有 tool_calls → 这就是最终回答，循环结束
    退出循环只有这两条路，没有第三条。

    为什么 while 写在这一处、而不是散在几个地方：
    模型每次回话都可能要求继续调工具，谁来决定「够了，停」只能是这个循环。

    MAX_AGENT_STEPS 是最后一层保险丝，数的是「问了几次模型」：
    每一轮都先问再执行，所以能执行的工具轮数最多是 MAX_AGENT_STEPS - 1
    （最后一次问必须用来收口给答案，否则工具结果发不出去、没有模型回答）。
    它不是唯一那一层——正常情况应该由模型自己判完就收口，
    重复调用检测负责在它原地打转时提醒一次；走到这里说明前两层都没拦住。
    达到上限时任务就此停止，模型最后一句「我还要调工具」被丢掉——
    那条孤立记录故意不进历史，下一轮提问的历史必须始终是合法的。
    """
    reply = first_reply

    for step in range(1, MAX_AGENT_STEPS + 1):
        if trace is not None:
            trace.record_model_turn(step, reply)
        if not reply.message.tool_calls:
            # 模型决定直接回答：当前任务完成
            finalize(messages, reply.message)
            if trace is not None:
                print(f"Trace > Turn {step} | action=Final Answer")
            return

        if step == MAX_AGENT_STEPS:
            if trace is not None:
                trace.mark_max_steps()
            print(
                f"\n[已达到最大步骤数 {MAX_AGENT_STEPS}，停止当前任务；"
                "此时模型仍要求调用工具]"
            )
            return

        # 还有预算，就执行并回喂；下一轮循环再问模型，由它决定继续还是收口
        run_tool_round(
            messages,
            reply.message,
            executed,
            approval_callback,
            trace=trace,
            turn=step,
            required_test=required_test,
        )
        # Keep canonical history intact; only shrink the outbound model view.
        reply = ask(client, model, build_model_context(messages))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the interactive Mini Agent")
    parser.add_argument("--resume", metavar="SESSION_ID", help="恢复一个已保存的 Session")
    args = parser.parse_args(argv)

    config = load_config()
    print_environment(config)

    if args.resume:
        try:
            record = load_session(args.resume)
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
                )
            except (APIError, ConnectionError, TimeoutError) as exc:
                # 只捕获「跟外界通信」相关的失败：鉴权、限流、超时、网络不通。
                # 代码自身的 bug 不在此列，应当让它正常抛出来，方便你发现真问题。
                del messages[position:]
                print(f"\n[请求失败] {exc}")
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
