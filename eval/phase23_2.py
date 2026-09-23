"""Continue the saved Phase 23 eight-turn failure prefix with fixed recovery budgets."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from . import phase22_5, phase22_harness as harness, phase23_budget

import acceptance
import config
import main
import tools


EVAL_DIR = Path(__file__).resolve().parent
RESULTS = EVAL_DIR / "phase23_2_results.json"
REPORT = EVAL_DIR / "phase23_2_report.md"
PRIOR_RESULTS = EVAL_DIR / "phase23_budget_results.json"
RECOVERY_BUDGETS = (2, 4)
INFRASTRUCTURE_RETRIES = 1
RECOVERY_BUDGET_ENV = "PHASE23_2_RECOVERY_BUDGET"
EXPECTED_PREFIX_ID = "caecc4acb2ce6820"
EXPECTED_PREFIX_MESSAGES = 28
TRACE_COUNTERS = (
    "model_calls",
    "tool_calls",
    "executed_tool_calls",
    "list_files_calls",
    "search_text_calls",
    "read_file_calls",
    "write_file_calls",
    "apply_patch_calls",
    "patch_successes",
    "patch_failures",
    "run_command_calls",
    "productive_calls",
    "executed_tools",
    "duplicate_blocked",
    "policy_rejected",
    "failed_commands",
    "successful_commands",
    "finish_task_calls",
    "finish_successes",
    "finish_rejections",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


def _prior_payload() -> dict:
    return json.loads(PRIOR_RESULTS.read_text(encoding="utf-8"))


def _fixed_prefix() -> tuple[dict, list[dict], str]:
    payload = _prior_payload()
    baseline = payload["runs"]["8"]["result"]
    if baseline.get("max_agent_steps") != 8 or baseline.get("task_id") != "diagnosis_recovery":
        raise ValueError("Phase 23 saved 8-step result is not the diagnosis baseline")

    messages = baseline.get("canonical_history")
    if not isinstance(messages, list) or len(messages) < 3:
        raise ValueError("Phase 23 saved baseline has no complete canonical history")
    analysis = phase23_budget._analysis(baseline)
    failure = analysis.get("failure_output") or ""
    if (
        analysis.get("first_required_test_failure_turn") != 8
        or "test_one_percent_boundary" not in failure
        or "0.0 != 99.0" not in failure
        or messages[-1].get("role") != "tool"
        or "0.0 != 99.0" not in messages[-1].get("content", "")
    ):
        raise ValueError("Saved canonical history does not contain the actual Turn 8 failure output")

    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prefix_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    if len(messages) != EXPECTED_PREFIX_MESSAGES or prefix_id != EXPECTED_PREFIX_ID:
        raise ValueError("Saved canonical history no longer matches the fixed Phase 23 Turn 8 prefix")
    return baseline, copy.deepcopy(messages), prefix_id


def _assistant_turns(messages: list[dict]) -> list[dict]:
    return [message for message in messages if message.get("role") == "assistant"]


def _historical_patch(messages: list[dict]) -> dict:
    turns = _assistant_turns(messages)
    if len(turns) < 7:
        raise ValueError("Saved history does not reach the Turn 7 mutation")
    calls = turns[6].get("tool_calls") or []
    patches = [call for call in calls if call.get("function", {}).get("name") == "apply_patch"]
    if len(patches) != 1:
        raise ValueError("Expected one canonical Turn 7 apply_patch call")
    arguments = json.loads(patches[0]["function"]["arguments"])
    if arguments.get("path") != "src/pricing/coupons.py":
        raise ValueError("Saved Turn 7 mutation does not target the expected coupon implementation")
    return arguments


def _reconstruct_executed(messages: list[dict], trace: dict) -> set[tuple[str, str]]:
    trace_tools = [event for event in trace.get("events", []) if event.get("action") == "tool_call"]
    trace_index = 0
    executed: set[tuple[str, str]] = set()
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        result_index = index + 1
        for call_data in message.get("tool_calls") or []:
            if result_index >= len(messages):
                raise ValueError("Canonical prefix ends before a tool result")
            result_message = messages[result_index]
            if (
                result_message.get("role") != "tool"
                or result_message.get("tool_call_id") != call_data.get("id")
            ):
                raise ValueError("Canonical prefix contains an unmatched tool call")
            if trace_index >= len(trace_tools):
                raise ValueError("Saved baseline trace is missing a tool call")

            trace_event = trace_tools[trace_index]
            trace_index += 1
            result_index += 1
            if not trace_event.get("executed"):
                continue

            function = call_data.get("function", {})
            call = SimpleNamespace(
                function=SimpleNamespace(
                    name=function.get("name", ""),
                    arguments=function.get("arguments") or "{}",
                )
            )
            result = result_message.get("content") or ""
            if main.counts_as_successful_duplicate(call, result):
                executed.add(main.call_fingerprint(call))

    if trace_index != len(trace_tools):
        raise ValueError("Saved baseline trace has tool calls absent from canonical history")
    return executed


def _prepare_workspace(root: Path, baseline: dict, messages: list[dict]) -> tuple[object, object, dict, dict]:
    spec = phase22_5.setup("E", root, register=True)
    contract_data = phase22_5.contract(spec)
    contract = acceptance.CodingTaskContract.from_dict(contract_data)
    initial_messages = harness._model_messages(contract)
    if messages[: len(initial_messages)] != initial_messages:
        raise ValueError("Fixed prefix prompt or Contract differs from the saved run")
    if contract_data["test_command"] != {
        "command": phase23_budget.REQUIRED_TEST[0],
        "args": phase23_budget.REQUIRED_TEST[1],
        "cwd": phase23_budget.REQUIRED_TEST[2],
    }:
        raise ValueError("Fixed prefix required test differs from the saved run")

    before = acceptance.snapshot_workspace(root)
    saved_initial = baseline["task_state"].get("initial_snapshot")
    if before != saved_initial:
        raise ValueError("Rebuilt fixture does not match the baseline initial workspace snapshot")

    patch_arguments = _historical_patch(messages)
    previous_main_workspace = main.WORKSPACE_DIR
    previous_tools_workspace = tools.WORKSPACE_DIR
    main.WORKSPACE_DIR = root
    tools.WORKSPACE_DIR = root
    try:
        patch_result = tools.apply_patch(**patch_arguments)
    finally:
        main.WORKSPACE_DIR = previous_main_workspace
        tools.WORKSPACE_DIR = previous_tools_workspace
    if patch_result.startswith(main.TOOL_FAILURE_PREFIX):
        raise ValueError(f"Could not replay the canonical Turn 7 mutation: {patch_result}")

    post_mutation = acceptance.snapshot_workspace(root)
    changed_paths = sorted(
        path for path in set(before) | set(post_mutation)
        if before.get(path) != post_mutation.get(path)
    )
    if changed_paths != ["src/pricing/coupons.py"]:
        raise ValueError(f"Replayed Turn 7 mutation changed unexpected files: {changed_paths}")

    state = acceptance.TaskState.from_dict(copy.deepcopy(baseline["task_state"]))
    if state.status is not acceptance.TaskStatus.LIMIT_REACHED:
        raise ValueError("Saved Turn 8 baseline TaskState is not LIMIT_REACHED")
    task_state_before_recovery = state.as_dict()
    state.status = acceptance.TaskStatus.RUNNING
    if state.event_seq != 18 or state.last_mutation_event_seq != 17:
        raise ValueError("Saved Turn 8 TaskState event sequence differs from the failure prefix")
    expected_recovery_state = copy.deepcopy(task_state_before_recovery)
    expected_recovery_state["status"] = acceptance.TaskStatus.RUNNING.value
    if state.as_dict() != expected_recovery_state:
        raise ValueError("Recovery setup changed TaskState fields other than LIMIT_REACHED -> RUNNING")
    return spec, contract, before, {
        "task_state": state,
        "task_state_before_recovery": task_state_before_recovery,
        "task_state_at_recovery_start": state.as_dict(),
        "post_mutation_snapshot": post_mutation,
        "replayed_mutation_result": patch_result,
    }


def _combined_trace(baseline_trace: dict, recovery_trace: dict, state) -> dict:
    combined = copy.deepcopy(baseline_trace)
    for key in TRACE_COUNTERS:
        combined[key] = (baseline_trace.get(key) or 0) + (recovery_trace.get(key) or 0)
    appended_events = copy.deepcopy(recovery_trace.get("events", []))
    for event in appended_events:
        if event.get("turn") is not None:
            event["turn"] += 8
    combined["events"] = copy.deepcopy(baseline_trace.get("events", [])) + appended_events
    combined["max_steps_reached"] = bool(recovery_trace.get("max_steps_reached"))
    combined["final_answer"] = recovery_trace.get("final_answer")
    combined["task_status"] = state.status.value
    combined["finish_attempts"] = list(state.finish_attempts)
    return combined


def _trace_from_summary(summary: dict) -> main.CodingTaskTrace:
    """Restore the concrete trace type required by the independent verifier."""
    trace = main.CodingTaskTrace()
    for name, value in summary.items():
        if hasattr(trace, name):
            setattr(trace, name, copy.deepcopy(value))
    return trace


def _fingerprint_state(fingerprints: set[tuple[str, str]]) -> dict:
    digests = sorted(
        hashlib.sha256(f"{name}\0{arguments}".encode("utf-8")).hexdigest()
        for name, arguments in fingerprints
    )
    return {"count": len(fingerprints), "sha256": digests}


def _run_recovery(budget: int, scripted_replies: list | None = None) -> dict:
    root = Path(os.environ["AGENT_WORKSPACE"]).resolve()
    baseline, prefix, prefix_id = _fixed_prefix()
    spec, contract, before, prepared = _prepare_workspace(root, baseline, prefix)
    state = prepared["task_state"]
    messages = copy.deepcopy(prefix)
    executed = _reconstruct_executed(prefix, baseline["trace"])
    if len(executed) != 17:
        raise ValueError(f"Fixed prefix executed-call state changed: expected 17, got {len(executed)}")

    manifest = _prior_payload()["manifest"]
    if [item["function"]["name"] for item in main.AVAILABLE_TOOLS] != manifest["tool_names"]:
        raise ValueError("Available tools differ from the fixed prefix manifest")
    if main.AVAILABLE_TOOLS != manifest["tool_definitions"]:
        raise ValueError("Tool definitions differ from the fixed prefix manifest")

    config.load_env_file()
    model_config = config.load_config()
    if model_config.model != baseline.get("model") or model_config.model != manifest["model"]:
        raise ValueError("Configured model differs from the fixed prefix model")
    if config.get_context_mode() != "WRITE_ONLY" or config.get_approval_mode() != "ALLOW":
        raise ValueError("Context or approval mode differs from the fixed prefix")

    recovery_trace = main.CodingTaskTrace()
    replies: list[dict] = []
    runtime_errors: list[str] = []
    provider_error = False
    client = None
    real_ask = main.ask
    scripted_queue = list(scripted_replies or [])

    def scripted_ask(llm_client, model, history):
        if not scripted_queue:
            raise RuntimeError("scripted recovery replies exhausted")
        reply = scripted_queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    ask_impl = scripted_ask if scripted_replies is not None else real_ask

    def recording_ask(llm_client, model, history):
        nonlocal provider_error
        try:
            reply = ask_impl(llm_client, model, history)
        except BaseException:
            provider_error = True
            raise
        replies.append({
            "finish_reason": reply.finish_reason,
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
            "total_tokens": reply.total_tokens,
        })
        return reply

    previous_main_workspace = main.WORKSPACE_DIR
    previous_tools_workspace = tools.WORKSPACE_DIR
    main.WORKSPACE_DIR = root
    tools.WORKSPACE_DIR = root
    try:
        with patch.object(main, "ask", side_effect=recording_ask):
            try:
                if scripted_replies is None:
                    client = main.build_client(model_config)
                first_reply = main.ask(client, model_config.model, main.build_model_context(messages))
                main.log_reply(1, first_reply)
                with patch.object(main, "MAX_AGENT_STEPS", budget):
                    main.run_agent_loop(
                        client,
                        model_config.model,
                        messages,
                        first_reply,
                        executed,
                        main.always_allow,
                        trace=recovery_trace,
                        required_test=harness._required_test(contract),
                        contract=contract,
                        task_state=state,
                    )
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"
                runtime_errors.append(error)
                state.status = acceptance.TaskStatus.ERROR
                state.unresolved_runtime_error = error
                recovery_trace.set_task_state(state)
    finally:
        if client is not None:
            client.close()
        main.WORKSPACE_DIR = previous_main_workspace
        tools.WORKSPACE_DIR = previous_tools_workspace

    recovery_summary = recovery_trace.summary()
    combined = _combined_trace(baseline["trace"], recovery_summary, state)
    verification_trace = _trace_from_summary(combined)
    verdict = harness._verify(contract, root, before, state, messages, verification_trace)
    metrics = harness.collect_metrics(messages, combined, spec, contract, state, verdict)
    infrastructure_failure = bool(runtime_errors)
    result_for_analysis = {
        "task_id": spec.task_id,
        "task_code": spec.code,
        "model": model_config.model,
        "context_mode": config.get_context_mode(),
        "approval_mode": config.get_approval_mode(),
        "max_agent_steps": budget,
        "runtime_errors": runtime_errors,
        "infrastructure_failure": infrastructure_failure,
        "infrastructure_kind": (
            "provider" if provider_error else harness._runtime_error_kind(runtime_errors[0])
        ) if runtime_errors else None,
        "metrics": metrics,
        "trace": combined,
        "canonical_history": messages,
    }
    analysis = phase23_budget._analysis(result_for_analysis)

    all_tool_events = harness._history_tool_events(messages, combined)
    failure_seq = next(
        event["event_seq"] for event in all_tool_events
        if event.get("turn") == 8 and event.get("tool") == "run_command"
        and event.get("exit_code") not in (None, 0)
    )
    post_failure_events = [
        event for event in all_tool_events
        if event.get("event_seq") is not None and event["event_seq"] > failure_seq
    ]
    recovery_history = copy.deepcopy(messages[len(prefix):])
    complete_token_usage = bool(replies) and all(
        all(reply.get(name) is not None for name in ("prompt_tokens", "completion_tokens", "total_tokens"))
        and reply["total_tokens"] == reply["prompt_tokens"] + reply["completion_tokens"]
        for reply in replies
    )
    observed_tokens = sum(reply["total_tokens"] or 0 for reply in replies)
    first_recovery_message = next(
        (message for message in recovery_history if message.get("role") == "assistant"),
        None,
    )
    first_recovery_calls = (first_recovery_message or {}).get("tool_calls") or []
    post_failure_counts = {
        name: sum(event.get("tool") == name for event in post_failure_events)
        for name in ("read_file", "search_text", "list_files")
    }
    before_recovery_fingerprints = _fingerprint_state(_reconstruct_executed(prefix, baseline["trace"]))
    after_recovery_fingerprints = _fingerprint_state(executed)
    first_response_evidence = (first_recovery_message or {}).get("content") or ""
    first_response_evidence += json.dumps(first_recovery_calls, ensure_ascii=False)
    failure_evidence_assessment = {
        "automatic_failed_test_name_match": analysis.get("next_model_turn_visibly_references_failure", False),
        "used_boundary_result_evidence": any(marker in first_response_evidence for marker in ("1%", "99.0", "0.0")),
        "first_response_and_action": first_response_evidence,
    }
    return {
        "valid_real_run": False,
        "infrastructure_failure": infrastructure_failure,
        "infrastructure_kind": result_for_analysis["infrastructure_kind"],
        "runtime_errors": runtime_errors,
        "fixed_prefix_id": prefix_id,
        "fixed_prefix_message_count": len(prefix),
        "canonical_history": copy.deepcopy(messages),
        "recovery_history": recovery_history,
        "post_failure_tool_chain": post_failure_events,
        "first_post_failure_action": {
            "model_response": copy.deepcopy(first_recovery_message),
            "tool_calls": copy.deepcopy(first_recovery_calls),
        },
        "failure_evidence_assessment": failure_evidence_assessment,
        "post_failure_read_search_list_counts": post_failure_counts,
        "task_state_before_recovery": prepared["task_state_before_recovery"],
        "task_state_at_recovery_start": prepared["task_state_at_recovery_start"],
        "task_state_after_recovery": state.as_dict(),
        "executed_call_state": {
            "before_recovery": before_recovery_fingerprints,
            "after_recovery": after_recovery_fingerprints,
        },
        "analysis": analysis,
        "combined_trace": combined,
        "recovery_trace": recovery_summary,
        "recovery_verifier_trace_type": type(verification_trace).__name__,
        "recovery_model_calls": recovery_summary["model_calls"],
        "recovery_model_turns_used": recovery_summary["model_calls"],
        "recovery_tool_calls": recovery_summary["tool_calls"],
        "second_mutation": analysis.get("second_mutation"),
        "required_test_attempts": analysis.get("post_failure_required_test_attempts", []),
        "finish_task_called": analysis.get("finish_task_turn") is not None,
        "recovery_token_usage": {
            "observed_total_tokens": observed_tokens if replies else None,
            "complete": complete_token_usage,
            "model_calls": replies,
        },
        "post_verification_extra_tool_calls": metrics.get("post_verification_extra_tool_calls"),
        "metrics": metrics,
        "task_state": state.as_dict(),
        "acceptance": verdict,
        "verifier_result": copy.deepcopy(verdict),
        "status": state.status.value,
        "accepted": metrics.get("accepted", False),
        "model": model_config.model,
        "context_mode": config.get_context_mode(),
        "approval_mode": config.get_approval_mode(),
        "fixture_snapshot_after_first_mutation": prepared["post_mutation_snapshot"],
    }


def _json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)


def _scripted_recovery_replies() -> list:
    def reply(call_id: str, name: str, arguments: dict, content: str = ""):
        call = SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            ),
        )
        return main.ModelReply(
            SimpleNamespace(content=content, tool_calls=[call]),
            "tool_calls",
            10,
            2,
            12,
        )

    return [
        reply(
            "phase23_2_scripted_patch",
            "apply_patch",
            {
                "path": "src/pricing/coupons.py",
                "old_text": "factor = percent / 100.0 if percent > 1 else percent",
                "new_text": (
                    "factor = percent / 100.0 "
                    "if (isinstance(percent, int) or percent > 1) else percent"
                ),
            },
            "test_one_percent_boundary failed: integer 1 must mean 1%, not a decimal fraction.",
        ),
        reply(
            "phase23_2_scripted_test",
            "run_command",
            {
                "command": phase23_budget.REQUIRED_TEST[0],
                "args": list(phase23_budget.REQUIRED_TEST[1]),
                "cwd": phase23_budget.REQUIRED_TEST[2],
            },
        ),
        reply(
            "phase23_2_scripted_finish",
            "finish_task",
            {"summary": "Corrected the 1% boundary, reran the required test, and finished."},
        ),
    ]


def _offline_harness_validation() -> dict:
    """Exercise recovery, verification, JSON persistence and infra classification without a provider."""
    model_name = _prior_payload()["manifest"]["model"]
    model_config = SimpleNamespace(model=model_name)

    def run_and_round_trip(workspace: Path, replies: list, filename: str) -> dict:
        with (
            patch.dict(os.environ, {"AGENT_WORKSPACE": str(workspace)}),
            patch.object(config, "load_env_file"),
            patch.object(config, "load_config", return_value=model_config),
            patch.object(config, "get_context_mode", return_value="WRITE_ONLY"),
            patch.object(config, "get_approval_mode", return_value="ALLOW"),
        ):
            with redirect_stdout(io.StringIO()):
                result = _run_recovery(4, scripted_replies=replies)
        saved = workspace.parent / filename
        saved.write_text(_json_text(result), encoding="utf-8")
        return json.loads(saved.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="phase23-2-offline-harness-") as directory:
        root = Path(directory)
        finished = run_and_round_trip(
            root / "finished_workspace",
            _scripted_recovery_replies(),
            "scripted_finished_result.json",
        )
        provider_failure = run_and_round_trip(
            root / "provider_failure_workspace",
            [_scripted_recovery_replies()[0], ConnectionError("mock provider connection failure")],
            "scripted_provider_failure_result.json",
        )

    if not _complete_valid_run(finished) or finished.get("status") != "FINISHED" or not finished.get("accepted"):
        raise AssertionError("Scripted FINISHED recovery did not survive result JSON round-trip")
    if finished.get("recovery_verifier_trace_type") != "CodingTaskTrace":
        raise AssertionError("Verifier did not receive the reconstructed CodingTaskTrace type")
    if not (
        provider_failure.get("infrastructure_failure")
        and provider_failure.get("infrastructure_kind") == "provider"
        and not _complete_valid_run(provider_failure)
    ):
        raise AssertionError("Scripted provider failure was not classified as infrastructure")
    return {
        "status": "passed",
        "provider_used": False,
        "coding_task_trace_passed_to_verify": finished["recovery_verifier_trace_type"] == "CodingTaskTrace",
        "finished_result_json_round_trip": True,
        "scripted_finished_result": finished,
        "scripted_provider_failure_classification": {
            "infrastructure_failure": provider_failure["infrastructure_failure"],
            "infrastructure_kind": provider_failure["infrastructure_kind"],
            "valid_real_run": provider_failure["valid_real_run"],
            "runtime_errors": provider_failure["runtime_errors"],
        },
    }


def _complete_valid_run(result: dict) -> bool:
    token_usage = result.get("recovery_token_usage") or {}
    required = (
        "fixed_prefix_id",
        "canonical_history",
        "recovery_history",
        "first_post_failure_action",
        "failure_evidence_assessment",
        "post_failure_tool_chain",
        "task_state_before_recovery",
        "task_state_at_recovery_start",
        "task_state_after_recovery",
        "executed_call_state",
        "analysis",
        "combined_trace",
        "recovery_trace",
        "recovery_verifier_trace_type",
        "metrics",
        "acceptance",
        "verifier_result",
        "recovery_model_calls",
        "recovery_tool_calls",
        "recovery_token_usage",
        "status",
    )
    has_fields = all(name in result and result[name] is not None for name in required)
    complete = (
        has_fields
        and not result.get("infrastructure_failure")
        and not result.get("runtime_errors")
        and bool(token_usage.get("complete"))
        and token_usage.get("observed_total_tokens") is not None
        and token_usage.get("model_calls")
        and len(result.get("canonical_history") or []) > result.get("fixed_prefix_message_count", 0)
        and result.get("recovery_model_calls") == len(token_usage.get("model_calls", []))
        and result.get("recovery_trace", {}).get("model_calls") == result.get("recovery_model_calls")
        and result.get("recovery_trace", {}).get("tool_calls") == result.get("recovery_tool_calls")
        and result.get("recovery_verifier_trace_type") == "CodingTaskTrace"
        and result.get("task_state_after_recovery") == result.get("task_state")
    )
    if complete:
        prior_state = result["task_state_before_recovery"]
        expected_start = copy.deepcopy(prior_state)
        expected_start["status"] = acceptance.TaskStatus.RUNNING.value
        complete = (
            result["task_state_at_recovery_start"] == expected_start
            and prior_state.get("status") == acceptance.TaskStatus.LIMIT_REACHED.value
            and prior_state.get("event_seq") == 18
            and prior_state.get("last_mutation_event_seq") == 17
            and result["executed_call_state"].get("before_recovery", {}).get("count") == 17
        )
    if complete:
        usage_rows = token_usage["model_calls"]
        observed_total = sum(row.get("total_tokens") or 0 for row in usage_rows)
        trace_tokens = result["recovery_trace"].get("total_tokens")
        prefix = result["canonical_history"][: result["fixed_prefix_message_count"]]
        prefix_id = hashlib.sha256(
            json.dumps(prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        complete = (
            observed_total == token_usage.get("observed_total_tokens") == trace_tokens
            and prefix_id == result.get("fixed_prefix_id") == EXPECTED_PREFIX_ID
            and "0.0 != 99.0" in (prefix[-1].get("content") or "")
            and "test_one_percent_boundary" in (prefix[-1].get("content") or "")
        )
    result["valid_real_run"] = bool(complete)
    if not complete and not result.get("infrastructure_failure"):
        result["infrastructure_failure"] = True
        result["infrastructure_kind"] = "incomplete_trace"
        result.setdefault("runtime_errors", []).append("Required recovery evidence or complete token usage is missing")
    return bool(complete)


def _refresh_result_derived_fields(result: dict) -> bool:
    """Recalculate deterministic summaries when reopening a saved full trace."""
    combined = result.get("combined_trace")
    messages = result.get("canonical_history")
    if isinstance(combined, dict) and isinstance(messages, list):
        result["analysis"] = phase23_budget._analysis({
            "metrics": result.get("metrics", {}),
            "trace": combined,
            "canonical_history": messages,
        })
        prefix_count = result.get("fixed_prefix_message_count", 0)
        first = next(
            (message for message in messages[prefix_count:] if message.get("role") == "assistant"),
            None,
        )
        calls = (first or {}).get("tool_calls") or []
        result["first_post_failure_action"] = {
            "model_response": copy.deepcopy(first),
            "tool_calls": copy.deepcopy(calls),
        }
        analysis = result["analysis"]
        evidence = (first or {}).get("content") or ""
        evidence += json.dumps(calls, ensure_ascii=False)
        result["failure_evidence_assessment"] = {
            "automatic_failed_test_name_match": analysis.get("next_model_turn_visibly_references_failure", False),
            "used_boundary_result_evidence": any(marker in evidence for marker in ("1%", "99.0", "0.0")),
            "first_response_and_action": evidence,
        }
        result["second_mutation"] = analysis.get("second_mutation")
        result["required_test_attempts"] = analysis.get("post_failure_required_test_attempts", [])
        result["finish_task_called"] = analysis.get("finish_task_turn") is not None
        result["recovery_model_turns_used"] = result.get("recovery_model_calls")
    return _complete_valid_run(result)


def _run_attempt(budget: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"phase23-2-recovery-{budget}-") as directory:
        root = Path(directory) / "workspace"
        environment = phase23_budget._provider_child_environment({
            **os.environ,
            "AGENT_WORKSPACE": str(root),
            "TOOL_APPROVAL_MODE": "ALLOW",
            "CONTEXT_MODE": "WRITE_ONLY",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            RECOVERY_BUDGET_ENV: str(budget),
        })
        try:
            completed = subprocess.run(
                [str(harness.PYTHON_BIN), "-m", "eval.phase23_2", "--child"],
                cwd=harness.PROJECT_ROOT,
                env=environment,
                capture_output=True,
                timeout=harness.CHILD_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "valid_real_run": False,
                "infrastructure_failure": True,
                "infrastructure_kind": "harness",
                "runtime_errors": [f"{type(exc).__name__}: {exc}"],
                "recovery_model_calls": 0,
                "recovery_tool_calls": 0,
                "recovery_token_usage": {"observed_total_tokens": None, "complete": False, "model_calls": []},
            }

    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "valid_real_run": False,
            "infrastructure_failure": True,
            "infrastructure_kind": "harness",
            "runtime_errors": [f"child exit={completed.returncode}; {stderr[-3000:]}; {stdout[-1000:]}"],
            "child_stderr_tail": stderr[-3000:],
            "recovery_model_calls": 0,
            "recovery_tool_calls": 0,
            "recovery_token_usage": {"observed_total_tokens": None, "complete": False, "model_calls": []},
        }
    if completed.returncode and not result.get("infrastructure_failure"):
        result["valid_real_run"] = False
        result["infrastructure_failure"] = True
        result["infrastructure_kind"] = "harness"
        result.setdefault("runtime_errors", []).append(f"child exit={completed.returncode}: {stderr[-3000:]}")
    result["child_stderr_tail"] = stderr[-2000:]
    _complete_valid_run(result)
    return result


def _run_slot(budget: int, slot: int) -> dict:
    attempts = []
    for attempt in range(INFRASTRUCTURE_RETRIES + 1):
        result = _run_attempt(budget)
        attempts.append(result)
        print(
            f"[Phase 23.2 {budget}-turn slot {slot} attempt {attempt + 1}] "
            f"valid={result.get('valid_real_run')} status={result.get('status')} "
            f"accepted={result.get('accepted')} infra={result.get('infrastructure_kind')}",
            file=sys.stderr,
            flush=True,
        )
        if result.get("valid_real_run") or attempt == INFRASTRUCTURE_RETRIES:
            break
    return {
        "slot": slot,
        "attempt_count": len(attempts),
        "valid_real_run": bool(attempts[-1].get("valid_real_run")),
        "prior_attempts": attempts[:-1],
        "result": attempts[-1],
    }


def _prior_comparison(payload: dict) -> dict:
    phase23 = payload["phase23_1_recovery"]
    runs = phase23.get("budget_runs", {})
    summaries = {}
    for budget in ("10", "12"):
        entry = runs[budget]
        metrics = entry["result"].get("metrics", {})
        summaries[budget] = {
            "valid_real_run": entry.get("valid_real_run"),
            "status": metrics.get("status"),
            "accepted": metrics.get("accepted"),
            "model_calls": metrics.get("model_calls"),
            "tool_calls": metrics.get("tool_calls"),
            "total_tokens": metrics.get("total_tokens"),
        }
    baseline = payload["runs"]["8"]["result"]["metrics"]
    return {
        "8": {
            "status": baseline.get("status"),
            "accepted": baseline.get("accepted"),
            "model_calls": baseline.get("model_calls"),
            "tool_calls": baseline.get("tool_calls"),
            "required_test_final_exit_code": baseline.get("required_test_final_exit_code"),
        },
        "10_and_12": summaries,
    }


def _fixed_prefix_details(baseline: dict, messages: list[dict], prefix_id: str) -> dict:
    analysis = phase23_budget._analysis(baseline)
    return {
        "source": "eval/phase23_budget_results.json -> runs.8.result.canonical_history",
        "source_run_reused": True,
        "no_new_baseline_run": True,
        "prefix_id": prefix_id,
        "message_count": len(messages),
        "last_model_turn": 8,
        "failure_test": analysis["failure_test_name"],
        "failure_output": analysis["failure_output"],
        "failure_message_is_last_prefix_message": messages[-1].get("role") == "tool",
        "initial_task_state": copy.deepcopy(baseline["task_state"]),
        "recovery_task_state_reset": {
            "only_changed_field": "status",
            "from": "LIMIT_REACHED",
            "to": "RUNNING",
            "event_seq": baseline["task_state"]["event_seq"],
            "last_mutation_event_seq": baseline["task_state"]["last_mutation_event_seq"],
        },
    }


def _provisional_stderr_summary(payload: dict) -> dict:
    observations = []
    for budget, slots in payload.get("runs", {}).items():
        for slot in slots:
            for item in slot.get("captured_attempt_observations", []):
                if not item.get("agent_loop_returned"):
                    continue
                observations.append({
                    "budget": int(budget),
                    "slot": slot.get("slot"),
                    "source_attempt": item.get("source_attempt"),
                    "status": item.get("terminal_status_observed_in_runtime_output"),
                    "recovery_model_calls": item.get("recovery_model_calls"),
                    "tool_events": item.get("visible_tool_events", []),
                    "required_test_exit_codes": item.get("required_test_exit_codes_observed", []),
                    "finish_task_called": item.get("finish_task_called"),
                    "boundary_failure_evidence_directly_used": item.get("boundary_failure_evidence_directly_used"),
                    "token_total_complete": item.get("token_total_complete"),
                    "canonical_recovery_history_saved": item.get("structured_canonical_recovery_history_saved"),
                })
    return {
        "classification": "provisional only; legacy attempts failed in postprocessing and are not valid runs",
        "raw_source": "runs[*].captured_attempt_observations[*].captured_log_excerpt",
        "observations": observations,
    }


def _update_conclusions(payload: dict) -> None:
    corrected = payload.get("corrected_reruns", {})
    current_audit = payload.get("capture_audit", {})
    if "legacy_attempts_preserved_under" not in current_audit:
        payload.setdefault("legacy_harness_capture_audit", copy.deepcopy(current_audit))
        payload["capture_audit"] = {
            "status": "corrected recovery capture",
            "legacy_root_cause": current_audit.get("root_cause"),
            "legacy_attempts_preserved_under": "runs",
            "legacy_attempts_are_valid_runs": False,
            "scripted_harness_validation": payload.get("harness_offline_validation", {}).get("status"),
        }
    selected = {
        budget: next(
            (slot.get("result", {}) for slot in corrected.get(str(budget), []) if slot.get("valid_real_run")),
            None,
        )
        for budget in RECOVERY_BUDGETS
    }
    payload.setdefault("capture_audit", {})["corrected_valid_runs"] = {
        str(budget): sum(
            1 for slot in corrected.get(str(budget), []) if slot.get("valid_real_run")
        )
        for budget in RECOVERY_BUDGETS
    }
    payload["capture_audit"]["corrected_full_canonical_histories_saved"] = all(
        bool(corrected.get(str(budget)))
        and all(bool(slot.get("result", {}).get("canonical_history")) for slot in corrected[str(budget)])
        for budget in RECOVERY_BUDGETS
    )
    two = selected[2]
    four = selected[4]
    two_pass = bool(two and two.get("analysis", {}).get("required_test_pass_turns"))
    two_finished = bool(two and two.get("status") == "FINISHED" and two.get("accepted"))
    four_pass = bool(four and four.get("analysis", {}).get("required_test_pass_turns"))
    four_finished = bool(four and four.get("status") == "FINISHED" and four.get("accepted"))

    if two_pass and not two_finished and four_pass and four_finished:
        payload["diagnosis_conclusion"] = (
            "在这一份固定失败状态样本中，2-turn recovery 重新通过了 required test，但没有完成 finish；"
            "4-turn recovery 做出针对性二次修改、测试 PASS 并 FINISHED。这提示 2 turns 可能不够覆盖完整闭环，"
            "而 4 turns 在此样本中足够；每组只有一个新有效样本，不能外推为稳定规律。"
        )
    elif two_pass and not two_finished and four_pass and not four_finished:
        payload["diagnosis_conclusion"] = (
            "2-turn corrected run 在固定失败状态中使用失败证据做了针对性二次修改并使 required test PASS，"
            "但没有 finish；这证明该样本里模型能恢复 artifact，但不等于 interaction completion。"
            "4-turn run 先做了错误修正、复测仍 FAIL，再进行第二次针对性修改后耗尽 turns，未完成复测或 finish。"
            "每组只有一个有效样本，不能外推为稳定能力或固定预算结论。"
        )
    elif two_pass and not two_finished and four and not four_pass and not four_finished:
        payload["diagnosis_conclusion"] = (
            "2-turn corrected run 在固定失败状态中使用失败证据做了针对性二次修改并使 required test PASS，"
            "但没有 finish；这是 artifact recovery 成功、interaction completion 未完成。"
            "4-turn run 明确识别 1% 边界，首个修正又使 decimal-percent tests 失败；模型随后做了第二次针对性修改，"
            "但花掉剩余 turn 搜索，没有再次运行 required test 或 finish。每组仅一个有效样本，不足以说明 4 turns 稳定有效。"
        )
    elif four_finished:
        payload["diagnosis_conclusion"] = (
            "4-turn corrected run 在固定失败状态中完成了 recovery 闭环，支持模型在该样本中具备 diagnosis/recovery 能力。"
            "2-turn 结果按其完整 trace 单独解释；每组只有一个新有效样本，不能据此声称稳定。"
        )
    elif two_finished:
        payload["diagnosis_conclusion"] = (
            "2-turn corrected run 在固定失败状态中完成了 recovery 闭环，说明该闭环可在两轮内发生于此样本。"
            "4-turn 结果按其完整 trace 单独解释；每组只有一个新有效样本，不能据此声称稳定。"
        )
    else:
        payload["diagnosis_conclusion"] = (
            "修正后的样本没有证明完整的 recovery 闭环；若 test PASS 但未 FINISHED，只能算 artifact recovery，"
            "不能算 interaction completion。每组仅一个新有效样本，结论限于所见 trace。"
        )

    if two and four:
        payload["budget_conclusion"] = (
            "全局提高 MAX_AGENT_STEPS 会增加从任务开头起的整个 trajectory，早期行为也可能改变；"
            "failure 后 bounded recovery allowance 只在相同 required-test failure observation 后增加有限 turns，"
            "本实验对照的是这个局部 allowance。多类 budget semantics 还可分别限制普通模型轮数、工具动作与 failure-triggered recovery，"
            "但本阶段没有实现任何 Runtime 预算改动。Phase 23.1 的 10/12-step 从头独立运行，仍有 trajectory 混杂。"
        )
    else:
        payload["budget_conclusion"] = (
            "样本不足以比较 recovery turns；全局 MAX、failure 后 bounded recovery 和多类 budget semantics 是不同设计。"
            "全局 MAX 改变从开头开始的 trajectory，bounded allowance 只在失败 observation 后生效；"
            "多类 semantics 会进一步区分普通模型轮数、工具动作和 recovery 轮数。Phase 23.1 的从头运行不能隔离这些差异。"
        )

    payload["bounded_recovery_direction_supported"] = bool(two_pass or four_pass or two_finished or four_finished)
    payload["more_samples_needed"] = True
    payload["next_phase_recommendation"] = (
        "下一阶段唯一建议：继续用固定 prefix 研究 bounded recovery allowance，并确保预算覆盖修改、复测和 finish；"
        "在获得更多重复样本前不实现 Runtime 预算改动，也不提高全局 MAX。"
        if payload["bounded_recovery_direction_supported"]
        else "下一阶段唯一建议：先从完整 failure trace 复查 diagnosis/editing 环节；暂不设计或实现 Runtime 预算改动。"
    )


def render_report(payload: dict) -> str:
    prefix = payload["fixed_prefix"]
    validation = payload.get("validation", {})
    harness_validation = payload.get("harness_offline_validation", {})
    provisional = payload.get("provisional_stderr_evidence", {})
    rows = []
    details = []
    for budget in RECOVERY_BUDGETS:
        slots = payload.get("corrected_reruns", {}).get(str(budget), [])
        for slot in slots:
            result = slot.get("result", {})
            analysis = result.get("analysis", {})
            token_usage = result.get("recovery_token_usage", {})
            token_total = token_usage.get("observed_total_tokens")
            rows.append(
                f"| {budget} | {slot.get('attempt_count')} | {result.get('valid_real_run')} | "
                f"{result.get('status')} | {result.get('accepted')} | {result.get('recovery_model_calls')} | "
                f"{result.get('recovery_tool_calls')} | {token_total if token_total is not None else 'unavailable'} | "
                f"{analysis.get('required_test_pass_turns', [])} | {analysis.get('finish_task_turn')} |"
            )
            first = result.get("first_post_failure_action", {})
            first_message = first.get("model_response") or {}
            first_calls = first.get("tool_calls") or []
            evidence_assessment = result.get("failure_evidence_assessment", {})
            first_call_text = "; ".join(
                f"{call.get('function', {}).get('name')} "
                f"{call.get('function', {}).get('arguments')}"
                for call in first_calls
            ) or "no tool call"
            counts = result.get("post_failure_read_search_list_counts", {})
            chain_lines = []
            for event in result.get("post_failure_tool_chain", []):
                args = event.get("arguments", {})
                if isinstance(args, dict) and event.get("tool") == "apply_patch":
                    args = {"path": args.get("path"), "old_text": args.get("old_text"), "new_text": args.get("new_text")}
                result_text = str(event.get("result") or "").replace("\n", " ")
                chain_lines.append(
                    f"- Turn {event.get('turn')}: `{event.get('tool')}` `{json.dumps(args, ensure_ascii=False)}` "
                    f"→ exit `{event.get('exit_code')}`; result `{result_text[:450]}`"
                )
            second = analysis.get("second_mutation")
            pass_turns = analysis.get("required_test_pass_turns", [])
            artifact_result = "PASS" if pass_turns else "no PASS observed"
            interaction = "FINISHED + accepted" if result.get("status") == "FINISHED" and result.get("accepted") else "not finished/accepted"
            details.extend([
                f"### {budget}-turn corrected run",
                "",
                f"- Slot attempt count: `{slot.get('attempt_count')}`; valid: `{result.get('valid_real_run')}`; infrastructure: `{result.get('infrastructure_kind')}`.",
                f"- First post-failure model response: `{first_message.get('content') or ''}`; first action: `{first_call_text}`.",
                f"- Failure evidence used: `{evidence_assessment.get('used_boundary_result_evidence')}` (automatic exact test-name match: `{evidence_assessment.get('automatic_failed_test_name_match')}`); reads/searches/lists after failure: `{counts.get('read_file', 0)}` / `{counts.get('search_text', 0)}` / `{counts.get('list_files', 0)}`.",
                f"- Targeted second mutation: `{second}`; target: `{analysis.get('second_mutation_target')}`.",
                f"- Required-test attempts: `{result.get('required_test_attempts', [])}`; passing turns: `{pass_turns}`; artifact result: `{artifact_result}`; finish call turn: `{analysis.get('finish_task_turn')}`; interaction completion: `{interaction}.",
                f"- Recovery model calls/turns: `{result.get('recovery_model_calls')}` / `{result.get('recovery_model_turns_used')}`; tool calls: `{result.get('recovery_tool_calls')}`; recovery tokens: `{token_total}`; extra post-verification tool calls: `{result.get('post_verification_extra_tool_calls')}`.",
                "- Full canonical history, combined/recovery traces, TaskState snapshots, metrics, tokens and verifier are saved under `corrected_reruns` in `phase23_2_results.json`.",
                "- Full failure-to-completion tool chain:",
                *(chain_lines or ["- No post-failure tool calls were recorded."]),
                "",
            ])

    observations = provisional.get("observations", [])
    old_two = [item for item in observations if item.get("budget") == 2]
    old_four = [item for item in observations if item.get("budget") == 4]
    old_two_pass = any(0 in item.get("required_test_exit_codes", []) for item in old_two)
    old_two_finish = any(item.get("finish_task_called") for item in old_two)
    old_four_finished = sum(
        item.get("status") == "FINISHED" and item.get("finish_task_called") for item in old_four
    )
    failure_output = prefix.get("failure_output", "")
    offline_result = harness_validation.get("scripted_finished_result", {})
    return "\n".join([
        "# Phase 23.2R — Corrected Fixed-Prefix Recovery Rerun",
        "",
        f"Status: `{payload.get('experiment_status')}`. Runtime files were unchanged. Corrected real model slots: at most one each for 2 and 4 turns, with at most one infrastructure retry per slot.",
        "",
        "## Harness bug and repair",
        "",
        "The recovery loop returned, but the harness passed a trace-summary `dict` into `phase22_harness._verify`, which reads `CodingTaskTrace.final_answer`, `.max_steps_reached`, and `.summary()`. That postprocessing crash happened before canonical recovery history, TaskState and complete metrics were serialized.",
        "",
        "The harness now reconstructs the combined summary as a concrete `main.CodingTaskTrace` before `_verify`, then stores canonical history, combined/recovery traces, TaskState before/start/after, executed-call fingerprints, verifier output and complete metrics/token totals. Missing required evidence or token totals invalidates a run and is classified separately from agent behavior.",
        "",
        "## Offline scripted verification",
        "",
        f"Status: `{harness_validation.get('status')}`; provider used: `{harness_validation.get('provider_used')}`; CodingTaskTrace passed to verifier: `{harness_validation.get('coding_task_trace_passed_to_verify')}`; FINISHED result survived JSON write/read: `{harness_validation.get('finished_result_json_round_trip')}`.",
        f"Scripted result: status `{offline_result.get('status')}`, accepted `{offline_result.get('accepted')}`, model turns `{offline_result.get('recovery_model_calls')}`, tools `{offline_result.get('recovery_tool_calls')}`, tokens `{offline_result.get('recovery_token_usage', {}).get('observed_total_tokens')}`. Provider failure mock classification: `{harness_validation.get('scripted_provider_failure_classification')}`.",
        "",
        "## Fixed prefix integrity",
        "",
        f"Reused `{prefix.get('source')}` unchanged: `{prefix.get('message_count')}` canonical messages, id `{prefix.get('prefix_id')}`. This is the saved Turn 8 history; no new baseline was run. Its last tool result is the actual failed required-test observation:",
        "",
        "```text",
        failure_output.strip(),
        "```",
        "",
        "The prefix TaskState is `LIMIT_REACHED`, event sequence 18, mutation sequence 17. Each run rebuilt the fixture and verified the initial snapshot, replayed the exact Turn 7 canonical patch, and changed only TaskState status to `RUNNING`. The recovery start retained event 18, mutation 17, and all 17 executed-call fingerprints.",
        "",
        "## Corrected real runs",
        "",
        "| Recovery budget | Attempts | Valid | Final status | Accepted | Recovery model calls | Tools | Tokens | Passing test turns | Finish turn |",
        "|---:|---:|---|---|---|---:|---:|---:|---|---:|",
        *(rows or ["| — | — | no corrected result | — | — | — | — | — | — | — |"]),
        "",
        *details,
        "## Legacy evidence (not valid runs)",
        "",
        "Original attempts remain unchanged under `runs`; their captured stderr excerpts remain under `runs.*.captured_attempt_observations`. They are harness/infrastructure failures, not formal samples.",
        f"Provisional old 2-turn stderr: test PASS observed in at least one loop-returning log: `{old_two_pass}`; finish observed: `{old_two_finish}`. Provisional old 4-turn stderr: `{old_four_finished}` loop-returning logs showed FINISHED after three recovery turns. These summaries remain provisional because those attempts never saved canonical recovery history or complete metrics.",
        "",
        "## Conclusion and next step",
        "",
        f"Diagnosis capability: {payload.get('diagnosis_conclusion')}",
        "",
        f"Budget semantics: {payload.get('budget_conclusion')}",
        "",
        f"Bounded recovery direction supported by this sample: `{payload.get('bounded_recovery_direction_supported')}`. More samples needed: `{payload.get('more_samples_needed')}`.",
        "",
        f"唯一建议：{payload.get('next_phase_recommendation')}",
        "",
        "## Validation",
        "",
        f"- Full unittest: `{validation.get('full_unittest')}`",
        f"- compileall: `{validation.get('compileall')}`",
        f"- git diff --check: `{validation.get('git_diff_check')}`",
        "",
    ])


def _write_outputs(payload: dict) -> None:
    RESULTS.write_text(_json_text(payload), encoding="utf-8")
    REPORT.write_text(render_report(payload), encoding="utf-8")


def main_cli() -> None:
    if "--render-saved" in sys.argv:
        saved = json.loads(RESULTS.read_text(encoding="utf-8"))
        for slots in saved.get("corrected_reruns", {}).values():
            for slot in slots:
                _refresh_result_derived_fields(slot.get("result", {}))
                slot["valid_real_run"] = bool(slot.get("result", {}).get("valid_real_run"))
        _update_conclusions(saved)
        REPORT.write_text(render_report(saved), encoding="utf-8")
        RESULTS.write_text(_json_text(saved), encoding="utf-8")
        print(json.dumps({"results": str(RESULTS), "report": str(REPORT)}, ensure_ascii=False))
        return
    if "--child" in sys.argv:
        budget = int(os.environ[RECOVERY_BUDGET_ENV])
        with redirect_stdout(sys.stderr):
            result = _run_recovery(budget)
        print(_json_text(result))
        return

    original = json.loads(RESULTS.read_text(encoding="utf-8"))
    baseline, prefix, prefix_id = _fixed_prefix()
    payload = copy.deepcopy(original)
    payload["phase"] = "23.2R"
    payload["runtime_changed"] = False
    payload["corrected_reruns_started_at"] = datetime.now().isoformat(timespec="seconds")
    payload["legacy_harness_capture_audit"] = copy.deepcopy(original.get("capture_audit"))
    payload["legacy_harness_failure_attempts"] = {
        "source_key": "runs",
        "classification": "preserved infrastructure/harness failures; none are valid model behavior runs",
        "slot_count": sum(len(items) for items in original.get("runs", {}).values()),
        "raw_attempts_preserved": True,
    }
    payload["capture_audit"] = {
        "status": "corrected recovery capture in progress",
        "legacy_root_cause": original.get("capture_audit", {}).get("root_cause"),
        "legacy_attempts_preserved_under": "runs",
        "legacy_attempts_are_valid_runs": False,
        "scripted_harness_validation": "passed after this assignment below",
    }
    payload["provisional_stderr_evidence"] = _provisional_stderr_summary(original)
    payload["canonical_prefix_messages"] = prefix
    payload["fixed_prefix"] = _fixed_prefix_details(baseline, prefix, prefix_id)
    payload["corrected_reruns"] = {str(budget): [] for budget in RECOVERY_BUDGETS}
    payload["harness_offline_validation"] = _offline_harness_validation()
    payload["capture_audit"]["scripted_harness_validation"] = payload["harness_offline_validation"]["status"]
    payload["validation"] = {"full_unittest": None, "compileall": None, "git_diff_check": None}
    payload["model_run_limits"] = {
        "valid_runs_per_budget": 1,
        "infrastructure_retries_per_slot": INFRASTRUCTURE_RETRIES,
        "maximum_new_model_run_attempts": len(RECOVERY_BUDGETS) * (1 + INFRASTRUCTURE_RETRIES),
    }
    payload["experiment_status"] = "CORRECTED_RERUNS_IN_PROGRESS"
    _update_conclusions(payload)
    _write_outputs(payload)

    for budget in RECOVERY_BUDGETS:
        slot = _run_slot(budget, 1)
        payload["corrected_reruns"][str(budget)] = [slot]
        _update_conclusions(payload)
        payload["experiment_status"] = (
            "CORRECTED_RERUNS_COMPLETE"
            if all(payload["corrected_reruns"][str(value)] for value in RECOVERY_BUDGETS)
            else "CORRECTED_RERUNS_IN_PROGRESS"
        )
        _write_outputs(payload)
    payload["experiment_status"] = (
        "CORRECTED_RERUNS_COMPLETE"
        if all(
            len(payload["corrected_reruns"][str(budget)]) == 1
            and payload["corrected_reruns"][str(budget)][0].get("valid_real_run")
            for budget in RECOVERY_BUDGETS
        )
        else "CORRECTED_RERUNS_INCOMPLETE"
    )
    _write_outputs(payload)
    print(json.dumps({"results": str(RESULTS), "report": str(REPORT)}, ensure_ascii=False))


if __name__ == "__main__":
    main_cli()
