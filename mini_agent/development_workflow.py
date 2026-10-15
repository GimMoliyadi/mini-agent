"""Evidence-driven development playbooks on the existing LangGraph checkpoint.

This layer does NOT dispatch tools outside create_agent. The single existing
model/tool graph chooses operations; this module reads actual ToolMessages and
supplies an ordered next step. Evidence is invalidated after later mutations.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from langchain.tools import ToolRuntime, tool
from langgraph.types import Command

from .sandbox import redact_text
from .state import TaskState
from .result import ToolResult
from .verifier import parse_test_result

_EXPLORATION_TOOLS = frozenset({
    "list_files", "read_file", "search_text", "find_files", "inspect_project",
    "inspect_module_dependencies", "inspect_python_symbols", "inspect_code_symbols",
    "find_code_references", "inspect_dev_environment", "discover_workspace_projects",
    "inspect_fullstack_interfaces", "inspect_openapi_contract", "github_issue_context",
})
_TEST_TOOLS = frozenset({"run_command", "run_dev_task", "run_fullstack_checks"})
_REVIEW_TOOLS = frozenset({"review_code_changes"})
_MIGRATION_TOOLS = frozenset({"generate_alembic_migration"})
_BROWSER_TOOLS = frozenset({"browser_action", "browser_diagnostics"})
_DELIVERY_TOOLS = frozenset({"github_create_draft_pr"})
_NON_CODE_MUTATIONS = frozenset({"browser_screenshot"})
_TEST_TASK_NAMES = frozenset({"test", "unittest"})
_PROFILES = frozenset({"general", "web", "backend"})
_MAX_GOAL = 500

def _test_passed(name: str, args: dict[str, Any], artifact: dict[str, Any], content: str) -> bool:
    """Successful tool execution is weaker than positive test evidence."""
    if name == "run_fullstack_checks":
        detail = artifact.get("metadata")
        return isinstance(detail, dict) and detail.get("both_passed") is True
    if name == "run_dev_task":
        if args.get("task", "test") not in _TEST_TASK_NAMES:
            return False
        meta = artifact.get("metadata")
        if not isinstance(meta, dict):
            return False
        result = ToolResult(content, "success", "command_succeeded", meta)
        return parse_test_result(result)["status"] == "PASS"
    if name == "run_command":
        command = str(args.get("command", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
        command_args = args.get("args")
        if not isinstance(command_args, list):
            return False
        is_test = (command in {"pytest", "pytest.exe"}
                   or (command in {"python", "python.exe", "py", "py.exe"}
                       and command_args[:2] in (["-m", "pytest"], ["-m", "unittest"])))
        return is_test and parse_test_result(content)["status"] == "PASS"
    return False


def _browser_diagnostics_clear(content: str) -> bool:
    """A successful diagnostics read can still report live browser failures."""
    try:
        details = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(details, dict) or not isinstance(details.get("url"), str):
        return False
    if not details["url"] or not isinstance(details.get("events"), list):
        return False
    if details.get("dropped_older_events", 0) != 0:
        return False
    return not any(
        isinstance(event, dict) and event.get("kind") in {
            "javascript_exception", "console_error", "request_failed", "http_error",
        }
        for event in details["events"]
    )


def _review_clear(content: str) -> bool:
    """Code review must actually inspect a diff and report no critical findings."""
    try:
        details = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(details, dict) or details.get("truncated") is not False:
        return False
    if not isinstance(details.get("changed_files"), list) or not details["changed_files"]:
        return False
    findings = details.get("findings")
    return isinstance(findings, list) and not any(
        not isinstance(item, dict) or item.get("severity") == "error"
        for item in findings
    )



# Progressive disclosure: every tool stays registered in the original Agent
# ToolNode. This only filters which schemas the model must pay to read now.
# The shared navigation/plan/repair tools stay accessible at every stage.
_ALWAYS_TOOLS = frozenset({
    "list_files", "find_files", "read_file", "search_text",
    "inspect_capabilities", "get_task_plan", "set_task_plan",
    "update_task_step", "development_workflow_status",
    "recall_project_facts", "remember_project_fact",
    "inspect_dev_environment", "discover_workspace_projects",
    "run_command", "run_dev_task", "apply_patch", "apply_patch_batch",
})
_STAGE_TOOLS = {
    "inspect": frozenset({
        "inspect_project", "inspect_python_symbols",
        "inspect_module_dependencies", "inspect_fullstack_interfaces",
        "inspect_code_symbols", "find_code_references",
        "inspect_openapi_contract", "inspect_openapi_changes",
        "github_issue_context", "github_list_issues",
    }),
    "implement": frozenset({
        "write_file", "rename_file", "create_files_batch", "prepare_dev_environment",
        "generate_pytest_regressions", "export_fastapi_openapi",
        "inspect_openapi_contract", "generate_openapi_types",
        "generate_typed_api_client", "generate_react_query_hooks",
        "inspect_alembic_status", "generate_alembic_migration",
        "inspect_fullstack_interfaces", "inspect_module_dependencies",
        "inspect_code_symbols", "find_code_references",
        "inspect_openapi_changes", "run_fullstack_checks",
    }),
    "migration": frozenset({
        "inspect_alembic_status", "generate_alembic_migration",
        "read_file", "write_file", "run_fullstack_checks",
    }),
    "test": frozenset({
        "run_fullstack_checks", "prepare_dev_environment",
        "inspect_fullstack_interfaces", "inspect_module_dependencies",
        "generate_pytest_regressions", "create_files_batch", "write_file",
        "dev_server", "browser_action", "browser_diagnostics",
    }),
    "browser": frozenset({
        "dev_server", "browser_action", "browser_screenshot",
        "browser_diagnostics", "run_fullstack_checks",
        "inspect_fullstack_interfaces", "inspect_openapi_contract",
        "write_file", "create_files_batch",
    }),
    "review": frozenset({
        "review_code_changes", "git_worktree_review",
        "run_fullstack_checks", "write_file", "create_files_batch",
        "inspect_module_dependencies",
    }),
    "deliver": frozenset({
        "review_code_changes", "git_worktree_review", "git_commit_files",
        "github_push_branch", "github_create_draft_pr", "github_pr_status",
    }),
}


def focus_model_tools(
    tools: list[Any], stage: str | None, *,
    external_tool_names: frozenset[str] = frozenset(),
) -> list[Any]:
    """Limit built-in schemas but retain opted-in MCP/custom extension tools.

    Tool exposure never changes the ToolNode registry or permissions. External
    tools are explicitly identified by AgentApplication during registration.
    """
    if stage not in _STAGE_TOOLS:
        return list(tools)
    allowed = _ALWAYS_TOOLS | _STAGE_TOOLS[stage] | external_tool_names
    selected = [
        tool for tool in tools
        if getattr(tool, "name", None) in allowed
        or not isinstance(getattr(tool, "name", None), str)
    ]
    # Defensive fallback for custom/partial tool registries.
    return selected if selected else list(tools)


def evaluate_workflow(state: dict[str, Any]) -> dict[str, Any] | None:
    """Derive milestones from actual tool results, never from model assertions."""
    workflow = state.get("development_workflow")
    if not isinstance(workflow, dict):
        return None
    start = state.get("task_start", 0)
    messages = state.get("messages", [])[start:]
    calls: dict[str, dict[str, Any]] = {}
    observations: list[tuple[int, str, dict[str, Any], dict[str, Any], str]] = []
    last_mutation = -1
    for index, message in enumerate(messages):
        for item in getattr(message, "tool_calls", ()) or ():
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                calls[item["id"]] = item
        if not isinstance(message, ToolMessage) or message.status != "success":
            continue
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        if artifact.get("status", "success") != "success":
            continue
        name = message.name or ""
        args = calls.get(message.tool_call_id, {}).get("args", {})
        if not isinstance(args, dict):
            args = {}
        if artifact.get("mutation", False) and name not in _NON_CODE_MUTATIONS:
            last_mutation = index
        observations.append((index, name, args, artifact, str(message.content)))

    def observed(tools: frozenset[str], *, after: int = -1) -> bool:
        return any(index > after and name in tools for index, name, _, _, _ in observations)

    explored = observed(_EXPLORATION_TOOLS)
    modified = last_mutation >= 0
    tested = modified and any(
        index > last_mutation and _test_passed(name, args, artifact, content)
        for index, name, args, artifact, content in observations
        if name in _TEST_TOOLS
    )
    reviewed = modified and any(
        index > last_mutation and name == "review_code_changes"
        and _review_clear(content)
        for index, name, _, _, content in observations
    )
    migration = observed(_MIGRATION_TOOLS)
    browser_open_indexes = [
        index for index, name, args, _, _ in observations
        if modified and index > last_mutation
        and name == "browser_action" and args.get("action") == "open"
    ]
    browser_open = bool(browser_open_indexes)
    browser_diagnostics = bool(browser_open_indexes) and any(
        index > browser_open_indexes[-1] and name == "browser_diagnostics"
        and _browser_diagnostics_clear(content)
        for index, name, _, _, content in observations
    )
    browser = browser_open and browser_diagnostics
    delivered = observed(_DELIVERY_TOOLS, after=last_mutation)
    stages = [
        {
            "id": "inspect", "title": "定位项目结构和相关源码",
            "done": explored,
            "suggested_tools": ["discover_workspace_projects", "inspect_code_symbols", "read_file"],
        },
        {
            "id": "implement", "title": "完成源码或测试文件的实际修改",
            "done": modified,
            "suggested_tools": ["apply_patch_batch", "create_files_batch", "write_file"],
        },
    ]
    if workflow.get("require_migration"):
        stages.append({
            "id": "migration", "title": "准备数据库迁移脚本",
            "done": migration,
            "suggested_tools": ["inspect_alembic_status", "generate_alembic_migration"],
        })
    stages.append({
        "id": "test", "title": "在最新文件修改后运行检查",
        "done": tested,
        "suggested_tools": ["run_dev_task", "run_fullstack_checks", "run_command"],
    })
    if workflow.get("require_browser"):
        stages.append({
            "id": "browser", "title": "在最新修改后打开网页并检查浏览器错误",
            "done": browser,
            "suggested_tools": ["dev_server", "browser_action", "browser_diagnostics"],
            "observations": {"opened": browser_open, "diagnostics": browser_diagnostics},
        })
    stages.append({
        "id": "review", "title": "在最新文件修改后检查 Git diff 与代码",
        "done": reviewed,
        "suggested_tools": ["review_code_changes"],
    })
    if workflow.get("deliver_pr"):
        stages.append({
            "id": "deliver", "title": "提交并创建 Draft PR",
            "done": delivered,
            "suggested_tools": ["git_worktree_review", "git_commit_files",
                                "github_push_branch", "github_create_draft_pr"],
        })
    next_stage = next((stage for stage in stages if not stage["done"]), None)
    evidence_issue = ""
    if next_stage is not None:
        pending = next_stage["id"]
        if pending == "test" and observed(_TEST_TOOLS, after=last_mutation):
            evidence_issue = (
                "已执行测试工具但缺少非零测试数与明确 PASS 结果，"
                "请检查测试摘要、退出码及前后端检查详情。"
            )
        elif pending == "browser" and observed(_BROWSER_TOOLS, after=last_mutation):
            evidence_issue = (
                "浏览器验收尚缺少打开目标页面之后的无错误诊断；"
                "请核对 JavaScript、HTTP、请求失败和诊断历史是否截断。"
            )
        elif pending == "review" and observed(_REVIEW_TOOLS, after=last_mutation):
            evidence_issue = (
                "代码审查已执行，但结果包含错误级发现、输出截断或缺少可审查的改动；"
                "请处理审查发现并核对 Git diff 与文件范围。"
            )
    return {
        "profile": workflow["profile"],
        "goal": redact_text(workflow["goal"]),
        "stages": stages,
        "completed": sum(stage["done"] for stage in stages),
        "total": len(stages),
        "complete": next_stage is None,
        "next_stage": next_stage["id"] if next_stage else None,
        "evidence_issue": evidence_issue,
        "suggested_tools": next_stage["suggested_tools"] if next_stage else [],
        "last_mutation_observed": modified,
        "note": "Only successful ToolMessages count. Test, browser and review evidence "
                "is invalidated after later file changes. Suggested tools are not automatic "
                "authorizations. Read the actual tool results, not only this checklist.",
    }


@tool
def start_development_workflow(
    goal: str,
    profile: Literal["general", "web", "backend"],
    runtime: ToolRuntime[Any, TaskState],
    require_browser: bool = False,
    require_migration: bool = False,
    deliver_pr: bool = False,
) -> Command:
    """Activate an evidence-driven coding workflow in the current checkpoint.

    Pick web for frontend/backend websites, backend for server/database tasks,
    general for library/code tasks. Explicitly request browser, migrations or
    PR delivery only when relevant. Does not run tools or bypass approvals.
    """
    if not isinstance(goal, str) or not 5 <= len(goal.strip()) <= _MAX_GOAL:
        raise ValueError("开发目标需要 5–500 个字符")
    if profile not in _PROFILES:
        raise ValueError("未知开发工作流类型")
    if any(not isinstance(value, bool) for value in
           (require_browser, require_migration, deliver_pr)):
        raise ValueError("开发工作流开关必须是布尔值")
    if runtime.state.get("development_workflow"):
        raise ValueError("当前任务已有工作流；请先完成或继续现有工作流")
    if not runtime.tool_call_id:
        raise ValueError("启动工作流需要 tool_call_id")
    definition: dict[str, Any] = {
        "goal": goal.strip(),
        "profile": profile,
        "require_browser": require_browser,
        "require_migration": require_migration,
        "deliver_pr": deliver_pr,
    }
    state = {**dict(runtime.state), "development_workflow": definition}
    result = evaluate_workflow(state)
    return Command(update={
        "development_workflow": definition,
        "messages": [ToolMessage(
            name="start_development_workflow",
            tool_call_id=runtime.tool_call_id,
            content=json.dumps(result, ensure_ascii=False),
        )],
    })


@tool
def development_workflow_status(runtime: ToolRuntime[Any, TaskState]) -> str:
    """Read verified milestones and the next suggested tools for this coding task.

    Milestones are calculated from successful tool results in this task's
    checkpoint and invalidated when a later source file mutation occurs.
    """
    return json.dumps(evaluate_workflow(dict(runtime.state)), ensure_ascii=False)


def build_development_workflow_tools() -> list[Any]:
    start_development_workflow.metadata = {"risk": "read", "mutation": False}
    development_workflow_status.metadata = {"risk": "read", "mutation": False}
    return [start_development_workflow, development_workflow_status]


class DevelopmentWorkflowMiddleware(AgentMiddleware):
    """Provide live next-stage guidance to the *existing* model/tool loop."""

    state_schema = TaskState

    def __init__(self, *, external_tool_names: frozenset[str] = frozenset()) -> None:
        self.external_tool_names = external_tool_names

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        evaluation = evaluate_workflow(dict(request.state))
        if evaluation is None or evaluation["complete"]:
            return handler(request)
        next_stage = evaluation["next_stage"]
        tools = ", ".join(evaluation["suggested_tools"])
        guidance = (
            "\n当前启用开发工作流，目标：" + evaluation["goal"] +
            f"。阶段 {evaluation['completed']}/{evaluation['total']} 已有真实工具证据。"
            f"下一项尚未完成：{next_stage}。可选择工具：{tools}。"
            "请根据当前项目与实际结果选择工具；批准和执行规则不变。"
            "代码修改后必须重新进行测试与审查，不能依据旧结果宣布完成。"
            "如果依赖或工具不可用，应如实说明受阻，不能谎报完成。"
            + (f" 尚缺验收证据：{evaluation['evidence_issue']}" if evaluation["evidence_issue"] else "")
        )
        system = request.system_message
        content = system.content if system is not None else ""
        if isinstance(content, str):
            content = content + guidance
        else:
            content = [*content, {"type": "text", "text": guidance}]
        selected_tools = focus_model_tools(
            request.tools, next_stage, external_tool_names=self.external_tool_names,
        )
        return handler(request.override(
            system_message=SystemMessage(content=content),
            tools=selected_tools,
        ))
