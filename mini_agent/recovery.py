"""Bounded, evidence-driven automatic follow-up for unfinished coding tasks.

This module makes decisions from existing LangGraph checkpoint facts; it does
not execute tools, replay interrupted calls or claim that an attempt succeeded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage

from .planning import plan_summary
from .verification import unresolved_tool_failures


@dataclass(frozen=True)
class RecoveryDecision:
    retry: bool
    reason: str
    category: str = "none"


def decide_recovery(state: dict[str, Any], pending_nodes: tuple[str, ...]) -> RecoveryDecision:
    """Only retry independently resumable work with actionable failure evidence."""
    if pending_nodes:
        return RecoveryDecision(False, "pending_checkpoint_nodes")
    if state.get("status") != "incomplete":
        return RecoveryDecision(False, "not_incomplete")
    plan = plan_summary(state.get("plan"))
    # A blocked step often needs credentials, a decision or an environment fix.
    # Never silently turn an explicitly blocked plan back into pending.
    if plan and any(step["status"] == "blocked" for step in plan["steps"]):
        return RecoveryDecision(False, "blocked_step")
    failures = unresolved_tool_failures(state)
    errors = [
        message for message in state.get("messages", [])[state.get("task_start", 0):]
        if isinstance(message, ToolMessage)
        and message.status == "error"
        and (message.name or "tool") in failures
    ]
    if any((item.artifact or {}).get("status") == "policy_rejected" for item in errors):
        return RecoveryDecision(False, "policy_rejected")
    if errors:
        last = errors[-1]
        artifact = last.artifact or {}
        if artifact.get("status") in {"duplicate_blocked", "policy_rejected"}:
            return RecoveryDecision(False, "tool_not_retryable")
        if last.name == "run_command":
            return RecoveryDecision(True, "failed_command", "command_failure")
        if last.name in {"apply_patch", "write_file", "rename_file"}:
            return RecoveryDecision(True, "failed_edit", "edit_failure")
        return RecoveryDecision(True, "failed_tool", "tool_failure")
    if plan and plan["completed"] < plan["total"]:
        return RecoveryDecision(True, "unfinished_plan", "plan_progress")
    # Without an unfinished plan, error evidence or a pending contract
    # check, repeated requests cannot be distinguished from wasted quota.
    if state.get("contract") and (state.get("verification") or {}).get("accepted") is False:
        return RecoveryDecision(True, "contract_unverified", "verification")
    return RecoveryDecision(False, "no_actionable_failure")


def work_signature(state: dict[str, Any]) -> tuple[str, int, int]:
    """Detect stalled attempts without copying file contents or tool arguments."""
    plan = state.get("plan")
    plan_state = json.dumps(plan, sort_keys=True, ensure_ascii=False, default=str)
    useful_tools = [
        message for message in state.get("messages", [])[state.get("task_start", 0):]
        if isinstance(message, ToolMessage) and message.status == "success"
        and (message.artifact or {}).get("mutation")
    ]
    verified_tests = [
        message for message in state.get("messages", [])[state.get("task_start", 0):]
        if isinstance(message, ToolMessage) and message.status == "success"
        and message.name == "run_command"
    ]
    return plan_state, len(useful_tools), len(verified_tests)


RECOVERY_PROMPTS = {
    "command_failure": "上一轮执行命令或测试失败。请根据已有工具结果定位具体问题、修复代码并重新执行必要验证。",
    "edit_failure": "上一轮文件修改失败。请重新核对实际文件内容与修改前提，调整补丁后再尝试。",
    "tool_failure": "上一轮工具调用失败。请判断是否能安全恢复；如涉及外部副作用或权限问题，停止并报告。",
    "plan_progress": "开发计划还没有完成。请核对已经完成的步骤，继续下一项可执行工作，不要把计划说明当作完成证据。",
    "verification": "验收尚未通过。请查看原任务契约、实际代码变更和指定测试的验证结果，修复缺口。",
}


def recovery_message(category: str, failed_tools: list[str] | None = None) -> str:
    """Short stable guidance; model still sees its own full checkpoint history."""
    guidance = RECOVERY_PROMPTS[category]
    failures = (
        "当前未解决的失败工具（诊断数据）：" + json.dumps(failed_tools, ensure_ascii=False)
        + "。请核对这些失败的前提和参数；其他成功调用不能证明这些失败已解决。"
        if failed_tools else ""
    )
    return (
        "自动恢复回合（仍是同一个任务和工作区）。" + guidance
        + failures
        + "先检查工具执行结果，避免重复已经成功的文件/外部副作用。"
        + "遵守原审批、路径、测试与资源限制；无法继续时明确列出阻碍，不要声称完成。"
    )
