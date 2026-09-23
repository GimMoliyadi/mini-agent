"""Phase 22 realistic coding-task evaluation harness.

The harness deliberately reuses the production Agent loop, Coding Contract,
Finish Gate, and independent verifier.  Fixtures and ground truth live in
``phase22_fixtures``; only the issue, contract, and exact required test enter
the model prompt.  The default command performs one provider preflight, then
runs each of six tasks once in a fresh fixture workspace.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
RESULTS_PATH = EVAL_DIR / "phase22_results.json"
REPORT_PATH = EVAL_DIR / "phase22_report.md"
PYTHON_BIN = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
CHILD_TIMEOUT_SECONDS = 900

sys.path.insert(0, str(PROJECT_ROOT))

import acceptance  # noqa: E402
import config  # noqa: E402
import main  # noqa: E402
import tools  # noqa: E402
from .phase22_fixtures import (  # noqa: E402
    TASKS,
    apply_ground_truth_fix,
    build_fixture,
    coding_contract,
    get_task,
    ground_truth_patch,
    validate_fixture,
)


_SEARCH_FILE = re.compile(r"^([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+):\d+$")
_LIST_FILE = re.compile(r"^\[f\]\s+(.+?)(?:\s{2}\(.+\))?$")
_MUTATION_TOOLS = {"write_file", "apply_patch"}
_NAVIGATION_TOOLS = {"list_files", "search_text", "read_file"}


def _task_values():
    """Return each real task exactly once, without A-F lookup aliases."""
    return list(TASKS.values())


def _required_test(contract: acceptance.CodingTaskContract) -> tuple[str, tuple[str, ...], str]:
    test = contract.test_command
    return test.command, test.args, test.cwd


def _call_arguments(call: dict) -> dict:
    try:
        arguments = json.loads(call["function"].get("arguments") or "{}")
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return arguments if isinstance(arguments, dict) else {}


def _bounded(value: str, limit: int = 500) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _exit_code(result: str) -> int | None:
    value = main.trace_exit_code(result)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _successful(result: str) -> bool:
    return bool(result) and not (
        result.startswith(main.TOOL_FAILURE_PREFIX)
        or result.startswith(main.APPROVAL_DENIED_PREFIX)
        or main.is_duplicate_notice(result)
    )


def _search_candidates(result: str) -> set[str]:
    found = set()
    for line in result.splitlines():
        match = _SEARCH_FILE.match(line.strip())
        if match:
            found.add(match.group(1).replace("\\", "/"))
    return found


def _list_file_candidates(result: str, path: object) -> set[str]:
    """Resolve files named in a one-level ``list_files`` response."""
    parent = str(path or ".").replace("\\", "/").strip("/")
    found = set()
    for line in result.splitlines():
        match = _LIST_FILE.match(line.strip())
        if not match:
            continue
        name = match.group(1).strip()
        found.add(name if parent in {"", "."} else f"{parent}/{name}")
    return found


def _history_tool_events(messages: list[dict], trace: dict) -> list[dict]:
    """Join canonical tool results with their compact Trace counterparts."""
    trace_tools = [event for event in trace.get("events", []) if event.get("action") == "tool_call"]
    trace_index = 0
    model_turn = 0
    events: list[dict] = []
    index = 0

    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant":
            index += 1
            continue

        model_turn += 1
        calls = message.get("tool_calls") or []
        result_index = index + 1
        for call in calls:
            result = ""
            if result_index < len(messages):
                result_message = messages[result_index]
                if (
                    result_message.get("role") == "tool"
                    and result_message.get("tool_call_id") == call.get("id")
                ):
                    result = result_message.get("content") or ""
                    result_index += 1
            trace_event = trace_tools[trace_index] if trace_index < len(trace_tools) else {}
            trace_index += 1
            events.append(
                {
                    "turn": model_turn,
                    "call": call,
                    "tool": call.get("function", {}).get("name", ""),
                    "arguments": _call_arguments(call),
                    "raw_arguments": call.get("function", {}).get("arguments") or "{}",
                    "result": result,
                    "exit_code": _exit_code(result),
                    "event_seq": trace_event.get("event_seq"),
                    "classification": trace_event.get("classification"),
                    "approval": trace_event.get("approval"),
                }
            )
        index = result_index if calls else index + 1

    return events


def _matches_required_test(event: dict, required: tuple[str, tuple[str, ...], str]) -> bool:
    if event["tool"] != "run_command":
        return False
    command, args, cwd = required
    arguments = event["arguments"]
    actual_args = arguments.get("args")
    if not isinstance(actual_args, list):
        return False
    return (
        arguments.get("command") == command
        and tuple(actual_args) == args
        and arguments.get("cwd", ".") == cwd
    )


def _has_required_test_retry_cycle(
    events: list[dict], required: tuple[str, tuple[str, ...], str]
) -> bool:
    """Detect only failed exact-test -> mutation -> passed exact-test sequences."""
    for failed_index, event in enumerate(events):
        if not (
            _matches_required_test(event, required)
            and event["exit_code"] not in {None, 0}
        ):
            continue
        mutation_index = next(
            (
                index
                for index in range(failed_index + 1, len(events))
                if events[index]["tool"] in _MUTATION_TOOLS
                and _successful(events[index]["result"])
            ),
            None,
        )
        if mutation_index is None:
            continue
        if any(
            _matches_required_test(later, required) and later["exit_code"] == 0
            for later in events[mutation_index + 1 :]
        ):
            return True
    return False


def collect_metrics(
    messages: list[dict],
    trace: dict,
    task,
    contract: acceptance.CodingTaskContract,
    state: acceptance.TaskState,
    verdict: dict,
) -> dict:
    """Extract deterministic Phase 22 metrics from canonical history and state."""
    spec = get_task(task)
    events = _history_tool_events(messages, trace)
    required = _required_test(contract)
    relevant_files = set(spec.ground_truth.relevant_files)
    source_relevant_files = {path for path in relevant_files if not path.startswith("tests/")}
    discovered_relevant: set[str] = set()
    source_files_read: set[str] = set()
    first_source_file_read_index = None
    first_correct_file_turn = None
    first_correct_file_method = None
    before_correct = {name: 0 for name in _NAVIGATION_TOOLS}
    first_navigation_tool = None
    read_fingerprints: set[str] = set()
    redundant_reads = 0
    list_calls_after_correct = 0
    mutation_indexes: list[int] = []
    correct_mutations = 0
    wrong_mutations = 0
    modified_tests = False
    required_attempts: list[dict] = []
    wrong_tests = 0
    finish_indexes: list[int] = []
    tool_failures = 0
    chain = []

    for index, event in enumerate(events):
        tool = event["tool"]
        arguments = event["arguments"]
        result = event["result"]
        if result.startswith(main.TOOL_FAILURE_PREFIX):
            tool_failures += 1

        candidates: set[str] = set()
        if tool in _NAVIGATION_TOOLS:
            if first_navigation_tool is None:
                first_navigation_tool = tool
            if tool == "search_text":
                candidates = _search_candidates(result)
            elif tool == "list_files" and _successful(result):
                candidates = _list_file_candidates(result, arguments.get("path"))
            elif tool == "read_file" and _successful(result):
                path = str(arguments.get("path", "")).replace("\\", "/")
                if path:
                    candidates = {path}
                    if path in source_relevant_files:
                        source_files_read.add(path)
                        if first_source_file_read_index is None:
                            first_source_file_read_index = index

            hit = candidates & relevant_files
            discovered_relevant.update(hit)
            if hit and first_correct_file_turn is None:
                first_correct_file_turn = event["turn"]
                first_correct_file_method = tool
                before_correct = {
                    name: sum(1 for prior in events[:index] if prior["tool"] == name)
                    for name in _NAVIGATION_TOOLS
                }
            if tool == "list_files" and first_correct_file_turn is not None:
                list_calls_after_correct += 1

        if tool == "read_file":
            fingerprint = event["raw_arguments"]
            if fingerprint in read_fingerprints:
                redundant_reads += 1
            read_fingerprints.add(fingerprint)

        if tool in _MUTATION_TOOLS and _successful(result):
            mutation_indexes.append(index)
            path = str(arguments.get("path", "")).replace("\\", "/")
            if path in spec.ground_truth.intended_changed_files:
                correct_mutations += 1
            else:
                wrong_mutations += 1
            modified_tests = modified_tests or path.startswith("tests/")

        if tool == "run_command":
            if _matches_required_test(event, required):
                required_attempts.append(event)
            else:
                wrong_tests += 1

        if tool == "finish_task":
            finish_indexes.append(index)

        chain.append(
            {
                "turn": event["turn"],
                "tool": tool,
                "arguments": _bounded(event["raw_arguments"], 240),
                "result": _bounded(result, 900),
                "exit_code": event["exit_code"],
                "event_seq": event["event_seq"],
                "classification": event["classification"],
            }
        )

    successful_required = [event for event in required_attempts if event["exit_code"] == 0]
    final_success_index = next(
        (index for index in range(len(events) - 1, -1, -1) if events[index] in successful_required),
        None,
    )
    required_failures_before_success = 0
    if successful_required:
        first_success = successful_required[0]
        for event in required_attempts:
            if event is first_success:
                break
            required_failures_before_success += int(event["exit_code"] != 0)
    else:
        required_failures_before_success = sum(event["exit_code"] != 0 for event in required_attempts)

    last_mutation = state.last_mutation_event_seq
    post_mutation_exact_test_pass = any(
        event["exit_code"] == 0
        and (
            last_mutation is None
            or event["event_seq"] is not None
            and event["event_seq"] > last_mutation
        )
        for event in required_attempts
    )
    finish_after_fresh_verification = bool(
        final_success_index is not None
        and post_mutation_exact_test_pass
        and any(index > final_success_index for index in finish_indexes)
    )
    next_finish = next(
        (index for index in finish_indexes if final_success_index is not None and index > final_success_index),
        None,
    )
    post_verification_extra_tool_calls = (
        sum(1 for _ in events[final_success_index + 1 : next_finish])
        if final_success_index is not None and next_finish is not None
        else None
    )
    sequence_tools = [event["tool"] for event in events]
    has_retry_cycle = _has_required_test_retry_cycle(events, required)
    first_mutation_index = mutation_indexes[0] if mutation_indexes else None
    mutation_before_source_context = bool(
        first_mutation_index is not None
        and (
            first_source_file_read_index is None
            or first_mutation_index < first_source_file_read_index
        )
    )

    return {
        "task_id": spec.task_id,
        "task_code": spec.code,
        "status": state.status.value,
        "accepted": verdict["accepted"],
        "artifact_passed": verdict["artifact_passed"],
        "interaction_completed": verdict["interaction_completed"],
        "agent_self_verified": verdict["agent_self_verified"],
        "model_calls": trace["model_calls"],
        "tool_calls": trace["tool_calls"],
        "total_tokens": trace["total_tokens"],
        "prompt_tokens": trace["prompt_tokens"],
        "completion_tokens": trace["completion_tokens"],
        "list_files_calls": trace["list_files_calls"],
        "search_text_calls": trace["search_text_calls"],
        "read_file_calls": trace["read_file_calls"],
        "apply_patch_calls": trace["apply_patch_calls"],
        "write_file_calls": trace["write_file_calls"],
        "run_command_calls": trace["run_command_calls"],
        "finish_task_calls": trace["finish_task_calls"],
        "duplicate_blocked": trace["duplicate_blocked"],
        "policy_rejected": trace["policy_rejected"],
        "tool_failures": tool_failures,
        "max_steps_reached": trace["max_steps_reached"],
        "finish_attempts": len(state.finish_attempts),
        "finish_rejections": trace["finish_rejections"],
        "finish_attempt_details": list(state.finish_attempts),
        "last_mutation_event_seq": state.last_mutation_event_seq,
        "last_successful_exact_required_test_seq": state.last_successful_exact_required_test_seq,
        "changed_files": verdict["changed_files"],
        "unexpected_changes": verdict["unexpected_changes"],
        "required_test_final_exit_code": verdict["final_test_exit_code"],
        "required_test_attempts": len(required_attempts),
        "required_test_failures_before_success": required_failures_before_success,
        "post_mutation_exact_test_pass": post_mutation_exact_test_pass,
        "finish_after_fresh_verification": finish_after_fresh_verification,
        "post_verification_extra_tool_calls": post_verification_extra_tool_calls,
        "first_correct_file_turn": first_correct_file_turn,
        "first_correct_file_method": first_correct_file_method,
        "search_first": first_navigation_tool == "search_text",
        "list_first": first_navigation_tool == "list_files",
        "navigation_before_first_correct": before_correct,
        "relevant_files_seen": sorted(discovered_relevant),
        "source_relevant_files_read": sorted(source_files_read),
        "first_mutation_turn": events[mutation_indexes[0]]["turn"] if mutation_indexes else None,
        "mutation_count": len(mutation_indexes),
        "correct_file_mutations": correct_mutations,
        "wrong_file_mutations": wrong_mutations,
        "modified_tests": modified_tests,
        "mutation_before_source_context": mutation_before_source_context,
        "mutation_test_mutation_final_test": has_retry_cycle,
        "productive_tool_calls": trace["productive_calls"],
        "redundant_reads": redundant_reads,
        "failed_commands": trace["failed_commands"],
        "wrong_tests": wrong_tests,
        "list_calls_after_first_correct_file": list_calls_after_correct,
        "tool_chain": chain,
        "ground_truth": spec.ground_truth.as_dict(),
        "task_flags": dict(spec.ground_truth.flags),
        "sequence_tools": sequence_tools,
    }


def _agent_ran_required_test(messages: list[dict], trace: dict, contract) -> bool:
    return any(
        event["exit_code"] == 0
        for event in _history_tool_events(messages, trace)
        if _matches_required_test(event, _required_test(contract))
    )


def _model_messages(contract: acceptance.CodingTaskContract) -> list[dict]:
    return [
        {"role": "system", "content": main.SYSTEM_PROMPT},
        {
            "role": "user",
            "content": contract.instruction + "\n\n" + acceptance.coding_task_guidance(contract),
        },
    ]


def _verify(contract, root: Path, before: dict[str, str], state, messages, trace) -> dict:
    return acceptance.verify_contract(
        contract,
        root,
        before,
        task_state=state,
        agent_final_answer_present=trace.final_answer is not None,
        agent_ran_required_test=_agent_ran_required_test(messages, trace.summary(), contract),
        max_steps_reached=trace.max_steps_reached,
        runtime_exception=state.unresolved_runtime_error,
    )


def run_scripted_case(root: str | Path, task, replies: list[main.ModelReply]) -> dict:
    """Run a real Contract/Finish loop with scripted replies and no provider."""
    if not replies:
        raise ValueError("scripted case needs at least one model reply")
    root = Path(root).resolve()
    spec = get_task(task)
    contract = acceptance.CodingTaskContract.from_dict(coding_contract(spec))
    before = acceptance.snapshot_workspace(root)
    state = acceptance.TaskState(initial_snapshot=dict(before))
    trace = main.CodingTaskTrace()
    messages = _model_messages(contract)
    original_main_workspace = main.WORKSPACE_DIR
    original_tools_workspace = tools.WORKSPACE_DIR

    main.WORKSPACE_DIR = root
    tools.WORKSPACE_DIR = root
    try:
        with patch.object(main, "ask", side_effect=replies[1:]):
            main.run_agent_loop(
                None,
                "scripted-model",
                messages,
                replies[0],
                set(),
                main.always_allow,
                trace=trace,
                required_test=_required_test(contract),
                contract=contract,
                task_state=state,
            )
        trace_summary = trace.summary()
        verdict = _verify(contract, root, before, state, messages, trace)
        return {
            "task_id": spec.task_id,
            "model": "scripted-model",
            "context_mode": "WRITE_ONLY",
            "approval_mode": "ALLOW",
            "max_agent_steps": config.MAX_AGENT_STEPS,
            "metrics": collect_metrics(messages, trace_summary, spec, contract, state, verdict),
            "trace": trace_summary,
            "task_state": state.as_dict(),
            "acceptance": verdict,
            "canonical_history": messages,
        }
    finally:
        main.WORKSPACE_DIR = original_main_workspace
        tools.WORKSPACE_DIR = original_tools_workspace


def _runtime_error_kind(error: str) -> str:
    text = error.casefold()
    if "missing" in text or "configuration" in text or "systemexit" in text:
        return "configuration"
    if any(
        token in text
        for token in (
            "api",
            "connection",
            "connect",
            "timeout",
            "authentication",
            "rate",
            "provider",
            "internalservererror",
            "servererror",
            "error code: 5",
            "status code: 5",
            "http 5",
        )
    ):
        return "provider"
    return "harness"


def _run_live_task(task_id: str) -> dict:
    """Run one real provider-backed task inside an already-isolated workspace."""
    root = Path(os.environ["AGENT_WORKSPACE"]).resolve()
    spec = get_task(task_id)
    validate_fixture(root)
    contract = acceptance.CodingTaskContract.from_dict(coding_contract(spec))
    before = acceptance.snapshot_workspace(root)
    state = acceptance.TaskState(initial_snapshot=dict(before))
    trace = main.CodingTaskTrace()
    messages = _model_messages(contract)
    runtime_errors: list[str] = []
    client = None
    model_name = os.environ.get("OPENAI_MODEL", "unknown")
    real_ask = main.ask
    provider_error = False

    def recording_ask(llm_client, model, history):
        nonlocal provider_error
        try:
            return real_ask(llm_client, model, history)
        except BaseException:
            provider_error = True
            raise

    main.ask = recording_ask
    try:
        llm_config = config.load_config()
        model_name = llm_config.model
        client = main.build_client(llm_config)
        first_reply = main.ask(client, model_name, messages)
        main.log_reply(1, first_reply)
        main.run_agent_loop(
            client,
            model_name,
            messages,
            first_reply,
            set(),
            main.always_allow,
            trace=trace,
            required_test=_required_test(contract),
            contract=contract,
            task_state=state,
        )
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        runtime_errors.append(error)
        state.status = acceptance.TaskStatus.ERROR
        state.unresolved_runtime_error = error
        trace.set_task_state(state)
    finally:
        main.ask = real_ask
        if client is not None:
            client.close()

    trace_summary = trace.summary()
    verdict = _verify(contract, root, before, state, messages, trace)
    result = {
        "task_id": spec.task_id,
        "task_code": spec.code,
        "model": model_name,
        "context_mode": config.get_context_mode(),
        "approval_mode": config.get_approval_mode(),
        "max_agent_steps": config.MAX_AGENT_STEPS,
        "runtime_errors": runtime_errors,
        "infrastructure_failure": bool(runtime_errors),
        "infrastructure_kind": (
            "provider"
            if runtime_errors and provider_error
            else _runtime_error_kind(runtime_errors[0]) if runtime_errors else None
        ),
        "metrics": collect_metrics(messages, trace_summary, spec, contract, state, verdict),
        "trace": trace_summary,
        "task_state": state.as_dict(),
        "acceptance": verdict,
        "canonical_history": messages,
    }
    return _annotate_result(result)


def _empty_metrics(spec) -> dict:
    """Return the complete metric schema for an unstarted infrastructure run."""
    return {
        "task_id": spec.task_id,
        "task_code": spec.code,
        "status": acceptance.TaskStatus.ERROR.value,
        "accepted": False,
        "artifact_passed": False,
        "interaction_completed": False,
        "agent_self_verified": False,
        "model_calls": 0,
        "tool_calls": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "list_files_calls": 0,
        "search_text_calls": 0,
        "read_file_calls": 0,
        "apply_patch_calls": 0,
        "write_file_calls": 0,
        "run_command_calls": 0,
        "finish_task_calls": 0,
        "duplicate_blocked": 0,
        "policy_rejected": 0,
        "tool_failures": 0,
        "max_steps_reached": False,
        "finish_attempts": 0,
        "finish_rejections": 0,
        "finish_attempt_details": [],
        "last_mutation_event_seq": None,
        "last_successful_exact_required_test_seq": None,
        "changed_files": [],
        "unexpected_changes": [],
        "required_test_final_exit_code": None,
        "required_test_attempts": 0,
        "required_test_failures_before_success": 0,
        "post_mutation_exact_test_pass": False,
        "finish_after_fresh_verification": False,
        "post_verification_extra_tool_calls": None,
        "first_correct_file_turn": None,
        "first_correct_file_method": None,
        "search_first": False,
        "list_first": False,
        "navigation_before_first_correct": {
            "list_files": 0,
            "search_text": 0,
            "read_file": 0,
        },
        "relevant_files_seen": [],
        "source_relevant_files_read": [],
        "first_mutation_turn": None,
        "mutation_count": 0,
        "correct_file_mutations": 0,
        "wrong_file_mutations": 0,
        "modified_tests": False,
        "mutation_before_source_context": False,
        "mutation_test_mutation_final_test": False,
        "productive_tool_calls": 0,
        "redundant_reads": 0,
        "failed_commands": 0,
        "wrong_tests": 0,
        "list_calls_after_first_correct_file": 0,
        "tool_chain": [],
        "ground_truth": spec.ground_truth.as_dict(),
        "task_flags": dict(spec.ground_truth.flags),
        "sequence_tools": [],
    }


def _minimal_infrastructure_result(task_id: str, kind: str, error: str) -> dict:
    spec = get_task(task_id)
    return {
        "task_id": spec.task_id,
        "task_code": spec.code,
        "model": os.environ.get("OPENAI_MODEL", "unknown"),
        "context_mode": os.environ.get("CONTEXT_MODE", "WRITE_ONLY"),
        "approval_mode": os.environ.get("TOOL_APPROVAL_MODE", "ALLOW"),
        "max_agent_steps": config.MAX_AGENT_STEPS,
        "runtime_errors": [error],
        "infrastructure_failure": True,
        "infrastructure_kind": kind,
        "metrics": _empty_metrics(spec),
        "trace": {},
        "task_state": {},
        "acceptance": {"accepted": False, "reasons": [error]},
        "canonical_history": [],
    }


def _run_child_subprocess(task_id: str, root: Path) -> dict:
    environment = {
        **os.environ,
        "AGENT_WORKSPACE": str(root),
        "TOOL_APPROVAL_MODE": "ALLOW",
        "CONTEXT_MODE": "WRITE_ONLY",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    command = [str(PYTHON_BIN), "-m", "eval.phase22_harness", "--child", task_id]
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            env=environment,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return _minimal_infrastructure_result(task_id, "harness", f"child timeout after {CHILD_TIMEOUT_SECONDS}s: {exc}")
    except OSError as exc:
        return _minimal_infrastructure_result(task_id, "harness", f"could not start child process: {exc}")

    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        result = _minimal_infrastructure_result(
            task_id,
            "harness",
            "child process did not return valid JSON",
        )
    else:
        if completed.returncode != 0:
            result = _minimal_infrastructure_result(
                task_id,
                "harness",
                f"child process exited with code {completed.returncode}",
            )
    result["process_exit_code"] = completed.returncode
    result["process_stderr"] = stderr[-6000:]
    return _annotate_result(result)


def _annotate_result(result: dict) -> dict:
    """Apply deterministic failure taxonomy; no LLM judge is involved."""
    if result.get("infrastructure_failure"):
        result["primary_failure"] = "INFRASTRUCTURE"
        result["secondary_failure"] = []
        return result

    metrics = result["metrics"]
    if metrics.get("accepted"):
        result["primary_failure"] = "NONE"
        result["secondary_failure"] = []
        return result

    flags = metrics.get("task_flags", {})
    reasons: list[str] = []
    if metrics.get("policy_rejected"):
        reasons.append("PERMISSION_RUNTIME")
    if metrics.get("mutation_before_source_context"):
        reasons.append("CONTEXT")
    if not metrics.get("relevant_files_seen"):
        reasons.append("NAVIGATION")
    if flags.get("cross_file_understanding") and len(metrics.get("source_relevant_files_read", [])) < 2:
        reasons.append("MULTI_FILE_REASONING")
    if metrics.get("modified_tests") or metrics.get("wrong_file_mutations"):
        reasons.append("EDITING")
    if not metrics.get("required_test_attempts") and metrics.get("run_command_calls"):
        reasons.append("TEST_SELECTION")
    if metrics.get("required_test_attempts") and not metrics.get("post_mutation_exact_test_pass"):
        reasons.append("VERIFICATION")
    if (
        metrics.get("artifact_passed")
        and metrics.get("agent_self_verified")
        and not metrics.get("interaction_completed")
    ):
        reasons.append("COMPLETION")
    if not reasons:
        reasons.append("DIAGNOSIS")
    result["primary_failure"] = reasons[0]
    result["secondary_failure"] = reasons[1:]
    return result


def _provider_preflight() -> dict:
    """Send one minimal, same-provider request before the six live runs."""
    started_at = datetime.now().isoformat(timespec="seconds")
    client = None
    try:
        llm_config = config.load_config()
        client = main.build_client(llm_config)
        response = client.chat.completions.create(
            model=llm_config.model,
            messages=[
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "OK"},
            ],
        )
        content = (response.choices[0].message.content or "").strip()
        accepted = content.upper().rstrip(".") == "OK"
        return {
            "attempted_at": started_at,
            "ok": accepted,
            "model": llm_config.model,
            "reply": content[:80],
            "error": None if accepted else "provider did not return OK",
        }
    except BaseException as exc:
        return {
            "attempted_at": started_at,
            "ok": False,
            "model": os.environ.get("OPENAI_MODEL", "unknown"),
            "reply": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if client is not None:
            client.close()


def _scripted_tool_reply(call_id: str, name: str, arguments: dict) -> main.ModelReply:
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )
    return main.ModelReply(
        SimpleNamespace(content=None, tool_calls=[call]), "tool_calls", 10, 2, 12
    )


def run_local_harness_validation() -> dict:
    """Verify reset, contracts, ground truth, metrics, verifier, and JSON offline."""
    checks = {
        "fixture_reset": False,
        "task_isolation": False,
        "contracts": False,
        "ground_truth_and_verifier": False,
        "scripted_finish_protocol": False,
        "metrics_and_serialization": False,
    }
    with tempfile.TemporaryDirectory(prefix="phase22-local-") as directory:
        first = Path(directory) / "first"
        second = Path(directory) / "second"
        build_fixture(first)
        validate_fixture(first)
        initial = acceptance.snapshot_workspace(first)
        apply_ground_truth_fix(first, "A")
        build_fixture(first)
        checks["fixture_reset"] = acceptance.snapshot_workspace(first) == initial
        build_fixture(second)
        checks["task_isolation"] = acceptance.snapshot_workspace(second) == initial

        verifier_results = []
        for spec in _task_values():
            contract = acceptance.CodingTaskContract.from_dict(coding_contract(spec))
            checks["contracts"] = True
            root = Path(directory) / spec.code
            build_fixture(root)
            before = acceptance.snapshot_workspace(root)
            apply_ground_truth_fix(root, spec)
            verdict = acceptance.verify_contract(contract, root, before)
            verifier_results.append(verdict["artifact_passed"])
        checks["ground_truth_and_verifier"] = all(verifier_results)

        root = Path(directory) / "scripted"
        build_fixture(root)
        fixed_source = ground_truth_patch("A")["src/orders/shipping.py"]
        scripted = run_scripted_case(
            root,
            "A",
            [
                _scripted_tool_reply("read", "read_file", {"path": "src/orders/shipping.py"}),
                _scripted_tool_reply("write", "write_file", {"path": "src/orders/shipping.py", "content": fixed_source}),
                _scripted_tool_reply(
                    "test",
                    "run_command",
                    {"command": "python", "args": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_shipping.py", "-q"]},
                ),
                _scripted_tool_reply("finish", "finish_task", {"summary": "Fixed shipping and ran the required test."}),
            ],
        )
        metrics = scripted["metrics"]
        checks["scripted_finish_protocol"] = bool(
            scripted["acceptance"]["accepted"]
            and metrics["finish_after_fresh_verification"]
            and metrics["first_correct_file_turn"] == 1
        )
        json.loads(json.dumps(scripted, ensure_ascii=False))
        checks["metrics_and_serialization"] = bool(
            metrics["required_test_attempts"] == 1
            and metrics["correct_file_mutations"] == 1
            and metrics["post_mutation_exact_test_pass"]
        )

    if not all(checks.values()):
        failed = [name for name, value in checks.items() if not value]
        raise AssertionError(f"Phase 22 local harness validation failed: {failed}")
    return {"passed": True, "checks": checks}


def _failure_taxonomy(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        category = result.get("primary_failure", "INFRASTRUCTURE")
        counts[category] = counts.get(category, 0) + 1
        for secondary in result.get("secondary_failure", []):
            counts[secondary] = counts.get(secondary, 0) + 1
    return dict(sorted(counts.items()))


def _phase23_recommendation(results: list[dict]) -> dict:
    valid = [result for result in results if not result.get("infrastructure_failure")]
    failures = [result for result in valid if result.get("primary_failure") not in {None, "NONE"}]
    if not valid:
        return {
            "direction": None,
            "reason": "No valid model run was available; do not infer a Phase 23 direction.",
            "evidence": [],
        }

    labels = {
        "NAVIGATION": "Repository Navigation Strategy",
        "CONTEXT": "Source Context Before Mutation",
        "MULTI_FILE_REASONING": "Multi-File Task Planning",
        "DIAGNOSIS": "Failure Diagnosis Loop",
        "EDITING": "Editing Reliability",
        "TEST_SELECTION": "Required-Test Selection",
        "VERIFICATION": "Post-Mutation Verification Control",
        "COMPLETION": "Completion Control",
        "PERMISSION_RUNTIME": "Permission Runtime Diagnosis",
    }
    if failures:
        counts: dict[str, int] = {}
        for result in failures:
            primary = result["primary_failure"]
            counts[primary] = counts.get(primary, 0) + 1
        category = max(sorted(counts), key=lambda name: counts[name])
        supporting = [result["task_code"] for result in failures if result["primary_failure"] == category]
        return {
            "direction": labels.get(category, category.title()),
            "reason": f"{category} is the most frequent deterministic primary failure in this six-task sample.",
            "evidence": supporting,
            "minimal_candidate_change": "Design the smallest targeted intervention only after this evaluation is reviewed; do not implement it in Phase 22.",
        }

    extra_calls = sum(
        result["metrics"].get("post_verification_extra_tool_calls") or 0 for result in valid
    )
    if extra_calls:
        return {
            "direction": "Completion Control",
            "reason": "All valid runs were accepted, but tool calls still occurred after the last successful required test.",
            "evidence": [result["task_code"] for result in valid if result["metrics"].get("post_verification_extra_tool_calls")],
            "minimal_candidate_change": "Investigate a narrower completion cue and validate it with a new controlled evaluation.",
        }
    return {
        "direction": "Repository Navigation Strategy",
        "reason": "No deterministic failure dominated this small first sample; navigation cost is the next observable efficiency dimension.",
        "evidence": [result["task_code"] for result in valid],
        "minimal_candidate_change": "Define a navigation-only treatment and measure it separately; do not implement it in Phase 22.",
    }


def _result_row(result: dict) -> str:
    metrics = result["metrics"]
    nav = f"{metrics.get('search_text_calls', 0)}/{metrics.get('list_files_calls', 0)}/{metrics.get('read_file_calls', 0)}"
    return "| " + " | ".join(
        [
            result.get("task_code", "?"),
            str(metrics.get("accepted", False)),
            str(metrics.get("model_calls", 0)),
            str(metrics.get("tool_calls", 0)),
            str(metrics.get("total_tokens", 0)),
            str(metrics.get("first_correct_file_turn")),
            nav,
            str(metrics.get("mutation_count", 0)),
            str(metrics.get("required_test_attempts", 0)),
            str(metrics.get("post_verification_extra_tool_calls")),
            str(metrics.get("finish_attempts", 0)),
            result.get("primary_failure", ""),
        ]
    ) + " |"


def render_report(payload: dict) -> str:
    """Render a fact-focused report; wording intentionally avoids causal claims."""
    results = payload.get("results", [])
    lines = [
        "# Phase 22: Realistic Coding Task Evaluation",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Fixture and task design",
        "",
        "The deterministic fixture contains 33 Python files across `src/pricing`, `src/orders`, `src/users`, `src/utils`, `tests`, and `config`. Each run rebuilds it in a fresh temporary workspace.",
        "",
    ]
    for spec in _task_values():
        flags = ", ".join(name for name, enabled in spec.ground_truth.flags.items() if enabled)
        lines.append(f"- Task {spec.code} — `{spec.task_id}`: {flags or 'focused coding repair'}.")
    lines.extend(
        [
            "",
            "## Configuration and harness validation",
            "",
            f"- Context mode: `{payload.get('context_mode', 'WRITE_ONLY')}`",
            f"- Approval mode: `{payload.get('approval_mode', 'ALLOW')}`",
            f"- MAX_AGENT_STEPS: `{payload.get('max_agent_steps', config.MAX_AGENT_STEPS)}`",
            f"- Local scripted validation: `{payload.get('harness_validation', {}).get('passed', False)}`",
            f"- Provider preflight: `{payload.get('provider_preflight', {}).get('ok', False)}`",
            "",
        ]
    )
    if not results:
        lines.extend(
            [
                "## Real evaluation",
                "",
                "No valid real Agent run was recorded. The harness is complete, but live evaluation remains blocked by the provider preflight.",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "## Result table",
            "",
            "| Task | Accepted | Model Calls | Tool Calls | Tokens | First Correct Turn | Search/List/Read | Mutations | Test Attempts | Extra Calls After PASS | Finish Attempts | Primary Issue |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    lines.extend(_result_row(result) for result in results)
    lines.extend(["", "## Per-task evidence", ""])
    for result in results:
        metrics = result["metrics"]
        chain = " → ".join(event["tool"] for event in metrics.get("tool_chain", [])) or "no tool calls"
        lines.extend(
            [
                f"### Task {result['task_code']} — `{result['task_id']}`",
                "",
                f"- Accepted/artifact/interaction/self-verified: `{metrics.get('accepted')}` / `{metrics.get('artifact_passed')}` / `{metrics.get('interaction_completed')}` / `{metrics.get('agent_self_verified')}`",
                f"- Navigation: first relevant file turn `{metrics.get('first_correct_file_turn')}`, method `{metrics.get('first_correct_file_method')}`, search-first `{metrics.get('search_first')}`, list-first `{metrics.get('list_first')}`.",
                f"- Context: source relevant files read `{', '.join(metrics.get('source_relevant_files_read', [])) or 'none'}`; mutation before source context `{metrics.get('mutation_before_source_context')}`.",
                f"- Mutation/test/finish: mutations `{metrics.get('mutation_count')}`, exact required-test attempts `{metrics.get('required_test_attempts')}`, failures before success `{metrics.get('required_test_failures_before_success')}`, finish attempts `{metrics.get('finish_attempts')}`.",
                f"- Verification: fresh post-mutation PASS `{metrics.get('post_mutation_exact_test_pass')}`, finish after fresh verification `{metrics.get('finish_after_fresh_verification')}`, extra calls after PASS `{metrics.get('post_verification_extra_tool_calls')}`.",
                f"- Files: changed `{', '.join(metrics.get('changed_files', [])) or 'none'}`; unexpected `{', '.join(metrics.get('unexpected_changes', [])) or 'none'}`.",
                f"- Tool chain: `{chain}`.",
                f"- Taxonomy: primary `{result.get('primary_failure')}`, secondary `{', '.join(result.get('secondary_failure', [])) or 'none'}`.",
                "",
            ]
        )

    valid = [result for result in results if not result.get("infrastructure_failure")]
    infrastructure = [result for result in results if result.get("infrastructure_failure")]
    average_tokens = round(sum(result["metrics"].get("total_tokens", 0) for result in valid) / len(valid), 1) if valid else None
    by_code = {result["task_code"]: result["metrics"] for result in valid}
    task_d = by_code.get("D", {})
    task_e = by_code.get("E", {})
    lines.extend(
        [
            "## Observations from this sample",
            "",
            f"- Valid real runs: `{len(valid)}`; infrastructure failures: `{len(infrastructure)}`.",
            f"- Average total tokens across valid runs: `{average_tokens}`.",
            f"- Failure taxonomy: `{json.dumps(_failure_taxonomy(results), ensure_ascii=False, sort_keys=True)}`.",
            f"- {sum(bool(result['metrics'].get('accepted')) for result in valid)}/{len(valid)} valid tasks were accepted; {sum(bool(result['metrics'].get('finish_after_fresh_verification')) for result in valid)}/{len(valid)} finished after fresh verification.",
            f"- Navigation is the largest visible cost: task D made {task_d.get('list_files_calls')} `list_files` calls, read {task_d.get('read_file_calls')} files, and used {task_d.get('tool_calls')} tool calls and {task_d.get('total_tokens')} tokens.",
            f"- Task E did not exercise the intended retry path: it made {task_e.get('mutation_count')} mutation(s) and its {task_e.get('required_test_attempts')} required-test attempt(s) had {task_e.get('required_test_failures_before_success')} failure(s) before success. The result cannot support a claim about second-diagnosis ability.",
            "- These are six single runs. Navigation cost is an observed candidate for study, not a demonstrated causal bottleneck.",
            "",
            "## Phase 23 recommendation",
            "",
        ]
    )
    recommendation = payload.get("phase23_recommendation", {})
    if recommendation.get("direction"):
        lines.extend(
            [
                f"- Candidate direction: `{recommendation['direction']}`.",
                f"- Evidence: {recommendation['reason']} Supporting task(s): `{', '.join(recommendation.get('evidence', [])) or 'none'}`.",
                f"- Smallest next experiment: {recommendation.get('minimal_candidate_change', 'Review this result before proposing a change.')}",
            ]
        )
    else:
        lines.append(f"- No Phase 23 direction is recommended yet: {recommendation.get('reason')}")
    return "\n".join(lines) + "\n"


def _write_artifacts(payload: dict) -> None:
    RESULTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(render_report(payload), encoding="utf-8")


def run_phase22() -> dict:
    """Run the one-shot Phase 22 evaluation, with one retry only for provider failures."""
    os.environ["TOOL_APPROVAL_MODE"] = "ALLOW"
    os.environ["CONTEXT_MODE"] = "WRITE_ONLY"
    harness_validation = run_local_harness_validation()
    preflight = _provider_preflight()
    payload = {
        "phase": 22,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "context_mode": "WRITE_ONLY",
        "approval_mode": "ALLOW",
        "max_agent_steps": config.MAX_AGENT_STEPS,
        "harness_validation": harness_validation,
        "provider_preflight": preflight,
        "planned_tasks": len(_task_values()),
        "results": [],
    }
    if not preflight["ok"]:
        payload["status"] = "real_evaluation_blocked"
        payload["valid_real_runs"] = 0
        payload["phase23_recommendation"] = _phase23_recommendation([])
        _write_artifacts(payload)
        return payload

    results = []
    for spec in _task_values():
        attempts = []
        for attempt in range(2):
            with tempfile.TemporaryDirectory(prefix=f"phase22-{spec.code.lower()}-") as directory:
                root = build_fixture(Path(directory) / "workspace")
                validate_fixture(root)
                record = _run_child_subprocess(spec.task_id, root)
            attempts.append(record)
            if not (record.get("infrastructure_failure") and record.get("infrastructure_kind") == "provider" and attempt == 0):
                break
        final = attempts[-1]
        final["attempt_count"] = len(attempts)
        final["prior_attempts"] = attempts[:-1]
        results.append(final)
        print(
            f"[Phase 22 {spec.code}] accepted={final['metrics'].get('accepted')} "
            f"infra={final.get('infrastructure_failure')} attempts={len(attempts)}",
            file=sys.stderr,
        )

    payload["results"] = results
    payload["valid_real_runs"] = sum(not result.get("infrastructure_failure") for result in results)
    payload["infrastructure_failures"] = sum(result.get("infrastructure_failure") for result in results)
    payload["failure_taxonomy"] = _failure_taxonomy(results)
    payload["phase23_recommendation"] = _phase23_recommendation(results)
    payload["status"] = "complete" if not payload["infrastructure_failures"] else "complete_with_infrastructure_failures"
    _write_artifacts(payload)
    return payload


def _child_main(task_id: str) -> None:
    original_stdout = sys.stdout
    with redirect_stdout(sys.stderr):
        result = _run_live_task(task_id)
    print(json.dumps(result, ensure_ascii=False, indent=2), file=original_stdout)


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=sorted(TASKS))
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.child:
        _child_main(args.child)
        return
    if args.self_check:
        print(json.dumps(run_local_harness_validation(), ensure_ascii=False, indent=2))
        return
    print(json.dumps(run_phase22(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main_cli()
