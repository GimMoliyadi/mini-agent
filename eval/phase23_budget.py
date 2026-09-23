"""Compare diagnosis recovery at larger model-turn budgets without changing Runtime."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import redirect_stdout
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

from . import phase22_5, phase22_harness as harness

import acceptance
import config
import main


EVAL_DIR = Path(__file__).resolve().parent
RESULTS = EVAL_DIR / "phase23_budget_results.json"
REPORT = EVAL_DIR / "phase23_budget_report.md"
BASELINE = EVAL_DIR / "phase22_5_results.json"
TASK = "A coupon of 10 percent on 200 currently gives 190 instead of 180. Fix the reported behavior, run the required test, and use any failure output to diagnose remaining cases."
REQUIRED_TEST = [
    "python",
    ["-m", "unittest", "discover", "-s", "tests", "-p", "test_coupons.py", "-q"],
    ".",
]
MUTATION_TOOLS = {"apply_patch", "write_file"}
READ_SEARCH_TOOLS = {"read_file", "search_text"}
INFRASTRUCTURE_RETRIES = 1
PROJECT_PROVIDER_PROXY_ENV = "MINI_AGENT_HTTP_PROXY"


def _baseline_run() -> dict:
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    result = payload["results"]["E"]
    if result.get("max_agent_steps") != 8 or result.get("task_id") != "diagnosis_recovery":
        raise ValueError("Phase 22.5 baseline is not the expected 8-step diagnosis run")
    return result


def _prepare_manifest(baseline: dict) -> dict:
    model_config = config.load_config()
    if model_config.model != baseline["model"]:
        raise ValueError(
            f"configured model {model_config.model!r} differs from baseline {baseline['model']!r}"
        )
    with tempfile.TemporaryDirectory(prefix="phase23-manifest-") as directory:
        spec = phase22_5.setup("E", Path(directory) / "workspace")
        contract = phase22_5.contract(spec)
        messages = harness._model_messages(acceptance.CodingTaskContract.from_dict(contract))
    baseline_messages = baseline["canonical_history"][: len(messages)]
    if messages != baseline_messages:
        raise ValueError("Phase 23 initial model messages differ from the Phase 22.5 baseline")
    if spec.issue != TASK or contract["test_command"] != {
        "command": REQUIRED_TEST[0],
        "args": REQUIRED_TEST[1],
        "cwd": REQUIRED_TEST[2],
    }:
        raise ValueError("Phase 23 diagnosis task or required test differs from the baseline")
    if contract["allowed_paths"] != phase22_5.contract(spec)["allowed_paths"]:
        raise ValueError("Phase 23 allowed paths differ from the Phase 22.5 contract")
    return {
        "baseline_commit": "950b235",
        "task_id": spec.task_id,
        "task_code": spec.code,
        "task": spec.issue,
        "required_test": contract["test_command"],
        "allowed_paths": contract["allowed_paths"],
        "model": model_config.model,
        "context_mode": "WRITE_ONLY",
        "approval_mode": "ALLOW",
        "tool_names": [item["function"]["name"] for item in main.AVAILABLE_TOOLS],
        "tool_definitions": main.AVAILABLE_TOOLS,
        "initial_messages_match_baseline": True,
        "runtime_changed": False,
    }


def _provider_child_environment(parent_environment: Mapping[str, str]) -> dict[str, str]:
    """Return an Eval/Provider environment without changing the caller's environment."""
    child_environment = dict(parent_environment)
    project_proxy = child_environment.get(PROJECT_PROVIDER_PROXY_ENV)
    if project_proxy:
        child_environment["HTTP_PROXY"] = project_proxy
        child_environment["HTTPS_PROXY"] = project_proxy
        child_environment.pop("ALL_PROXY", None)
    return child_environment


def _proxy_diagnostics(environment: Mapping[str, str]) -> dict:
    diagnostics = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        value = environment.get(name, "")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            port = None
        diagnostics[name] = {
            "configured": bool(value),
            "scheme": parsed.scheme or None,
            "loopback": parsed.hostname in ("localhost", "127.0.0.1", "::1"),
            "port": port,
        }
    return diagnostics


def _valid_assistant_response(response) -> bool:
    choices = getattr(response, "choices", None)
    if not choices:
        return False
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return (
        getattr(message, "role", None) == "assistant"
        and isinstance(content, str)
        and bool(content.strip())
    )


def _provider_preflight() -> dict:
    config.load_env_file()
    model_config = config.load_config()
    provider_environment = _provider_child_environment(os.environ)
    result = {
        "attempts": 1,
        "status": "fail",
        "request_completed": False,
        "valid_assistant_response": False,
        "response_text_recorded": False,
        "model": model_config.model,
        "provider_scheme": urlsplit(model_config.base_url).scheme,
        "proxy_settings": _proxy_diagnostics(provider_environment),
        "sdk_max_retries": 0,
        "api_key_recorded": False,
    }
    try:
        with patch.dict(os.environ, provider_environment, clear=True):
            client = main.build_client(model_config).with_options(max_retries=0)
            try:
                response = client.chat.completions.create(
                    model=model_config.model,
                    messages=[{"role": "user", "content": "Reply OK"}],
                )
            finally:
                client.close()
        result["request_completed"] = True
        result["valid_assistant_response"] = _valid_assistant_response(response)
        result["status"] = "pass" if result["valid_assistant_response"] else "fail"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    return result


