"""One live continuation from the saved Phase 23.2 boundary, using Runtime grace."""

from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from openai.types.chat import ChatCompletionMessage

import acceptance
import config
import main
import tools
from . import phase22_5, phase22_harness as harness, phase23_2, phase23_budget


OUTPUT = Path(__file__).with_name("phase24_1r_results.json")


def _saved_replies(prefix: list[dict], mode: str) -> list[main.ModelReply]:
    assistants = [copy.deepcopy(message) for message in prefix if message["role"] == "assistant"]
    if len(assistants) != 8:
        raise ValueError("Fixed prefix does not contain eight assistant turns")
    if mode == "PASS":
        saved = json.loads(phase23_2.RESULTS.read_text(encoding="utf-8"))
        correction = saved["corrected_reruns"]["2"][0]["result"]["recovery_history"][0]
        calls = correction.get("tool_calls") or []
        if len(calls) != 1 or calls[0]["function"]["name"] != "apply_patch":
            raise ValueError("Saved successful correction is not one patch")
        assistants[6]["tool_calls"].append(copy.deepcopy(calls[0]))
    return [
        main.ModelReply(ChatCompletionMessage.model_validate(message), "tool_calls", None, None, None)
        for message in assistants
    ]


def _run(mode: str) -> dict:
    if mode not in {"FAIL", "PASS"}:
        raise ValueError(mode)
    baseline, prefix, prefix_id = phase23_2._fixed_prefix()
    if prefix_id != "caecc4acb2ce6820":
        raise ValueError("Fixed prefix id changed")
    saved_last = prefix[-1]["content"]
    if main.trace_exit_code(saved_last) != "1" or "test_one_percent_boundary" not in saved_last:
        raise ValueError("Saved prefix does not end in the real required-test failure")

    config.load_env_file()
    settings = config.load_config()
    if settings.model != baseline["model"]:
        raise ValueError("Provider model differs from the fixed prefix")
    if config.get_context_mode() != "WRITE_ONLY" or config.get_approval_mode() != "ALLOW":
        raise ValueError("Context or approval mode differs from the fixed prefix")

    with tempfile.TemporaryDirectory(prefix=f"phase24-1r-{mode.lower()}-") as directory:
        root = Path(directory) / "workspace"
        spec = phase22_5.setup("E", root, register=True)
        contract = acceptance.CodingTaskContract.from_dict(phase22_5.contract(spec))
        before = acceptance.snapshot_workspace(root)
        messages = harness._model_messages(contract)
        if messages != prefix[:2] or before != baseline["task_state"]["initial_snapshot"]:
            raise ValueError("Rebuilt prompt or fixture differs from the fixed prefix")
        replies = _saved_replies(prefix, mode)
        state = acceptance.TaskState(initial_snapshot=dict(before))
        trace = main.CodingTaskTrace()
        executed: set[tuple[str, str]] = set()
        live_replies: list[dict] = []
        runtime_errors: list[str] = []
        boundary: dict | None = None
        client = None
        real_ask = main.ask

        def recording_ask(llm_client, model, history):
            nonlocal boundary
            if replies:
                return replies.pop(0)
            if boundary is None:
                test_events = [event for event in trace.events if event.get("turn") == 8 and event.get("tool") == "run_command"]
                boundary = {
                    "event_seq": state.event_seq,
                    "last_mutation_event_seq": state.last_mutation_event_seq,
                    "last_successful_exact_required_test_seq": state.last_successful_exact_required_test_seq,
                    "recovery_grace": trace.recovery_grace,
                    "final_model_call_limit": trace.final_model_call_limit,
                    "model_calls": trace.model_calls,
                    "message_count": len(messages),
                    "test_exit_code": test_events[-1]["exit_code"] if test_events else None,
                }
                expected_exit = "1" if mode == "FAIL" else "0"
                expected_seq = 18 if mode == "FAIL" else 19
                expected_mutation = 17 if mode == "FAIL" else 18
                expected_limit = main.HARD_CEILING if mode == "FAIL" else 9
                if (
                    boundary["model_calls"] != 8
                    or boundary["event_seq"] != expected_seq
                    or boundary["last_mutation_event_seq"] != expected_mutation
                    or boundary["test_exit_code"] != expected_exit
                    or boundary["recovery_grace"] != mode
                    or boundary["final_model_call_limit"] != expected_limit
                    or boundary["message_count"] != (28 if mode == "FAIL" else 29)
                ):
                    raise ValueError(f"Runtime boundary differs from expected {mode}: {boundary}")
            reply = real_ask(llm_client, model, history)
            live_replies.append({
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
            provider_environment = phase23_budget._provider_child_environment(os.environ)
            with patch.dict(os.environ, provider_environment, clear=True), patch.object(main, "ask", side_effect=recording_ask), redirect_stdout(io.StringIO()):
                client = main.build_client(settings).with_options(max_retries=0)
                try:
                    main.run_agent_loop(
                        client, settings.model, messages, replies.pop(0), executed,
                        main.always_allow, trace=trace,
                        required_test=harness._required_test(contract),
                        contract=contract, task_state=state,
                    )
                except Exception as exc:
                    runtime_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if client is not None:
                client.close()
            main.WORKSPACE_DIR = previous_main_workspace
            tools.WORKSPACE_DIR = previous_tools_workspace

        verdict = harness._verify(contract, root, before, state, messages, trace)
        recovery_events = [
            event for event in trace.events
            if event.get("turn", 0) > 8 and event.get("action") == "tool_call"
        ]
        test_events = [
            event for event in trace.events
            if event.get("turn", 0) >= 8 and event.get("tool") == "run_command"
        ]
        result = {
            "mode": mode,
            "fixed_prefix_id": prefix_id,
            "saved_prefix_failure": "test_one_percent_boundary",
            "boundary": boundary,
            "valid_real_run": boundary is not None and not runtime_errors,
            "infrastructure_failure": bool(runtime_errors),
            "runtime_errors": runtime_errors,
            "recovery_model_calls": max(0, trace.model_calls - 8),
            "recovery_tool_calls": len(recovery_events),
            "total_model_calls": trace.model_calls,
            "recovery_token_usage": live_replies,
            "recovery_total_tokens": sum(item["total_tokens"] or 0 for item in live_replies),
            "mutations": [event for event in recovery_events if event["tool"] in {"write_file", "apply_patch"}],
            "test_attempts": test_events,
            "finish_attempts": [event for event in recovery_events if event["tool"] == "finish_task"],
            "status": state.status.value,
            "recovery_grace": trace.recovery_grace,
            "final_model_call_limit": trace.final_model_call_limit,
            "accepted": verdict["accepted"],
            "artifact_passed": verdict["artifact_passed"],
            "interaction_completed": verdict["interaction_completed"],
            "agent_self_verified": verdict["agent_self_verified"],
            "verifier": verdict,
            "task_state": state.as_dict(),
            "trace": trace.summary(),
            "canonical_history": messages,
        }
        return result


if __name__ == "__main__":
    mode = sys.argv[1]
    result = _run(mode)
    saved = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    saved[mode] = result
    OUTPUT.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "mode", "fixed_prefix_id", "boundary", "infrastructure_failure", "runtime_errors",
        "recovery_model_calls", "recovery_tool_calls", "recovery_total_tokens", "status",
        "recovery_grace", "final_model_call_limit", "accepted", "artifact_passed",
        "interaction_completed", "agent_self_verified",
    )}, ensure_ascii=False))
