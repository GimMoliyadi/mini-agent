"""Execution boundaries around standard framework tools and human interrupts."""

from time import perf_counter
from contextlib import nullcontext
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from .sandbox import (
    capture_precondition, journal_context, preview_tool_change, redact_text,
    resolve_path, validate_precondition,
)
from .settings import Settings
from .state import TaskState
from .verifier import CodingTaskContract, snapshot_workspace
from .runtime_guards import get_current_budget


class BoundaryMiddleware(AgentMiddleware):
    state_schema = TaskState

    def __init__(self, settings: Settings, events):
        self.settings = settings
        self.events = events

    def after_model(self, state, runtime):
        message = state["messages"][-1]
        finish_reason = message.response_metadata.get("finish_reason")
        if finish_reason not in {None, "stop", "tool_calls"}:
            raise ValueError(f"模型响应未完整结束：{finish_reason}")
        budget = get_current_budget()
        if budget is not None:
            budget.validate_batch_size(len(getattr(message, "tool_calls", [])))
        plan_updates = [call for call in getattr(message, "tool_calls", [])
                        if call["name"] in {"set_task_plan", "update_task_step"}]
        if any(call["name"] == "git_commit_files" for call in getattr(message, "tool_calls", [])) and len(getattr(message, "tool_calls", [])) != 1:
            raise ValueError("Git 提交必须单独一个工具调用，避免与并发文件变更冲突")
        if len(plan_updates) > 1:
            raise ValueError("每批最多一次计划更新，避免并行写入同一个 Checkpoint 状态")
        preconditions = {}
        for call in getattr(message, "tool_calls", []):
            if call["name"] in {"write_file", "apply_patch", "rename_file"}:
                try:
                    preconditions[call["id"]] = capture_precondition(
                        call["name"], call["args"], self.settings.workspace,
                    )
                except (OSError, ValueError, KeyError):
                    # Execution validates again and produces a structured failure.
                    pass
        return {"preconditions": preconditions}

    def approval_description(self, call, state, runtime):
        if call["name"] in {"write_file", "apply_patch", "rename_file"}:
            try:
                return preview_tool_change(call["name"], call["args"], self.settings.workspace)
            except (OSError, ValueError, KeyError) as exc:
                return redact_text(f"操作预览失败：{exc}")
        if call["name"] == "git_commit_files":
            return redact_text(f"创建本地 Git 提交：{call['args'].get('message', '')}；文件：{call['args'].get('paths', [])}；不会推送远程")
        return f"执行工具 {call['name']}（工作区内受控命令或扩展工具）"

    def _validate(self, request):
        if request.state.get("workspace") != str(self.settings.workspace):
            raise PermissionError("会话与当前工作目录不匹配")
        metadata = request.tool.metadata or {} if request.tool else {}
        if self.settings.approval_mode == "DENY" and metadata.get("risk", "execute") != "read":
            raise PermissionError("副作用操作已由 DENY 策略拒绝")
        for argument in metadata.get("workspace_arguments", ()):
            path = request.tool_call["args"].get(argument)
            if path is not None:
                resolve_path(path, self.settings.workspace)
        contract_data = request.state.get("contract")
        if contract_data and metadata.get("mutation"):
            contract = CodingTaskContract.from_dict(contract_data)
            paths = metadata.get("mutation_paths", ())
            if not paths:
                raise PermissionError("该变更工具没有声明修改路径，不能用于 Coding Contract")
            for argument in paths:
                path = resolve_path(request.tool_call["args"][argument], self.settings.workspace)
                if path.relative_to(self.settings.workspace).as_posix() not in contract.allowed_paths:
                    raise PermissionError("修改路径不在 Coding Contract.allowed_paths 中")
        name = request.tool_call["name"]
        before = request.state.get("preconditions", {}).get(request.tool_call["id"])
        if before is not None:
            validate_precondition(name, request.tool_call["args"], self.settings.workspace, before)

    def wrap_tool_call(self, request, handler):
        started = perf_counter()
        name = request.tool_call["name"]
        metadata = request.tool.metadata or {} if request.tool else {}
        try:
            budget = get_current_budget()
            if budget is not None:
                budget.before_tool_call()
            self._validate(request)
            journal = journal_context(
                request.state["task_id"], self.settings.workspace, self.settings.state_dir / ".mini-agent" / "journals", resume=True,
            ) if metadata.get("mutation") else nullcontext()
            with journal:
                result = handler(request)
            if isinstance(result, ToolMessage):
                artifact = dict(result.artifact or {})
                if artifact.get("status", "success") != "success":
                    result.status = "error"
                artifact["mutation"] = bool(metadata.get("mutation"))
                if name == "run_command":
                    artifact["command"] = {
                        "command": request.tool_call["args"].get("command"),
                        "args": request.tool_call["args"].get("args", []),
                        "cwd": request.tool_call["args"].get("cwd", "."),
                    }
                    if request.state.get("contract"):
                        artifact["snapshot"] = snapshot_workspace(self.settings.workspace)
                result.artifact = artifact
        except (OSError, ValueError, KeyError) as exc:
            result = ToolMessage(
                content=redact_text(f"工具执行失败：{type(exc).__name__}: {exc}"),
                name=name, tool_call_id=request.tool_call["id"], status="error",
                artifact={"status": "policy_rejected" if isinstance(exc, PermissionError) else "tool_error"},
            )
        self.events.emit("tool", task_id=request.state.get("task_id"), name=name, status=getattr(result, "status", "success"),
                         duration=round(perf_counter() - started, 4))
        return result