def _run_child(steps: int) -> dict:
    root = Path(os.environ["AGENT_WORKSPACE"])
    spec = phase22_5.setup("E", root, register=True)
    if spec.issue != TASK:
        raise ValueError("diagnosis task drifted")
    with (
        patch.object(harness, "coding_contract", phase22_5.contract),
        patch.object(config, "MAX_AGENT_STEPS", steps),
        patch.object(main, "MAX_AGENT_STEPS", steps),
    ):
        return harness._run_live_task(spec.task_id)


def _run_attempt(steps: int) -> dict:
    config.load_env_file()
    with tempfile.TemporaryDirectory(prefix=f"phase23-budget-{steps}-") as directory:
        root = Path(directory) / "workspace"
        phase22_5.setup("E", root)
        environment = _provider_child_environment({
            **os.environ,
            "AGENT_WORKSPACE": str(root),
            "TOOL_APPROVAL_MODE": "ALLOW",
            "CONTEXT_MODE": "WRITE_ONLY",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PHASE23_MAX_AGENT_STEPS": str(steps),
        })
        completed = subprocess.run(
            [str(harness.PYTHON_BIN), "-m", "eval.phase23_budget", "--child"],
            cwd=harness.PROJECT_ROOT,
            env=environment,
            capture_output=True,
            timeout=harness.CHILD_TIMEOUT_SECONDS,
        )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "max_agent_steps": steps,
            "infrastructure_failure": True,
            "infrastructure_kind": "harness",
            "runtime_errors": [f"child exit={completed.returncode}; {stderr[-3000:]}; {stdout[-1000:]}"]
        }
    if completed.returncode and not result.get("infrastructure_failure"):
        result["infrastructure_failure"] = True
        result["infrastructure_kind"] = "harness"
        result.setdefault("runtime_errors", []).append(f"child exit={completed.returncode}: {stderr[-3000:]}")
    return result


def _run_budget(steps: int) -> dict:
    attempts = []
    for attempt in range(INFRASTRUCTURE_RETRIES + 1):
        try:
            result = _run_attempt(steps)
        except (OSError, subprocess.SubprocessError) as exc:
            result = {
                "max_agent_steps": steps,
                "infrastructure_failure": True,
                "infrastructure_kind": "harness",
                "runtime_errors": [f"{type(exc).__name__}: {exc}"],
            }
        attempts.append(result)
        if not result.get("infrastructure_failure") or attempt == INFRASTRUCTURE_RETRIES:
            break
    return {
        "attempt_count": len(attempts),
        "valid_real_run": not attempts[-1].get("infrastructure_failure", False),
        "prior_attempts": attempts[:-1],
        "result": attempts[-1],
    }


def _network_diagnostics() -> dict:
    model_config = config.load_config()
    base = urlsplit(model_config.base_url)
    proxy_settings = {}
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.environ.get(name, "")
        parsed = urlsplit(value)
        proxy_settings[name] = {
            "configured": bool(value),
            "scheme": parsed.scheme or None,
            "loopback": parsed.hostname in ("localhost", "127.0.0.1", "::1"),
            "port": parsed.port,
        }
    listeners = {}
    for port in (7897, 9674):
        connection = socket.socket()
        connection.settimeout(0.3)
        listeners[str(port)] = connection.connect_ex(("127.0.0.1", port)) == 0
        connection.close()
    return {
        "provider_base_scheme": base.scheme,
        "provider_base_is_loopback": base.hostname in ("localhost", "127.0.0.1", "::1"),
        "proxy_settings": proxy_settings,
        "local_proxy_listeners": listeners,
    }


def _historical_budget_evidence() -> dict:
    phase22 = json.loads((EVAL_DIR / "phase22_results.json").read_text(encoding="utf-8"))
    phase22_metrics = [run.get("metrics", {}) for run in phase22.get("results", [])]
    phase22_5 = json.loads((EVAL_DIR / "phase22_5_results.json").read_text(encoding="utf-8"))
    return {
        "phase22_budget": phase22.get("max_agent_steps"),
        "phase22_runs": len(phase22_metrics),
        "phase22_accepted": sum(bool(item.get("accepted")) for item in phase22_metrics),
        "phase22_model_calls": [item.get("model_calls") for item in phase22_metrics],
        "phase22_max_steps_reached": sum(bool(item.get("max_steps_reached")) for item in phase22_metrics),
        "phase22_5_navigation": {
            code: {
                "accepted": phase22_5["results"][code]["metrics"].get("accepted"),
                "model_calls": phase22_5["results"][code]["metrics"].get("model_calls"),
                "max_steps_reached": phase22_5["results"][code]["metrics"].get("max_steps_reached"),
            }
            for code in ("N1", "N2", "N3")
        },
    }


def _exact_required_test(event: dict) -> bool:
    if event.get("tool") != "run_command":
        return False
    args = event.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return False
    return (
        args.get("command") == REQUIRED_TEST[0]
        and args.get("args") == REQUIRED_TEST[1]
        and args.get("cwd", ".") == REQUIRED_TEST[2]
    )


