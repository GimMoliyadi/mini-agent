"""Approval policies and tool preflight checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import runtime
from .result import ToolResult


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
    from . import runtime

    input_stream = runtime.sys.stdin if stdin is None else stdin
    output_stream = runtime.sys.stdout if stdout is None else stdout
    return _is_terminal(input_stream) and _is_terminal(output_stream)


def ask_for_approval(tool_name: str, arguments: dict, operation: str) -> bool:
    from . import runtime

    if not runtime.interactive_approval_available():
        raise runtime.ApprovalUnavailableError(runtime.NON_INTERACTIVE_APPROVAL_ERROR)
    print("\nAgent 请求执行有副作用的工具：")
    print(f"工具：{tool_name}")
    print(f"操作：{operation}")
    print(
        runtime.redact_text(
            runtime.preview_tool_change(tool_name, arguments, runtime.WORKSPACE_DIR)
        )
    )
    if tool_name == "run_command":
        print("测试代码会以当前用户权限运行，仅批准你信任的项目。")
    try:
        print("Ctrl+C 取消整个任务。")
        answer = input("是否允许？[y/N] ").strip().lower()
    except EOFError as exc:
        raise runtime.ApprovalUnavailableError(
            runtime.NON_INTERACTIVE_APPROVAL_ERROR
        ) from exc
    return answer in {"y", "yes"}


def approval_callback_for_mode(mode: str, input_func=None) -> runtime.ApprovalCallback:
    """Build the small approval policy selected by the caller/environment."""
    from . import runtime

    selected_mode = mode.strip().upper()
    if selected_mode == "ALLOW":
        return runtime.always_allow
    if selected_mode == "DENY":
        return runtime.always_deny
    if selected_mode == "ASK":
        if input_func is None:
            return runtime.ask_for_approval

        def ask_with_injected_input(
            tool_name: str, arguments: dict, operation: str
        ) -> bool:
            print("\nAgent 请求执行有副作用的工具：")
            print(f"工具：{tool_name}")
            if "path" in arguments:
                print(f"文件：{arguments.get('path', '?')}")
            if tool_name == "rename_file":
                print(f"从：{arguments.get('source', '?')}")
                print(f"到：{arguments.get('destination', '?')}")
            print(f"操作：{operation}")
            answer = input_func("是否允许？[y/N] ").strip().lower()
            return answer in {"y", "yes"}

        return ask_with_injected_input
    raise ValueError(f"审批模式必须是 ASK, ALLOW, DENY 之一，当前是：{mode!r}")


def approval_needs_interactive_input(mode: str) -> bool:
    """Only ASK blocks a headless process; ALLOW and DENY decide by themselves."""
    from . import runtime

    return mode.strip().upper() == "ASK" and (
        not runtime.interactive_approval_available()
    )


def check_tool_permission(
    call, approval_callback: runtime.ApprovalCallback
) -> tuple[bool, str | None]:
    from . import runtime

    tool_name = call.function.name
    definition = runtime.TOOL_REGISTRY.get(tool_name)
    if definition is None:
        return (
            False,
            f"{runtime.TOOL_FAILURE_PREFIX} 没有名为 {tool_name} 的工具，无法执行",
        )
    if (
        definition.tool_kind is runtime.ToolKind.CONTROL_FLOW
        or definition.risk_level is runtime.RiskLevel.READ_ONLY
    ):
        return (True, None)
    try:
        arguments = runtime.parse_tool_arguments(call)
        required = (
            definition.schema.get("function", {})
            .get("parameters", {})
            .get("required", [])
        )
        missing = set(required).difference(arguments)
        if missing:
            raise ValueError("缺少工具参数：" + ", ".join(sorted(missing)))
        operation = (
            "RENAME" if tool_name == "rename_file" else definition.risk_level.value
        )
        for argument_name in definition.workspace_arguments:
            value = arguments.get(argument_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"{argument_name} 必须是字符串")
            target = runtime.resolve_inside_workspace(value)
            if argument_name == definition.operation_path_argument:
                operation = "OVERWRITE" if target.is_file() else "CREATE"
        if definition.preflight is not None:
            definition.preflight(arguments)
        before = runtime.capture_precondition(
            tool_name, arguments, runtime.WORKSPACE_DIR
        )
        if not approval_callback(tool_name, arguments, operation):
            return (
                False,
                ToolResult(
                    f"{runtime.APPROVAL_DENIED_PREFIX}\n{tool_name} 未执行。\n文件没有被修改。",
                    "policy_rejected",
                    "approval_denied",
                ),
            )
        budget = runtime.get_current_budget()
        if budget is not None:
            budget.check()
        runtime.validate_precondition(
            tool_name, arguments, runtime.WORKSPACE_DIR, before
        )
        return (True, None)
    except (OSError, TypeError, ValueError) as exc:
        return (
            False,
            ToolResult(
                f"{runtime.TOOL_FAILURE_PREFIX} {type(exc).__name__}：{runtime.redact_text(str(exc))}",
                "policy_rejected",
                type(exc).__name__,
            ),
        )
