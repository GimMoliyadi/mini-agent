"""Business acceptance middleware; framework still owns all model/tool routing."""

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
import json

from .state import TaskState
from .planning import plan_summary
from .development_workflow import evaluate_workflow
from .tools.command import run_command
from .verifier import (
    CodingTaskContract, TaskState as VerificationFacts, TaskStatus,
    evaluate_finish_request, parse_test_result, verify_contract,
)


def verification_facts(state: dict) -> VerificationFacts:
    facts = VerificationFacts(initial_snapshot=state["initial_snapshot"])
    contract = CodingTaskContract.from_dict(state["contract"])
    for message in state["messages"][state["task_start"]:]:
        if not isinstance(message, ToolMessage):
            continue
        facts.next_event()
        artifact = message.artifact or {}
        if artifact.get("mutation") and message.status == "success":
            facts.last_mutation_event_seq = facts.event_seq
        if artifact.get("command") == contract.test_command.as_dict():
            parsed = parse_test_result(str(message.content))
            if message.status != "success":
                # A failed ToolMessage is never evidence that the required
                # test ran successfully, even if a provider-shaped payload
                # happens to contain a passing summary.
                parsed["status"] = "UNKNOWN"
                parsed["test_count"] = None
            facts.last_test_status = parsed["status"]
            facts.last_test_count = parsed["test_count"]
            facts.verified_snapshot = artifact.get("snapshot") if parsed["status"] == "PASS" else None
            facts.last_successful_exact_required_test_seq = facts.event_seq if parsed["status"] == "PASS" else None
    return facts


def unresolved_tool_failures(state: dict) -> list[str]:
    failures = set()
    calls = {}
    for message in state["messages"][state["task_start"]:]:
        for call in getattr(message, "tool_calls", []):
            calls[call["id"]] = call
        if isinstance(message, ToolMessage):
            args = calls.get(message.tool_call_id, {}).get("args", {})
            # A successful write to B does not repair a failed write to A.
            target = tuple(str(args.get(field, "")) for field in ("path", "source", "destination", "cwd"))
            command = json.dumps([args.get("command"), args.get("args")], sort_keys=True)
            key = (message.name or "tool", target, command)
            if message.status == "error":
                failures.add(key)
            else:
                failures.discard(key)
    return sorted({key[0] for key in failures})


class VerificationMiddleware(AgentMiddleware):
    state_schema = TaskState

    def __init__(self, settings, events):
        self.settings = settings
        self.events = events

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):
        message = state["messages"][-1]
        if not state.get("contract") or getattr(message, "tool_calls", None):
            return None
        contract = CodingTaskContract.from_dict(state["contract"])
        gate = evaluate_finish_request(contract, verification_facts(state), self.settings.workspace)
        if not gate.accepted:
            self.events.emit("verification_rejected", task_id=state.get("task_id"), reasons=list(gate.reasons))
            return {"messages": [HumanMessage(content=(
                "验收尚未通过：" + ", ".join(gate.reasons) +
                "; 完成允许的修改并运行指定测试：" + str(contract.test_command.as_dict())
            ))], "jump_to": "model"}
        return None

    def after_agent(self, state, runtime):
        answer = state["messages"][-1]
        failures = unresolved_tool_failures(state)
        plan = plan_summary(state.get("plan"))
        # A model's final text is not evidence that planned work is finished.
        # Blocked steps also count as incomplete, but the checkpoint stays
        # available for an explicit continuation of the same task.
        unfinished = bool(plan is not None and plan["completed"] != plan["total"])
        workflow = evaluate_workflow(state)
        workflow_unfinished = bool(workflow is not None and not workflow["complete"])
        if workflow_unfinished:
            self.events.emit("development_workflow_incomplete", task_id=state.get("task_id"),
                             next_stage=workflow["next_stage"], completed=workflow["completed"],
                             total=workflow["total"])
        if unfinished:
            self.events.emit(
                "plan_incomplete", task_id=state.get("task_id"),
                completed=plan["completed"] if plan else 0,
                total=plan["total"] if plan else 0,
            )
        if not state.get("contract"):
            return {"status": "completed" if answer.content and not failures and not unfinished
                    and not workflow_unfinished else "incomplete"}
        contract = CodingTaskContract.from_dict(state["contract"])
        facts = verification_facts(state)
        gate = evaluate_finish_request(contract, facts, self.settings.workspace)
        if gate.accepted and not failures:
            facts.status = TaskStatus.FINISHED
        # Rerun only the exact test already approved and proved fresh in the graph.
        runner = run_command if (
            gate.accepted and facts.last_test_status == "PASS" and facts.verified_snapshot is not None
            and self.settings.approval_mode != "DENY"
        ) else None
        verification = verify_contract(
            contract, self.settings.workspace, state["initial_snapshot"], task_state=facts,
            agent_final_answer_present=bool(answer.content),
            agent_ran_required_test=facts.last_test_status == "PASS", command_runner=runner,
        )
        self.events.emit("verification", task_id=state.get("task_id"), accepted=verification["accepted"])
        return {"verification": verification,
                "status": "completed" if verification["accepted"] and not unfinished
                and not workflow_unfinished else "incomplete"}