def _test_name_from_failure(output: str) -> str | None:
    match = re.search(r"(?:FAIL|ERROR):\s+([\w.]+)", output)
    return match.group(1) if match else None


def _analysis(result: dict) -> dict:
    metrics = result.get("metrics", {})
    trace_events = {
        event.get("event_seq"): event
        for event in result.get("trace", {}).get("events", [])
    }
    chain = []
    for event in metrics.get("tool_chain", []):
        enriched_event = dict(event)
        trace_event = trace_events.get(event.get("event_seq"), {})
        for name in ("action", "write_target"):
            if trace_event.get(name) is not None:
                enriched_event[name] = trace_event[name]
        chain.append(enriched_event)
    attempts = [event for event in chain if _exact_required_test(event)]
    failed = next((event for event in attempts if event.get("exit_code") not in (None, 0)), None)
    mutation_events = [
        event for event in chain
        if event.get("tool") in MUTATION_TOOLS
    ]
    successful_tests = [event for event in attempts if event.get("exit_code") == 0]
    finish = next((event for event in chain if event.get("tool") == "finish_task"), None)
    post_failure = (
        [event for event in chain if event.get("event_seq", 0) > failed.get("event_seq", 0)]
        if failed
        else []
    )
    first_read_search = next(
        (event for event in post_failure if event.get("tool") in READ_SEARCH_TOOLS), None
    )
    second_mutation = next(
        (
            event for event in post_failure
            if event.get("tool") in MUTATION_TOOLS
        ),
        None,
    )
    next_turn = failed["turn"] + 1 if failed and failed.get("turn") is not None else None
    assistant_messages = [
        message for message in result.get("canonical_history", [])
        if message.get("role") == "assistant"
    ]
    next_message = assistant_messages[next_turn - 1] if next_turn and len(assistant_messages) >= next_turn else None
    next_content = next_message.get("content") if next_message else None
    failure_observations = []
    for failed_event in (event for event in attempts if event.get("exit_code") not in (None, 0)):
        failure_turn = failed_event.get("turn")
        following_turn = failure_turn + 1 if failure_turn is not None else None
        following_message = (
            assistant_messages[following_turn - 1]
            if following_turn and len(assistant_messages) >= following_turn
            else None
        )
        following_actions = [
            event for event in chain
            if event.get("turn") == following_turn
            and event.get("event_seq", 0) > failed_event.get("event_seq", 0)
        ]
        failure_name = _test_name_from_failure(str(failed_event.get("result", "")))
        message_evidence = (following_message or {}).get("content") or ""
        message_evidence += json.dumps(
            (following_message or {}).get("tool_calls", []), ensure_ascii=False
        )
        explicitly_referenced = bool(failure_name and failure_name in message_evidence) or any(
            failure_name
            and (
                failure_name in str(event.get("arguments", ""))
                or failure_name in str(event.get("result", ""))
            )
            for event in following_actions
        )
        failure_observations.append({
            "test_name": failure_name,
            "failure_turn": failure_turn,
            "failure_output": failed_event.get("result"),
            "next_model_turn": following_turn if following_message else None,
            "next_model_turn_content": (following_message or {}).get("content"),
            "next_model_turn_actions": following_actions,
            "next_model_turn_visibly_references_failure": explicitly_referenced,
        })
    first_failure_observation = failure_observations[0] if failure_observations else {}
    failure_name = first_failure_observation.get("test_name")
    visible_failure_reference = first_failure_observation.get(
        "next_model_turn_visibly_references_failure", False
    )
    later_explanatory_message = next(
        (
            (turn, message.get("content"))
            for turn, message in enumerate(assistant_messages, start=1)
            if failed and turn > failed.get("turn", 0) and str(message.get("content") or "").strip()
        ),
        (None, None),
    )
    post_failure_tests = [
        event for event in attempts
        if failed and event.get("event_seq", 0) > failed.get("event_seq", 0)
    ]
    post_failure_mutations = [
        event for event in post_failure
        if event.get("tool") in MUTATION_TOOLS
    ]
    accepted = metrics.get("accepted", False)
    diagnosis_recovery_observed = bool(
        failed
        and any(event.get("exit_code") == 0 for event in post_failure_tests)
        and finish
        and accepted
    )
    return {
        "first_mutation_turn": mutation_events[0].get("turn") if mutation_events else None,
        "first_required_test_failure_turn": failed.get("turn") if failed else None,
        "failure_output": failed.get("result") if failed else None,
        "failure_test_name": failure_name,
        "next_model_turn": next_turn if next_message else None,
        "next_model_turn_content": next_content,
        "next_model_turn_actions": [event for event in post_failure if event.get("turn") == next_turn],
        "next_model_turn_visibly_references_failure": visible_failure_reference,
        "failure_observations": failure_observations,
        "first_later_explanatory_turn": later_explanatory_message[0],
        "first_later_explanatory_content": later_explanatory_message[1],
        "post_failure_tool_chain": post_failure,
        "post_failure_mutations": post_failure_mutations,
        "post_failure_mutation_targets": [
            {"turn": event.get("turn"), "tool": event.get("tool"), "target": event.get("write_target")}
            for event in post_failure_mutations
        ],
        "required_test_events": [
            {
                "turn": event.get("turn"),
                "test_name": _test_name_from_failure(str(event.get("result", ""))),
                "exit_code": event.get("exit_code"),
                "output": event.get("result"),
            }
            for event in attempts
        ],
        "post_failure_required_test_attempts": [
            {
                "turn": event.get("turn"),
                "test_name": _test_name_from_failure(str(event.get("result", ""))),
                "exit_code": event.get("exit_code"),
                "output": event.get("result"),
            }
            for event in post_failure_tests
        ],
        "first_post_failure_read_or_search": first_read_search,
        "second_mutation": second_mutation,
        "second_mutation_target": second_mutation.get("write_target") if second_mutation else None,
        "required_test_attempts": len(attempts),
        "required_test_failures": sum(event.get("exit_code") not in (None, 0) for event in attempts),
        "required_test_pass_turns": [event.get("turn") for event in successful_tests],
        "finish_task_turn": finish.get("turn") if finish else None,
        "finish_attempts": metrics.get("finish_task_calls", 0),
        "accepted": accepted,
        "diagnosis_recovery_observed": diagnosis_recovery_observed,
        "model_calls": metrics.get("model_calls", result.get("trace", {}).get("model_calls")),
        "tool_calls": metrics.get("tool_calls", result.get("trace", {}).get("tool_calls")),
        "total_tokens": metrics.get("total_tokens", result.get("trace", {}).get("total_tokens")),
        "post_verification_extra_tool_calls": metrics.get("post_verification_extra_tool_calls"),
        "changed_files": metrics.get("changed_files", []),
        "unexpected_changes": metrics.get("unexpected_changes", []),
    }


