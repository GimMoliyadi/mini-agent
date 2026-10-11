"""Business acceptance middleware; framework still owns all model/tool routing."""

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage
import json

from .state import TaskState
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
            self.events.emit("verification_rejected", reasons=list(gate.reasons))
            return {"messages": [HumanMessage(content=(
                "验收尚未通过：" + ", ".join(gate.reasons) +
                "; 完成允许的修改并运行指定测试：" + str(contract.test_command.as_dict())
            ))], "jump_to": "model"}
        return None

    def after_agent(self, state, runtime):
        answer = state["messages"][-1]
        failures = unresolved_tool_failures(state)
        if not state.get("contract"):
            return {"status": "completed" if answer.content and not failures else "incomplete"}
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
        self.events.emit("verification", accepted=verification["accepted"])
        return {"verification": verification,
                "status": "completed" if verification["accepted"] else "incomplete"}
