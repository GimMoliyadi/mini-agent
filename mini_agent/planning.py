"""Checkpoint-backed planning tools for multi-step coding tasks.

Plans live in LangGraph task state: there is no second mutable JSON plan store.
Model-visible tools may update one plan per tool batch. Files and external
systems remain governed by the existing workspace/approval boundaries.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from .sandbox import redact_text
from .state import TaskState

MAX_STEPS = 12
MAX_TITLE = 140
MAX_NOTE = 400


def _current(runtime: ToolRuntime[TaskState]) -> dict[str, Any] | None:
    """Return a detached view; never mutate checkpoint state in place."""
    value = runtime.state.get("plan")
    return dict(value) if isinstance(value, dict) else None


def plan_summary(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redact output while retaining structured progress and all step states."""
    if plan is None:
        return None
    steps = [
        {
            "id": item["id"],
            "title": redact_text(item["title"]),
            "status": item["status"],
            "note": redact_text(item.get("note", "")),
        }
        for item in plan["steps"]
    ]
    return {
        "revision": plan["revision"],
        "reason": redact_text(plan.get("reason", "")),
        "steps": steps,
        "completed": sum(item["status"] == "completed" for item in steps),
        "total": len(steps),
    }


def _reply(name: str, runtime: ToolRuntime[TaskState], plan: dict[str, Any]) -> Command:
    """A Command must include its matching ToolMessage for graph invariants."""
    return Command(update={
        "plan": plan,
        "messages": [ToolMessage(
            content=json.dumps(plan_summary(plan), ensure_ascii=False),
            name=name,
            tool_call_id=runtime.tool_call_id,
        )],
    })


@tool
def set_task_plan(steps: list[str], runtime: ToolRuntime[TaskState], reason: str = "") -> Command:
    """Create/revise a bounded ordered development plan in the current task checkpoint.

    Use for substantial multi-step work, not trivial requests. When revising an
    existing plan, explain why in reason. Make one plan mutation per tool batch.
    """
    if not 2 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"计划需要 2 到 {MAX_STEPS} 个步骤")
    names = []
    for title in steps:
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            raise ValueError(f"步骤描述需为 1 到 {MAX_TITLE} 个字符")
        names.append(title.strip())
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("计划步骤不可重复")
    if not isinstance(reason, str) or len(reason) > MAX_NOTE:
        raise ValueError("规划原因过长")
    old = _current(runtime)
    if old and not reason.strip():
        raise ValueError("修订已有计划时必须说明原因")
    plan = {
        "revision": old["revision"] + 1 if old else 1,
        "reason": reason.strip(),
        "steps": [
            {"id": number, "title": title, "status": "pending", "note": ""}
            for number, title in enumerate(names, 1)
        ],
    }
    return _reply("set_task_plan", runtime, plan)


@tool
def update_task_step(
    step_id: int,
    status: Literal["pending", "in_progress", "completed", "blocked"],
    runtime: ToolRuntime[TaskState],
    note: str = "",
) -> Command:
    """Mark progress in a checkpointed development plan; explain blockers/retries.

    Advance one step at a time. Complete work only after its evidence is checked.
    A blocked step may return to pending to retry. Mutations are sequential.
    """
    old = _current(runtime)
    if not old:
        raise ValueError("当前任务尚无计划，请先调用 set_task_plan")
    if not 1 <= step_id <= len(old["steps"]):
        raise ValueError("步骤编号超出计划范围")
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ValueError("步骤说明过长")
    steps = [dict(item) for item in old["steps"]]
    item = steps[step_id - 1]
    previous = item["status"]
    if status == "in_progress":
        if previous not in {"pending", "blocked", "in_progress"}:
            raise ValueError("已完成步骤不可重新启动；需要调整请重新规划")
        if any(other["status"] == "in_progress" and other["id"] != step_id for other in steps):
            raise ValueError("请先完成或阻塞当前进行中的步骤")
        if any(other["status"] != "completed" for other in steps[:step_id - 1]):
            raise ValueError("请先处理前置步骤")
    elif status == "completed":
        if previous != "in_progress":
            raise ValueError("步骤必须先标记 in_progress，验证后才能完成")
    elif status == "pending":
        if previous != "blocked":
            raise ValueError("只有 blocked 步骤可以恢复为 pending")
    elif status == "blocked":
        if previous not in {"pending", "in_progress", "blocked"}:
            raise ValueError("已完成步骤不可标记为 blocked")
        if not note.strip():
            raise ValueError("blocked 步骤必须说明阻塞原因")
    item["status"] = status
    item["note"] = note.strip()
    plan = {**old, "steps": steps}
    return _reply("update_task_step", runtime, plan)


@tool
def get_task_plan(runtime: ToolRuntime[TaskState]) -> str:
    """Read current development plan, progress, blockers and revision (no side effects)."""
    return json.dumps(plan_summary(_current(runtime)), ensure_ascii=False)


def build_plan_tools() -> list[Any]:
    """Use ordinary LangChain tools so policy and checkpoint remain centralized."""
    for plan_tool in (set_task_plan, update_task_step, get_task_plan):
        plan_tool.metadata = {"risk": "read", "mutation": False, "workspace_arguments": ()}
    return [set_task_plan, update_task_step, get_task_plan]