def _event_label(event: dict | None) -> str:
    if not event:
        return "none"
    if event.get("write_target"):
        return f"Turn {event.get('turn')} `{event.get('tool')}` target `{event['write_target']}`"
    args = event.get("arguments", "")
    try:
        rendered = json.dumps(json.loads(args), ensure_ascii=False)
    except (TypeError, json.JSONDecodeError):
        rendered = str(args)
    return f"Turn {event.get('turn')} `{event.get('tool')}` {rendered}"


def render_report(payload: dict) -> str:
    baseline = payload["runs"]["8"]["result"]
    lines = [
        "# Phase 23 — Diagnosis Recovery & Step-Budget Semantics",
        "",
        f"Experiment status: `{payload.get('status', 'unknown')}`.",
        "Baseline commit: `950b235`.",
        "",
        "## Phase 22.5 baseline and runtime semantics",
        "",
        "Phase 22.5 E used the diagnosis task, the coupon fixture, `test_coupons.py`, WRITE_ONLY context, ALLOW approval, and `sensenova-6.8-flash-lite`. Turn 7 applied the first mutation. Turn 8 ran the exact required test and failed only `test_one_percent_boundary` (`0.0 != 99.0`). The failed tool result was appended to canonical history. The 8-model-call budget was then exhausted, so no Turn 9 model response consumed that observation. Baseline: accepted `False`, 8 model calls, 18 tool calls, 30,421 tokens, one required-test attempt, one failure, no finish call.",
        "",
        "`MAX_AGENT_STEPS` counts model requests/responses (the loop iteration), not individual tools. A model response can request and execute multiple tool calls; the final allowed response is processed through its tool calls before the loop sets `LIMIT_REACHED`, unless a `finish_task` succeeds first. Therefore the last allowed tool result can enter history without a following model request. This is exactly what happened after Turn 8's test failure.",
        "",
        "## 8 / 10 / 12 step comparison",
        "",
        "| Budget | Source | Accepted | Model calls | Tool calls | Tokens | Required test attempts / failures | PASS turn | Finish turn | Extra tools after PASS |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    history = payload["historical_budget_evidence"]
    nav_calls = [history["phase22_5_navigation"][code]["model_calls"] for code in ("N1", "N2", "N3")]
    historical = (
        f"Prior controls: Phase 22 accepted {history['phase22_accepted']}/{history['phase22_runs']} tasks in "
        f"{history['phase22_model_calls']} model calls, with {history['phase22_max_steps_reached']} budget hits; "
        f"Phase 22.5 navigation accepted 3/3 in {nav_calls} calls. N1 used all 8 calls and still finished "
        "because the final `finish_task` succeeded. These samples do not show systematic cutoff of ordinary tasks."
    )
    compare_at = lines.index("## 8 / 10 / 12 step comparison")
    lines[compare_at:compare_at] = [historical, ""]
    for budget in (8, 10, 12):
        entry = payload["runs"][str(budget)]
        result = entry["result"]
        analysis = _analysis(result)
        source = "Phase 22.5 trace" if budget == 8 else f"{entry['attempt_count']} Phase 23 attempt(s)"
        pass_turn = ", ".join(str(turn) for turn in analysis["required_test_pass_turns"]) or "—"
        lines.append(
            f"| {budget} | {source} | {analysis['accepted']} | {analysis['model_calls'] or 0} | {analysis['tool_calls'] or 0} | {analysis['total_tokens'] or 0} | {analysis['required_test_attempts']} / {analysis['required_test_failures']} | {pass_turn} | {analysis['finish_task_turn'] or '—'} | {analysis['post_verification_extra_tool_calls'] if analysis['post_verification_extra_tool_calls'] is not None else '—'} |"
        )
    lines.extend(["", "## Diagnosis chains", ""])
    for budget in (10, 12):
        entry = payload["runs"][str(budget)]
        result = entry["result"]
        analysis = _analysis(result)
        lines.extend([
            f"### Run {'B' if budget == 10 else 'C'} — MAX_AGENT_STEPS={budget}",
            "",
        ])
        if entry.get("prior_attempts"):
            lines.append(f"Infrastructure retry: {len(entry['prior_attempts'])} prior attempt(s), recorded separately and excluded from Agent behavior.")
            lines.append("")
        if result.get("infrastructure_failure"):
            lines.extend([
                f"Infrastructure failure: `{result.get('infrastructure_kind')}` — `{'; '.join(result.get('runtime_errors', []))}`",
                "No model response was received: first mutation, first required-test failure, post-failure read/search, second mutation, PASS, and finish turn are all absent. Required-test attempts/failures: 0/0; accepted: `False`; completed model calls/tool calls/tokens: 0/0/0.",
                "Exploration redundancy and post-PASS behavior are not assessable because neither run reached a PASS.",
                "",
            ])
            continue
        lines.extend([
            f"- First mutation: Turn {analysis['first_mutation_turn']}",
            f"- First required-test failure: Turn {analysis['first_required_test_failure_turn']}",
            f"- Failure output: `{analysis['failure_output'] or 'none'}`",
            f"- Next model turn: Turn {analysis['next_model_turn']}; visibly references failure output: `{analysis['next_model_turn_visibly_references_failure']}`",
            f"- First post-failure read/search: {_event_label(analysis['first_post_failure_read_or_search'])}",
            f"- Second mutation: {_event_label(analysis['second_mutation'])}",
            f"- Required-test attempts/failures: {analysis['required_test_attempts']}/{analysis['required_test_failures']}",
            f"- Required-test PASS turn(s): {analysis['required_test_pass_turns'] or 'none'}; finish_task turn: {analysis['finish_task_turn'] or 'none'}",
            f"- Accepted: `{analysis['accepted']}`; model calls: {analysis['model_calls']}; tool calls: {analysis['tool_calls']}; total tokens: {analysis['total_tokens']}",
            f"- Post-verification extra tool calls: {analysis['post_verification_extra_tool_calls']}",
            "",
            "Observed post-failure chain:",
            "",
        ])
        for event in analysis["post_failure_tool_chain"]:
            lines.append(f"- {_event_label(event)} — {event.get('classification')}; exit={event.get('exit_code')}")
        lines.append("")
    network = payload.get("network_diagnostics", {})
    proxy_listeners = network.get("local_proxy_listeners", {})
    requested_proxy = network.get("proxy_settings", {}).get("HTTPS_PROXY", {})
    lines.extend([
        "## Provider connectivity and evidence limits",
        "",
        f"Both budgets attempted one initial provider request and one infrastructure retry. Both attempts for each budget returned `APIConnectionError: Connection error.` before a model response; no Agent behavior, test run, mutation, or recovery chain was observed. The run used HTTPS proxy port `{requested_proxy.get('port')}`; the local TCP check found port 7897 accepting connections=`{proxy_listeners.get('7897')}` and port 9674 accepting connections=`{proxy_listeners.get('9674')}`. The provider endpoint was HTTPS and non-loopback. This points to the configured proxy endpoint as the connection blocker.",
        "",
        payload.get("harness_note", "Child JSON is captured on stdout; stderr remains separate for runtime logs."),
        "",
        "## Interpretation",
        "",
        "A recovery is supported only when the trace shows that the model received the failed test result, made a later mutation that addresses the reported boundary, then obtained a fresh required-test PASS and finished. The next assistant message and its tool arguments are the observable evidence; hidden reasoning is not inferred. Any unrelated post-failure reads/searches and calls after PASS are reported from the trace.",
        "",
        "## Candidate designs",
        "",
        "| Design | Complexity | Predictability | Token cost | Extension risk | Preserves hard cap? | Fit to observed issue |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| A. Raise the default cap | Lowest: change one constant | Simple global ceiling, but no work-type semantics | Raises worst-case model turns for every task (10 is +25% vs 8; 12 is +50%); token growth also depends on history size | Still bounded; another end-of-budget observation can be stranded | Yes, at the new higher ceiling | May work if the tested recovery chain fits; does not reserve a chance to consume the last observation |",
        "| B. One bounded recovery turn after a final failure observation | Moderate: classify eligible results and track one exception | Precise for the diagnosed failure path | At most one additional model response per eligible terminal failure | Low if the exception is consumed once and never renews | Yes, with a strict one-turn exception | Directly targets Turn 8 FAIL → no Turn 9; may be insufficient if recovery needs several model turns |",
        "| C. Explicit model/action/recovery budgets | Highest: define and enforce multiple counters | Most explicit when each counter and total ceiling is specified | Controllable by category; can avoid paying recovery budget on ordinary tasks | Depends on a nonrenewable global ceiling; otherwise easy to extend indefinitely | Yes, if a hard total ceiling remains | Clarifies the semantic mismatch, but only solves this case if recovery budget is allocated after failure |",
        "",
        "## Conclusion",
        "",
        f"- Diagnosis capability: {payload['conclusions']['diagnosis_capability']}",
        f"- Phase 22.5 budget finding: {payload['conclusions']['baseline_budget_truncation']}",
        f"- Systematic 8-step truncation: {payload['conclusions']['eight_step_systematic_truncation']}",
        f"- Budget truncation evidence: {payload['conclusions']['budget_truncation_evidence']}",
        f"- Larger-budget effect: {payload['conclusions']['larger_budget_recovery_effect']}",
        f"- Single next recommendation: {payload['conclusions']['next_recommended_direction']}",
        "",
        "## Validation",
        "",
    ])
    validation = payload.get("validation")
    if validation:
        for name in ("unittest", "compileall", "diff_check"):
            check = validation[name]
            lines.append(f"- `{check['command']}`: {check['status']} (exit {check['exit_code']})")
    else:
        lines.append("Validation results are not recorded yet.")
    lines.append("")
    return "\n".join(lines)


def _phase23_1_report_section(payload: dict) -> str:
    recovery = payload["phase23_1_recovery"]
    parent_proxy = recovery["proxy_environment"]["parent"]
    provider_proxy = recovery["proxy_environment"]["provider"]
    preflight = recovery["provider_preflight"]
    lines = [
        "## Phase 23.1 — Provider recovery and budget rerun",
        "",
        f"Status: `{recovery['status']}`.",
        "",
        "### Provider proxy isolation",
        "",
        "The Eval parent proxy environment is recorded separately from the effective Provider child environment:",
        "",
        f"- Parent HTTP/HTTPS/ALL: `{parent_proxy['HTTP_PROXY']}` / `{parent_proxy['HTTPS_PROXY']}` / `{parent_proxy['ALL_PROXY']}`",
        f"- Provider child HTTP/HTTPS/ALL: `{provider_proxy['HTTP_PROXY']}` / `{provider_proxy['HTTPS_PROXY']}` / `{provider_proxy['ALL_PROXY']}`",
        f"- Project override configured: `{recovery['project_proxy_configured']}`; Codex/global proxy settings changed: `False`.",
        "",
        "### Provider preflight",
        "",
        f"- Status: `{preflight['status']}`; valid assistant response: `{preflight['valid_assistant_response']}`; completed request: `{preflight['request_completed']}`.",
        f"- Provider scheme/model: `{preflight['provider_scheme']}` / `{preflight['model']}`; SDK retries: `{preflight['sdk_max_retries']}`.",
        "- Response text and API key were not recorded. The check accepts any non-empty assistant response; it does not require the text `OK`.",
        "",
    ]
    provenance = recovery.get("historical_proxy_provenance") or {}
    if provenance.get("phase23_7897_source"):
        lines.extend([f"- Earlier 7897 source finding: {provenance['phase23_7897_source']}", ""])
    if recovery.get("preflight_history"):
        lines.extend([
            f"Earlier preflight observations retained: {len(recovery['preflight_history'])}; the earlier exact-`OK` mismatch is historical and is not the current acceptance rule.",
            "",
        ])
    baseline = payload["runs"]["8"]["result"]
    baseline_analysis = _analysis(baseline)
    lines.extend([
        "### Budget comparison",
        "",
        "| Budget | Run status | Accepted | Model calls | Tool calls | Tokens | Required tests / failures | PASS turn(s) | Finish turn | Max steps reached |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- |",
        f"| 8 | Reused Phase 22.5 baseline | {baseline_analysis['accepted']} | {baseline_analysis['model_calls'] or 0} | {baseline_analysis['tool_calls'] or 0} | {baseline_analysis['total_tokens'] or 0} | {baseline_analysis['required_test_attempts']} / {baseline_analysis['required_test_failures']} | {baseline_analysis['required_test_pass_turns'] or '—'} | {baseline_analysis['finish_task_turn'] or '—'} | {baseline.get('trace', {}).get('max_steps_reached', False)} |",
    ])
    for budget in (10, 12):
        entry = recovery.get("budget_runs", {}).get(str(budget))
        if not entry:
            lines.append(f"| {budget} | Not started | — | — | — | — | — | — | — | — |")
            continue
        result = entry["result"]
        analysis = entry.get("analysis", _analysis(result))
        pass_turns = analysis["required_test_pass_turns"] or "—"
        if entry.get("valid_real_run"):
            run_status = "Valid run"
        elif result.get("infrastructure_failure"):
            run_status = "Infrastructure failure"
        else:
            run_status = "Invalid run"
        lines.append(
            f"| {budget} | {run_status} ({entry['attempt_count']} attempt(s)) | {analysis['accepted']} | {analysis['model_calls'] or 0} | {analysis['tool_calls'] or 0} | {analysis['total_tokens'] or 0} | {analysis['required_test_attempts']} / {analysis['required_test_failures']} | {pass_turns} | {analysis['finish_task_turn'] or '—'} | {result.get('trace', {}).get('max_steps_reached', False)} |"
        )
    lines.extend(["", "### Diagnosis traces", ""])
    for budget in (10, 12):
        entry = recovery.get("budget_runs", {}).get(str(budget))
        lines.extend([f"#### MAX_AGENT_STEPS={budget}", ""])
        if not entry:
            reason = "Provider preflight did not pass" if recovery["status"] == "preflight_failed" else "Not run yet"
            lines.extend([f"Not started: {reason}.", ""])
            continue
        result = entry["result"]
        analysis = entry.get("analysis", _analysis(result))
        if result.get("infrastructure_failure"):
            lines.extend([
                f"Infrastructure failure: `{result.get('infrastructure_kind')}`; model calls/tool calls/tokens: {analysis['model_calls'] or 0}/{analysis['tool_calls'] or 0}/{analysis['total_tokens'] or 0}.",
                f"Error types: `{[item.split(':', 1)[0] for item in result.get('runtime_errors', [])]}`.",
                "",
            ])
            continue
        second_mutation = analysis["second_mutation"]
        post_failure_reads = [
            event for event in analysis["post_failure_tool_chain"]
            if event.get("tool") in READ_SEARCH_TOOLS
        ]
        lines.extend([
            f"- First mutation turn: {analysis['first_mutation_turn']}; first required-test failure turn: {analysis['first_required_test_failure_turn']}.",
        ])
        for test_event in analysis["required_test_events"]:
            if test_event["exit_code"] == 0:
                lines.append(f"- Required test PASS at turn {test_event['turn']}.")
            else:
                observation = next(
                    (
                        item for item in analysis["failure_observations"]
                        if item["failure_turn"] == test_event["turn"]
                    ),
                    {},
                )
                lines.extend([
                    f"- Required test failure at turn {test_event['turn']} ({test_event['test_name'] or 'test name unavailable'}):",
                    "```text",
                    str(test_event["output"] or "(no failure output)"),
                    "```",
                    f"  Immediate next model turn: {observation.get('next_model_turn') or 'none'}; explicitly references the failing test: `{observation.get('next_model_turn_visibly_references_failure', False)}`; actions: `{[_event_label(event) for event in observation.get('next_model_turn_actions', [])]}`.",
                ])
        lines.extend([
            f"- First model turn after the first failure: {analysis['next_model_turn']}; explicitly names the failing test: `{analysis['next_model_turn_visibly_references_failure']}`.",
            f"- First later explanatory assistant turn: {analysis['first_later_explanatory_turn'] or 'none'}; content: `{analysis['first_later_explanatory_content'] or 'none'}`.",
            f"- Post-failure list/search/read calls: `{[_event_label(event) for event in post_failure_reads]}`.",
            f"- Second mutation: {_event_label(second_mutation)}.",
            f"- All post-failure mutations: `{[_event_label(event) for event in analysis['post_failure_mutations']]}`.",
            f"- Required-test attempts/failures: {analysis['required_test_attempts']}/{analysis['required_test_failures']}; PASS turn(s): {analysis['required_test_pass_turns'] or 'none'}.",
            f"- Finish turn/attempts: {analysis['finish_task_turn'] or 'none'}/{analysis['finish_attempts']}; accepted: `{analysis['accepted']}`.",
            f"- Post-verification extra tool calls: {analysis['post_verification_extra_tool_calls']}; max steps reached: `{result.get('trace', {}).get('max_steps_reached', False)}`.",
            "",
        ])
    if recovery["status"] == "completed":
        recommendation = (
            "The 10-step run was accepted after 9 model calls; the 12-step run used its full "
            "budget and did not finish verification. These single runs do not justify changing "
            "the Runtime or selecting a new default budget."
        )
    elif recovery["status"] == "preflight_failed":
        recommendation = "Resolve the project Provider proxy or response failure before retrying; no budget run was started."
    else:
        recommendation = "Keep Runtime unchanged; review both budget traces before selecting any budget-policy change."
    lines.extend([
        "### Current conclusion",
        "",
        f"- Diagnosis recovery observed: `{any(entry.get('analysis', {}).get('diagnosis_recovery_observed') for entry in recovery.get('budget_runs', {}).values())}`.",
        f"- Next step: {recommendation}",
        "- Full child traces, canonical messages, tool chains, and metrics are retained in `phase23_budget_results.json`.",
        "",
    ])
    return "\n".join(lines)


def _write_phase23_1_outputs(payload: dict) -> None:
    RESULTS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
    marker = "## Phase 23.1 — Provider recovery and budget rerun"
    if marker in report:
        report = report.split(marker, 1)[0].rstrip()
    section = _phase23_1_report_section(payload)
    REPORT.write_text(f"{report}\n\n{section}", encoding="utf-8")


def run_phase23_1() -> dict:
    baseline = _baseline_run()
    manifest = _prepare_manifest(baseline)
    payload = json.loads(RESULTS.read_text(encoding="utf-8"))
    previous = payload.get("phase23_1_recovery", {})
    preflight_history = list(previous.get("preflight_history", []))
    if previous.get("provider_preflight"):
        preflight_history.append(previous["provider_preflight"])

    provider_environment = _provider_child_environment(os.environ)
    preflight = _provider_preflight()
    recovery = {
        "status": "running" if preflight["status"] == "pass" else "preflight_failed",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "manifest_check": {
            "task_id": manifest["task_id"],
            "task_code": manifest["task_code"],
            "model": manifest["model"],
            "context_mode": manifest["context_mode"],
            "approval_mode": manifest["approval_mode"],
            "initial_messages_match_baseline": manifest["initial_messages_match_baseline"],
        },
        "project_proxy_configured": bool(os.environ.get(PROJECT_PROVIDER_PROXY_ENV)),
        "proxy_environment": {
            "parent": _proxy_diagnostics(os.environ),
            "provider": _proxy_diagnostics(provider_environment),
        },
        "provider_preflight": preflight,
        "preflight_history": preflight_history,
        "historical_proxy_provenance": previous.get("proxy_provenance"),
        "previous_budget_status": previous.get("budget_runs"),
        "budget_runs": {},
        "runtime_changed": False,
        "codex_proxy_configuration_changed": False,
        "windows_global_proxy_changed": False,
    }
    payload["phase23_1_recovery"] = recovery
    _write_phase23_1_outputs(payload)
    if preflight["status"] != "pass":
        return payload

    for budget in (10, 12):
        entry = _run_budget(budget)
        entry["analysis"] = _analysis(entry["result"])
        if entry.get("valid_real_run") and entry["result"].get("model") != manifest["model"]:
            entry["valid_real_run"] = False
            entry["validation_error"] = "model differs from Phase 22.5 baseline"
        recovery["budget_runs"][str(budget)] = entry
        _write_phase23_1_outputs(payload)

    recovery["status"] = (
        "completed"
        if all(recovery["budget_runs"][str(budget)]["valid_real_run"] for budget in (10, 12))
        else "budget_run_failed"
    )
    _write_phase23_1_outputs(payload)
    return payload


def run_phase23() -> dict:
    """Backward-compatible entry point for the Phase 23.1 continuation."""
    return run_phase23_1()


def _conclusions(payload: dict) -> dict:
    larger_runs = [payload["runs"][str(budget)]["result"] for budget in (10, 12)]
    if any(result.get("infrastructure_failure") for result in larger_runs):
        return {
            "diagnosis_capability": "UNRESOLVED: neither B nor C produced a valid model response",
            "baseline_budget_truncation": "CONFIRMED: Turn 8's failure result was recorded after the eighth model response, then LIMIT_REACHED prevented a ninth response",
            "larger_budget_recovery_effect": "UNTESTED: one or both larger-budget runs failed before model behavior was observed",
            "eight_step_systematic_truncation": "NOT SHOWN in current historical samples: 6/6 Phase 22 tasks and 3/3 Phase 22.5 navigation tasks completed; one successful run used exactly 8 calls. This does not remove the demonstrated terminal-observation truncation path.",
            "budget_truncation_evidence": "The mechanism is proven; historical samples do not show systematic truncation, while the larger-budget recovery effect remains unmeasured.",
            "next_recommended_direction": "Restore provider connectivity, then rerun the same 10-step and 12-step experiment; make no Runtime or diagnosis-capability changes until valid traces exist.",
        }
    if any(result.get("metrics", {}).get("accepted") for result in larger_runs):
        return {
            "diagnosis_capability": "PRESENT in at least one of the two larger-budget traces",
            "baseline_budget_truncation": "CONSISTENT with the Phase 22.5 result",
            "larger_budget_recovery_effect": "See the per-run diagnosis chains and traces.",
            "eight_step_systematic_truncation": "Not shown across the existing Phase 22 and navigation samples.",
            "budget_truncation_evidence": "The larger-budget traces provide sample-level evidence.",
            "next_recommended_direction": "Study Step-Budget / Recovery Semantics before adding diagnosis capability.",
        }
    return {
        "diagnosis_capability": "NOT DEMONSTRATED in these two traces; inspect the per-run failure stage",
        "baseline_budget_truncation": "CONFIRMED as the reason Phase 22.5 had no post-failure inference",
        "larger_budget_recovery_effect": "Larger budgets did not produce an accepted run.",
        "eight_step_systematic_truncation": "Not shown across the existing Phase 22 and navigation samples.",
        "budget_truncation_evidence": "The original truncation is established, but increasing the budget alone was insufficient in this sample.",
        "next_recommended_direction": "Use the B/C traces to select the next diagnosis or completion investigation; leave Runtime unchanged until that analysis is reviewed.",
    }


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        output = sys.stdout
        with redirect_stdout(sys.stderr):
            steps = int(os.environ["PHASE23_MAX_AGENT_STEPS"])
            result = _run_child(steps)
        print(json.dumps(result, ensure_ascii=False), file=output)
        return
    payload = run_phase23_1()
    recovery = payload["phase23_1_recovery"]
    for budget, entry in recovery.get("budget_runs", {}).items():
        metrics = entry["result"].get("metrics", {})
        print(
            f"[Phase 23.1 {budget}] accepted={metrics.get('accepted')} "
            f"infra={entry['result'].get('infrastructure_failure')} attempts={entry['attempt_count']}",
            file=sys.stderr,
        )
    print(f"[Phase 23.1 preflight] {recovery['provider_preflight']['status']}", file=sys.stderr)
    print(json.dumps({"results": str(RESULTS), "report": str(REPORT)}, ensure_ascii=False))


if __name__ == "__main__":
    main_cli()
