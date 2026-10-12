"""Business facts extend framework state without replacing its message reducer."""

from typing import NotRequired
from langchain.agents import AgentState


class TaskState(AgentState):
    workspace: str
    task_id: str
    contract: dict | None
    initial_snapshot: dict[str, str]
    task_start: int
    plan: NotRequired[dict | None]
    preconditions: NotRequired[dict]
    verification: NotRequired[dict | None]
    status: NotRequired[str]
