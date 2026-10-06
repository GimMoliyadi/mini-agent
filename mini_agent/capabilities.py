"""Safe, read-only view of the tools and runtime state of this Agent run."""

from acceptance import evaluate_finish_request, verify_contract
from config import MAX_AGENT_STEPS, WORKSPACE_DIR, get_approval_mode, get_context_mode
from recovery import HARD_CEILING, Recovery
from session import load_session, save_session
from tools import (
    RiskLevel,
    TOOL_REGISTRY,
    ToolKind,
    _ALLOWED_GIT_COMMANDS,
    _ALLOWED_PYTHON_MODULES,
    resolve_inside_workspace,
    validate_run_command_arguments,
)


def capability_snapshot(context: dict) -> dict:
    # Import at call time: main owns the live loop and already imports tools.
    import main

    contract = context.get("contract")
    state = context.get("task_state")
    recovery = context.get("recovery")
    approval_mode = get_approval_mode()
    approval_channel = main.interactive_approval_available() if approval_mode == "ASK" else None
    callable_tools = []
    for name, definition in TOOL_REGISTRY.items():
        full_description = definition.schema["function"]["description"]
        first_sentence, separator, _ = full_description.partition("。")
        if not separator:
            first_sentence, separator, _ = full_description.partition(". ")
        description = first_sentence + ("。" if separator == "。" else "." if separator else "")
        requires_approval = definition.risk_level is not RiskLevel.READ_ONLY and definition.tool_kind is not ToolKind.CONTROL_FLOW
        reason = None
        if name == "finish_task" and (contract is None or state is None):
            reason = "requires an active Coding Contract"
        elif name == "finish_task" and state is not None and state.status.value != "RUNNING":
            reason = "Coding Task is no longer running"
        elif requires_approval and approval_mode == "DENY":
            reason = "tool approval mode is DENY"
        elif requires_approval and approval_mode == "ASK" and not approval_channel:
            reason = "interactive approval channel unavailable"
        elif definition.workspace_arguments and not WORKSPACE_DIR.is_dir():
            reason = "workspace directory unavailable"
        item = {
            "name": name,
            "description": description,
            "risk_level": definition.risk_level.value,
            "tool_kind": definition.tool_kind.value,
            "approval_requirement": "user approval or ALLOW mode" if requires_approval else "none",
            "currently_available": reason is None,
        }
        if reason:
            item["unavailable_reason"] = reason
        if name == "run_command":
            item["command_policy"] = {
                "execution": "controlled, no shell; command and args array; workspace bounded",
                "python_modules": sorted(_ALLOWED_PYTHON_MODULES),
                "git_subcommands": sorted(_ALLOWED_GIT_COMMANDS),
                "validator": validate_run_command_arguments.__name__,
            }
        callable_tools.append(item)

    context_mode = get_context_mode()
    freshness = "no Coding Contract"
    if state is not None:
        test_seq = state.last_successful_exact_required_test_seq
        mutation_seq = state.last_mutation_event_seq
        freshness = ("not verified" if test_seq is None else
                     "fresh" if mutation_seq is None or test_seq > mutation_seq else "stale")

    def feature(supported: bool, active: bool | None, current_state: str | int) -> dict:
        return {"supported": supported, "active": active, "current_state": current_state}

    session_active = context.get("session_active")
    runtime_features = {
        "Session Persistence / Resume": feature(
            callable(save_session) and callable(load_session),
            session_active,
            "active session" if session_active else "no session in this entry point" if session_active is False else "unknown outside Agent loop"),
        "Context Management": feature(callable(main.build_model_context), context_mode != "OFF", context_mode),
        "Duplicate Tool Call Protection": feature(callable(main.call_fingerprint), True, "active per task"),
        "Workspace Sandbox": feature(callable(resolve_inside_workspace), True, "ready" if WORKSPACE_DIR.is_dir() else "workspace missing"),
        "Tool Permission / Approval": feature(callable(main.check_tool_permission), True, approval_mode),
        "Coding Contract": feature(True, contract is not None, "active" if contract else "absent"),
        "Explicit finish_task protocol": feature("finish_task" in TOOL_REGISTRY, contract is not None, "active" if contract else "Coding Contract required"),
        "Deterministic Finish Gate": feature(callable(evaluate_finish_request), contract is not None, "active" if contract else "Coding Contract required"),
        "Independent Verifier": feature(
            callable(verify_contract),
            context.get("verifier_enabled"),
            "runs after Coding Task" if context.get("verifier_enabled") else "not configured in this entry point"),
        "verification freshness": feature(True, state is not None, freshness),
        "stage-aware recovery": feature(callable(Recovery.observe), recovery is not None, recovery.stage if recovery else "inactive"),
        "hard model-call ceiling": feature(True, True, HARD_CEILING if contract else MAX_AGENT_STEPS),
    }
    return {"callable_tools": callable_tools, "runtime_features": runtime_features}
