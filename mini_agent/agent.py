"""Bounded model/tool loop and recovery continuation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from .config import MAX_AGENT_STEPS

if TYPE_CHECKING:
    from . import runtime
from dataclasses import dataclass, field


@dataclass
class AgentLoopState:
    client: runtime.OpenAI
    model: str
    messages: list[dict]
    reply: runtime.ModelReply
    executed: set
    approval_callback: runtime.ApprovalCallback
    tools: runtime.ToolRunContext
    trace: runtime.CodingTaskTrace | None
    checkpoint: runtime.Callable[[], None] | None
    limit: int = MAX_AGENT_STEPS
    grace: str | None = None
    workspace_snapshot: dict[str, str] = field(default_factory=dict)


def _save_loop_checkpoint(loop: AgentLoopState) -> None:
    if loop.trace is not None:
        loop.trace.set_task_state(loop.tools.task_state)
        if loop.tools.recovery is not None:
            loop.trace.recovery_state = vars(loop.tools.recovery).copy()
    if loop.checkpoint is not None:
        loop.checkpoint()


def _loop_outcome(loop: AgentLoopState, status: str) -> str:
    from . import runtime

    if loop.trace is not None:
        loop.trace.outcome = status
        if status == "limit_reached":
            loop.trace.mark_max_steps()
    if loop.tools.task_state is not None and status == "limit_reached":
        loop.tools.task_state.status = runtime.TaskStatus.LIMIT_REACHED
    _save_loop_checkpoint(loop)
    return status


def _process_model_turn(loop: AgentLoopState, step: int):
    from . import runtime

    reply = loop.reply
    if loop.trace is not None:
        loop.trace.record_model_turn(step, reply)
    refusal = getattr(reply.message, "refusal", None)
    if refusal:
        loop.messages.append({"role": "assistant", "content": refusal})
        print("Runtime > 模型拒绝了此次请求，任务未完成。")
        return ("incomplete", None)
    if reply.finish_reason not in {"stop", "tool_calls"} or (
        reply.finish_reason == "tool_calls" and (not reply.message.tool_calls)
    ):
        text = reply.message.content or "模型没有返回完整文本。"
        messages = loop.messages
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": f"Runtime: 上一响应结束原因是 {reply.finish_reason!r}，任务未完整完成，其中工具请求未执行。",
            }
        )
        print(f"Runtime > 模型响应未完整结束（{reply.finish_reason!r}），任务未完成。")
        return ("incomplete", None)
    if not reply.message.tool_calls:
        runtime.finalize(loop.messages, reply.message)
        if loop.tools.contract is None:
            return ("completed" if reply.message.content else "incomplete", None)
        loop.messages.append(
            {"role": "user", "content": runtime.CODING_FINISH_PROTOCOL_NOTICE}
        )
        print(f"Runtime > {runtime.CODING_FINISH_PROTOCOL_NOTICE}")
        return (None, None)
    if loop.tools.contract is not None:
        current_snapshot = runtime.snapshot_workspace(runtime.WORKSPACE_DIR)
        if current_snapshot != loop.workspace_snapshot:
            loop.executed.clear()
    last_tool = runtime.run_tool_round(
        loop.messages,
        reply.message,
        loop.executed,
        loop.approval_callback,
        trace=loop.trace,
        turn=step,
        required_test=loop.tools.required_test,
        contract=loop.tools.contract,
        task_state=loop.tools.task_state,
        recovery=loop.tools.recovery,
        session_active=loop.tools.session_active,
        verifier_enabled=loop.tools.verifier_enabled,
    )
    if loop.tools.contract is not None:
        loop.workspace_snapshot = runtime.snapshot_workspace(runtime.WORKSPACE_DIR)
    state = loop.tools.task_state
    return (
        "completed"
        if state is not None and state.status is runtime.TaskStatus.FINISHED
        else None,
        last_tool,
    )


def _advance_loop_limits(loop: AgentLoopState, step: int, last_tool) -> bool:
    from . import runtime

    if (
        step == runtime.MAX_AGENT_STEPS
        and loop.grace is None
        and (loop.tools.contract is not None)
    ):
        loop.grace, loop.limit = runtime.recovery_grace_limit(
            step, last_tool, loop.tools.required_test, loop.tools.task_state
        )
        if loop.grace == "FAIL":
            loop.tools.recovery = runtime.Recovery()
        if loop.trace is not None:
            loop.trace.recovery_grace = loop.grace
            loop.trace.final_model_call_limit = loop.limit
    recovery = loop.tools.recovery
    exhausted = (
        recovery.end_round(step)
        if recovery is not None and step > runtime.MAX_AGENT_STEPS
        else False
    )
    return exhausted or step >= loop.limit


def _drive_agent_loop(loop: AgentLoopState) -> str:
    from . import runtime

    budget = runtime.get_current_budget()
    assert budget is not None
    for step in range(1, runtime.HARD_CEILING + 1):
        budget.check()
        status, last_tool = _process_model_turn(loop, step)
        if status is not None:
            if loop.tools.recovery is not None:
                loop.tools.recovery.end_round(step)
            return _loop_outcome(loop, status)
        if _advance_loop_limits(loop, step, last_tool):
            print(
                f"Runtime > 当前任务达到执行额度（模型轮数上限 {loop.limit}），尚未完成。"
            )
            return _loop_outcome(loop, "limit_reached")
        _save_loop_checkpoint(loop)
        loop.reply = runtime.ask(
            loop.client, loop.model, runtime.build_model_context(loop.messages)
        )
        runtime.log_reply(step + 1, loop.reply)
    return _loop_outcome(loop, "limit_reached")


def run_agent_loop(
    client: runtime.OpenAI,
    model: str,
    messages: list[dict],
    first_reply: runtime.ModelReply,
    executed: set[tuple[str, str]],
    approval_callback: runtime.ApprovalCallback,
    trace: runtime.CodingTaskTrace | None = None,
    required_test: runtime.RequiredTest | None = None,
    contract: runtime.CodingTaskContract | None = None,
    task_state: runtime.TaskState | None = None,
    session_active: bool = False,
    verifier_enabled: bool = False,
    checkpoint: runtime.Callable[[], None] | None = None,
) -> str:
    from . import runtime

    if contract is not None:
        if task_state is None:
            task_state = runtime.TaskState(
                initial_snapshot=runtime.snapshot_workspace(runtime.WORKSPACE_DIR)
            )
        if required_test is None:
            command = contract.test_command
            required_test = (command.command, command.args, command.cwd)
    context = runtime.ToolRunContext(
        contract, task_state, required_test, None, session_active, verifier_enabled
    )
    loop = AgentLoopState(
        client,
        model,
        messages,
        first_reply,
        executed,
        approval_callback,
        context,
        trace,
        checkpoint,
    )
    active_budget = runtime.get_current_budget() or runtime.RunBudget()
    with runtime.budget_context(active_budget):
        try:
            return _drive_agent_loop(loop)
        except KeyboardInterrupt:
            if task_state is not None:
                task_state.status = runtime.TaskStatus.CANCELLED
            if trace is not None:
                trace.outcome = "cancelled"
                trace.set_task_state(task_state)
            raise
        except runtime.BudgetExceeded:
            if task_state is not None:
                task_state.status = runtime.TaskStatus.LIMIT_REACHED
            if trace is not None:
                trace.outcome = "limit_reached"
                trace.mark_max_steps()
                trace.set_task_state(task_state)
            raise
        except Exception as exc:
            runtime._record_runtime_error(task_state, trace, exc)
            raise
