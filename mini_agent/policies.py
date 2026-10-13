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
from .multi_file import patch_batch_precondition, preview_patch_batch, prepare_patch_batch
from .scaffolding import creation_precondition, preview_file_creations, prepare_file_creations


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
        exclusive = {"git_commit_files", "github_push_branch", "github_create_draft_pr", "apply_patch_batch", "create_files_batch", "generate_openapi_types"}
        if any(call["name"] in exclusive for call in getattr(message, "tool_calls", [])) and len(getattr(message, "tool_calls", [])) != 1:
            raise ValueError("Git 提交或 GitHub 远程写入必须单独调用，避免并行操作冲突")
        if len(plan_updates) > 1:
            raise ValueError("每批最多一次计划更新，避免并行写入同一个 Checkpoint 状态")
        preconditions = {}
        for call in getattr(message, "tool_calls", []):
            if call["name"] in {"write_file", "apply_patch", "rename_file", "apply_patch_batch", "create_files_batch", "generate_openapi_types"}:
                try:
                    preconditions[call["id"]] = (
                        patch_batch_precondition(call["args"]["patches"], self.settings.workspace)
                        if call["name"] == "apply_patch_batch"
                        else creation_precondition(call["args"]["files"], self.settings.workspace)
                        if call["name"] == "create_files_batch"
                        else capture_precondition(
                            "write_file", {"path": call["args"].get("output_path", "frontend/src/openapi.d.ts"), "content": ""},
                            self.settings.workspace,
                        )
                        if call["name"] == "generate_openapi_types"
                        else capture_precondition(call["name"], call["args"], self.settings.workspace)
                    )
                except (OSError, ValueError, KeyError):
                    # Execution validates again and produces a structured failure.
                    pass
        return {"preconditions": preconditions}

    def approval_description(self, call, state, runtime):
        if call["name"] in {"write_file", "apply_patch", "rename_file", "apply_patch_batch", "create_files_batch", "generate_openapi_types"}:
            try:
                return (
                    preview_patch_batch(call["args"]["patches"], self.settings.workspace)
                    if call["name"] == "apply_patch_batch"
                    else preview_file_creations(call["args"]["files"], self.settings.workspace)
                    if call["name"] == "create_files_batch"
                    else redact_text(
                        f"使用本地 openapi-typescript 将 {call['args'].get('schema_path', 'backend/openapi.json')} "
                        f"生成到 {call['args'].get('output_path', 'frontend/src/openapi.d.ts')} "
                        f"(前端项目 {call['args'].get('frontend_path', 'frontend')})"
                    )
                    if call["name"] == "generate_openapi_types"
                    else preview_tool_change(call["name"], call["args"], self.settings.workspace)
                )
            except (OSError, ValueError, KeyError) as exc:
                return redact_text(f"操作预览失败：{exc}")
        if call["name"] == "git_commit_files":
            return redact_text(f"创建本地 Git 提交：{call['args'].get('message', '')}；文件：{call['args'].get('paths', [])}；不会推送远程")
        if call["name"] == "github_push_branch":
            return "向配置的 GitHub origin 推送当前非保护分支（不强推、不创建 PR）"
        if call["name"] == "github_create_draft_pr":
            return redact_text(
                f"在配置的 GitHub 仓库创建 Draft PR：标题 {call['args'].get('title', '')}；"
                f"目标分支 {call['args'].get('base_branch', '')}。不会自动合并"
            )
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
        name = request.tool_call["name"]
        if name == "apply_patch_batch":
            before, _ = prepare_patch_batch(request.tool_call["args"]["patches"], self.settings.workspace)
            if contract_data:
                contract = CodingTaskContract.from_dict(contract_data)
                for path in before:
                    relative = path.relative_to(self.settings.workspace).as_posix()
                    if relative not in contract.allowed_paths:
                        raise PermissionError("跨文件补丁存在 Coding Contract.allowed_paths 之外的路径")
        elif name == "create_files_batch":
            created = prepare_file_creations(request.tool_call["args"]["files"], self.settings.workspace)
            if contract_data:
                contract = CodingTaskContract.from_dict(contract_data)
                for path in created:
                    if path.relative_to(self.settings.workspace).as_posix() not in contract.allowed_paths:
                        raise PermissionError("批量创建包含 Coding Contract.allowed_paths 之外的路径")
        elif contract_data and metadata.get("mutation"):
            contract = CodingTaskContract.from_dict(contract_data)
            paths = metadata.get("mutation_paths", ())
            if not paths:
                raise PermissionError("该变更工具没有声明修改路径，不能用于 Coding Contract")
            for argument in paths:
                chosen = request.tool_call["args"].get(argument)
                if name == "generate_openapi_types" and argument == "output_path":
                    chosen = chosen or "frontend/src/openapi.d.ts"
                path = resolve_path(chosen, self.settings.workspace)
                if path.relative_to(self.settings.workspace).as_posix() not in contract.allowed_paths:
                    raise PermissionError("修改路径不在 Coding Contract.allowed_paths 中")
        before = request.state.get("preconditions", {}).get(request.tool_call["id"])
        if name == "apply_patch_batch":
            if before is None or before != patch_batch_precondition(
                request.tool_call["args"]["patches"], self.settings.workspace,
            ):
                raise PermissionError("跨文件补丁审批前后文件已变化，请重新预览和批准")
        elif name == "create_files_batch":
            if before is None or before != creation_precondition(
                request.tool_call["args"]["files"], self.settings.workspace,
            ):
                raise PermissionError("批量创建审批后目标已变化，请重新预览和批准")
        elif name == "generate_openapi_types":
            validate_precondition(
                "write_file",
                {"path": request.tool_call["args"].get("output_path", "frontend/src/openapi.d.ts"),
                 "content": ""},
                self.settings.workspace, before,
            )
        elif before is not None:
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
