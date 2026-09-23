"""Compare diagnosis recovery at larger model-turn budgets without changing Runtime."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import json
import os
from pathlib import Path
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
    with tempfile.TemporaryDirectory(prefix=f"phase23-budget-{steps}-") as directory:
        root = Path(directory) / "workspace"
        phase22_5.setup("E", root)
        environment = {
            **os.environ,
            "AGENT_WORKSPACE": str(root),
            "TOOL_APPROVAL_MODE": "ALLOW",
            "CONTEXT_MODE": "WRITE_ONLY",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PHASE23_MAX_AGENT_STEPS": str(steps),
        }
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


def _analysis(result: dict) -> dict:
    metrics = result.get("metrics", {})
    chain = metrics.get("tool_chain", [])
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
    assistant_turn_messages = []
    turn = 0
    for message in result.get("canonical_history", []):
        if message.get("role") == "assistant":
            turn += 1
            if turn == next_turn:
                assistant_turn_messages.append(message)
    next_message = assistant_turn_messages[0] if assistant_turn_messages else None
    next_content = next_message.get("content") if next_message else None
    failed_result = next((event.get("result", "") for event in attempts if event is failed), "")
    failure_name = "test_one_percent_boundary"
    visible_failure_reference = bool(
        next_content and failure_name in next_content
    ) or any(
        failure_name in event.get("arguments", "")
        or failure_name in event.get("result", "")
        for event in post_failure[:2]
    )
    return {
        "first_mutation_turn": mutation_events[0].get("turn") if mutation_events else None,
        "first_required_test_failure_turn": failed.get("turn") if failed else None,
        "failure_output": failed.get("result") if failed else None,
        "next_model_turn": next_turn if next_message else None,
        "next_model_turn_content": next_content,
        "next_model_turn_actions": [event for event in post_failure if event.get("turn") == next_turn],
        "next_model_turn_visibly_references_failure": visible_failure_reference,
        "post_failure_tool_chain": post_failure,
        "first_post_failure_read_or_search": first_read_search,
        "second_mutation": second_mutation,
        "required_test_attempts": len(attempts),
        "required_test_failures": sum(event.get("exit_code") not in (None, 0) for event in attempts),
        "required_test_pass_turns": [event.get("turn") for event in successful_tests],
        "finish_task_turn": finish.get("turn") if finish else None,
        "accepted": metrics.get("accepted", False),
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


def run_phase23() -> dict:
    baseline = _baseline_run()
    manifest = _prepare_manifest(baseline)
    payload = {
        "phase": 23,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "manifest": manifest,
        "network_diagnostics": _network_diagnostics(),
        "historical_budget_evidence": _historical_budget_evidence(),
        "runs": {
            "8": {"valid_real_run": True, "attempt_count": 0, "reused_from": "phase22_5_results.json#results.E", "result": baseline},
            "10": _run_budget(10),
            "12": _run_budget(12),
        },
    }
    payload["status"] = (
        "completed"
        if all(payload["runs"][str(budget)]["valid_real_run"] for budget in (10, 12))
        else "infrastructure_blocked"
    )
    for budget in (10, 12):
        result = payload["runs"][str(budget)]["result"]
        if not result.get("infrastructure_failure") and result.get("model") != manifest["model"]:
            raise ValueError(f"Run {budget} model differs from baseline")
    payload["conclusions"] = _conclusions(payload)
    RESULTS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(render_report(payload), encoding="utf-8")
    return payload


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
    payload = run_phase23()
    for budget in (10, 12):
        entry = payload["runs"][str(budget)]
        metrics = entry["result"].get("metrics", {})
        print(
            f"[Phase 23 {budget}] accepted={metrics.get('accepted')} "
            f"infra={entry['result'].get('infrastructure_failure')} attempts={entry['attempt_count']}",
            file=sys.stderr,
        )
    print(json.dumps({"results": str(RESULTS), "report": str(REPORT)}, ensure_ascii=False))


if __name__ == "__main__":
    main_cli()
