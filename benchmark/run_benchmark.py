"""Run Benchmark v1 through the production Agent Loop, offline.

The benchmark uses scripted ``ModelReply`` objects as a provider-free model
substitute. Tool dispatch, permissions, sandbox checks, Coding Contracts,
Finish Gate, recovery, and Verifier all run through the repository runtime;
this file only builds deterministic replies and aggregates their evidence.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.metrics import aggregate, render_metrics_table  # noqa: E402
import acceptance  # noqa: E402
from eval.phase22_fixtures import (  # noqa: E402
    build_fixture,
    coding_contract,
    get_task,
    ground_truth_patch,
)
import main  # noqa: E402
import tools  # noqa: E402


MANIFEST = Path(__file__).resolve().parent / "tasks" / "manifest.json"
MAX_SCRIPTED_STEPS = 8
_NAV_SCENARIOS = {"navigation", "file_search", "long_file"}
_NAV_QUERIES = {
    "hidden_single_file_bug": "checkout_total",
    "error_string_diagnosis": "two segments",
    "cross_file_understanding": "total_after_discount",
    "allowed_multi_file_change": "quote_total_after_credit",
    "first_fix_insufficient": "discounted_amount",
    "forbidden_test_temptation": "isalnum",
}


@dataclass(frozen=True)
class RuntimeRun:
    trace: dict[str, Any]
    verdict: dict[str, Any] | None
    error: str | None
    stdout: str
    stderr: str
    changed_files: list[str]


def load_manifest(path: Path = MANIFEST) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != int(payload.get("task_count", 0)):
        raise ValueError("Benchmark v1 manifest task_count does not match tasks")
    if any(not isinstance(task, dict) or not task.get("id") for task in tasks):
        raise ValueError("every benchmark task needs an id")
    ids = [task.get("id") for task in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("benchmark task IDs must be unique")
    return tasks


def _reply(call_id: str, tool: str, arguments: dict[str, Any]) -> main.ModelReply:
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=tool,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    # Deliberately omit usage. The runtime must expose unknown usage as null;
    # a separate local estimate is kept only as a scripted diagnostic.
    return main.ModelReply(
        SimpleNamespace(content=None, tool_calls=[call]),
        "tool_calls",
        None,
        None,
        None,
    )


def _answer(content: str) -> main.ModelReply:
    return main.ModelReply(SimpleNamespace(content=content, tool_calls=[]), "stop", None, None, None)


def _estimate_reply_tokens(reply: main.ModelReply, prompt: str) -> int:
    calls = reply.message.tool_calls or []
    arguments = "".join(call.function.arguments or "{}" for call in calls)
    content = reply.message.content or ""
    return 48 + (len(prompt) + len(arguments) + len(content)) // 4


def _query_for(task: dict[str, Any], fixture_task: str) -> str:
    return str(task.get("query") or _NAV_QUERIES.get(fixture_task, fixture_task))


def _test_args(contract: acceptance.CodingTaskContract) -> dict[str, Any]:
    test = contract.test_command
    return {"command": test.command, "args": list(test.args), "cwd": test.cwd}


def _patch_reply(
    call_id: str,
    root: Path,
    fixture_task: str,
    relative: str,
    *,
    content_override: str | None = None,
) -> main.ModelReply:
    current = (root / relative).read_text(encoding="utf-8")
    if content_override is not None:
        return _reply(call_id, "write_file", {"path": relative, "content": content_override})
    return _reply(
        call_id,
        "apply_patch",
        {
            "path": relative,
            "old_text": current,
            "new_text": ground_truth_patch(fixture_task)[relative],
        },
    )


def _rejected_finish_tail(start: str = "finish") -> list[main.ModelReply]:
    return [
        _reply(f"{start}-{index}", "finish_task", {"summary": "The task is not complete."})
        for index in range(MAX_SCRIPTED_STEPS)
    ]


def _navigation_accepted(task: dict[str, Any], trace: dict[str, Any]) -> bool:
    """Require observable search/read evidence for read-only tasks.

    A final answer by itself is insufficient evidence that the runtime reached
    the requested source file. The production trace is the oracle input here;
    no private state or model text is trusted for this check.
    """

    events = trace.get("events", [])
    if not isinstance(events, list) or not trace.get("final_answer"):
        return False
    if any(event.get("mutation") for event in events if isinstance(event, dict)):
        return False

    target = str(task.get("target") or "").replace("\\", "/")
    search_seen = False
    target_read = False
    listed = False
    for event in events:
        if not isinstance(event, dict) or event.get("action") != "tool_call":
            continue
        if event.get("result_status") != "success":
            continue
        tool = event.get("tool")
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict):
            continue
        if tool == "search_text":
            search_seen = True
        elif tool == "read_file" and str(arguments.get("path", "")).replace("\\", "/") == target:
            target_read = True
        elif tool == "list_files":
            listed = True

    if not (search_seen and target_read):
        return False
    return task.get("scenario") != "navigation" or listed


def _build_replies(
    root: Path,
    task: dict[str, Any],
    contract: acceptance.CodingTaskContract | None,
) -> list[main.ModelReply]:
    fixture_task = str(task["fixture_task"])
    scenario = str(task["scenario"])
    spec = get_task(fixture_task)
    target = str(task.get("target") or spec.ground_truth.relevant_files[0])
    query = _query_for(task, fixture_task)
    if scenario in _NAV_SCENARIOS:
        replies = [_reply("list", "list_files", {"path": "."})]
        if scenario == "file_search" or scenario == "long_file":
            replies = []
        if scenario == "long_file":
            replies.extend(
                [
                    _reply("read-head", "read_file", {"path": target, "start_line": 1, "max_lines": 12}),
                    _reply("missing-range", "read_file", {"path": target, "start_line": 1000, "max_lines": 8})
                    if task["id"] == "long-03"
                    else _reply("search", "search_text", {"path": "docs", "query": "marker-071"}),
                ]
            )
            if task["id"] == "long-03":
                replies.append(_reply("search", "search_text", {"path": "docs", "query": "marker-071"}))
            replies.extend(
                [
                    _reply("read-context", "read_file", {"path": target, "start_line": 68, "max_lines": 10}),
                    _answer(f"The requested marker is in {target}."),
                ]
            )
            return replies
        replies.extend(
            [
                _reply("search", "search_text", {"path": ".", "query": query}),
                _reply("read", "read_file", {"path": target}),
                _answer(f"The matching implementation is {target}."),
            ]
        )
        return replies

    if contract is None:
        raise ValueError(f"coding scenario needs a contract: {scenario}")
    allowed = list(contract.allowed_paths)
    test = _test_args(contract)
    if scenario == "exact_modification":
        replies = [
            _reply("search", "search_text", {"path": ".", "query": query}),
            _reply("read", "read_file", {"path": allowed[0]}),
        ]
        replies.extend(_patch_reply(f"patch-{index}", root, fixture_task, path) for index, path in enumerate(allowed))
        replies.extend(
            [
                _reply("test", "run_command", test),
                _reply("finish", "finish_task", {"summary": "Applied the focused fix and verified the required test."}),
            ]
        )
        return replies

    if scenario == "multi_file":
        replies = [_reply("list", "list_files", {"path": "src"})]
        replies.extend(_reply(f"read-{index}", "read_file", {"path": path}) for index, path in enumerate(allowed))
        replies.extend(_patch_reply(f"patch-{index}", root, fixture_task, path) for index, path in enumerate(allowed))
        replies.extend(
            [
                _reply("test", "run_command", test),
                _reply("finish", "finish_task", {"summary": "Updated both allowed production paths and verified the test."}),
            ]
        )
        return replies

    if scenario in {"retry_repair", "recovery"}:
        relative = allowed[0]
        correct = ground_truth_patch(fixture_task)[relative]
        replies = [_reply("read", "read_file", {"path": relative})]
        if fixture_task == "first_fix_insufficient":
            insufficient = correct.replace(
                "rate = percent / 100 if percent > 1 else percent",
                "rate = percent / 100",
            )
            replies.append(_patch_reply("weak", root, fixture_task, relative, content_override=insufficient))
            replies.append(_reply("test-fail", "run_command", test))
            replies.append(_reply("repair", "write_file", {"path": relative, "content": correct}))
        else:
            # The multi-file recovery case fixes one allowed path first, then
            # uses the failed required test to reveal the remaining path.
            replies.append(_patch_reply("first", root, fixture_task, relative))
            replies.append(_reply("test-fail", "run_command", test))
            for index, other in enumerate(allowed[1:], start=1):
                replies.append(_patch_reply(f"repair-{index}", root, fixture_task, other))
        replies.append(_reply("test-pass", "run_command", test))
        replies.append(_reply("finish", "finish_task", {"summary": "Recovered from the failed test and finished after a fresh pass."}))
        return replies

    if scenario == "recovery_grace":
        # Reach the production turn-eight recovery boundary with a partial
        # multi-file mutation and a failing required test. The runtime creates
        # Recovery() itself; turns 9-11 then consume its mutation/test/finish
        # stages through the normal Tool Registry and Finish Gate.
        first, *remaining = allowed
        test_name = str(contract.test_command.args[-2])
        test_relative = f"tests/{test_name}"
        replies = [
            _reply("list", "list_files", {"path": "src"}),
            _reply("read-first", "read_file", {"path": first}),
            _reply("read-second", "read_file", {"path": remaining[0]}),
            _reply("search", "search_text", {"path": ".", "query": "quote_total_after_credit"}),
            _patch_reply("partial", root, fixture_task, first),
            _reply("read-test", "read_file", {"path": test_relative}),
            _reply("read-source-again", "read_file", {"path": remaining[0]}),
            _reply("test-fail-at-boundary", "run_command", test),
        ]
        replies.extend(_patch_reply(f"repair-{index}", root, fixture_task, path) for index, path in enumerate(remaining, start=1))
        replies.extend(
            [
                _reply("test-pass-after-recovery", "run_command", test),
                _reply("finish-after-recovery", "finish_task", {"summary": "Recovered after the required test failed and verified both paths."}),
            ]
        )
        return replies

    if scenario == "wrong_path":
        outside = "../benchmark-outside.txt" if task["id"] == "path-01" else str(root.parent / "benchmark-absolute.txt")
        return [_reply("outside", "apply_patch", {"path": outside, "old_text": "missing", "new_text": "must not be written"})] + _rejected_finish_tail("path-finish")

    if scenario == "permission_denied":
        relative = allowed[0]
        return [_patch_reply("denied", root, fixture_task, relative)] + _rejected_finish_tail("permission-finish")

    if scenario == "illegal_modification":
        replies = [_patch_reply("fix", root, fixture_task, path) for path in allowed]
        replies.append(_reply("test", "run_command", test))
        test_name = str(contract.test_command.args[-2])
        test_relative = f"tests/{test_name}"
        test_source = (root / test_relative).read_text(encoding="utf-8")
        replies.append(_reply("illegal", "write_file", {"path": test_relative, "content": test_source + "\n# illegal benchmark mutation\n"}))
        replies.extend([_reply("test-after-illegal", "run_command", test)])
        replies.extend(_rejected_finish_tail("illegal-finish"))
        return replies

    if scenario == "impossible":
        return [
            _reply("read", "read_file", {"path": allowed[0]}),
            _reply("test", "run_command", test),
        ] + _rejected_finish_tail("impossible-finish")

    if scenario == "finish_gate":
        replies: list[main.ModelReply] = []
        if task["id"] == "finish-02":
            replies.append(_reply("finish-early", "finish_task", {"summary": "Attempting to finish before editing."}))
        replies.extend(_patch_reply(f"patch-{index}", root, fixture_task, path) for index, path in enumerate(allowed))
        if task["id"] != "finish-02":
            replies.append(_reply("finish-before-test", "finish_task", {"summary": "Attempting to finish before the required test."}))
        replies.extend(
            [
                _reply("test", "run_command", test),
                _reply("finish-final", "finish_task", {"summary": "The required test passed after the mutation."}),
            ]
        )
        return replies

    if scenario == "duplicate_success":
        return [
            _reply("search-1", "search_text", {"path": ".", "query": query}),
            _reply("search-2", "search_text", {"path": ".", "query": query}),
            _reply("read", "read_file", {"path": allowed[0]}),
            _patch_reply("patch", root, fixture_task, allowed[0]),
            _reply("test", "run_command", test),
            _reply("finish", "finish_task", {"summary": "Handled the duplicate, verified the fix, and finished."}),
        ]
    raise ValueError(f"unknown benchmark scenario: {scenario}")


def _required_test_was_run(trace: dict[str, Any], contract: acceptance.CodingTaskContract) -> bool:
    required = _test_args(contract)
    for event in trace.get("events", []):
        if event.get("tool") != "run_command" or event.get("result_status") != "success":
            continue
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if arguments == required and str(event.get("exit_code")) == "0":
            return True
    return False


def _runtime_case(
    root: Path,
    task: dict[str, Any],
    replies: list[main.ModelReply],
    contract: acceptance.CodingTaskContract | None,
) -> RuntimeRun:
    if not replies:
        raise ValueError("a scripted runtime case needs at least one reply")
    before = acceptance.snapshot_workspace(root)
    state = acceptance.TaskState(initial_snapshot=dict(before)) if contract else None
    trace = main.CodingTaskTrace()
    if contract is None:
        user_prompt = str(task["prompt"])
    else:
        guidance = acceptance.coding_task_guidance(contract)
        user_prompt = contract.instruction + "\n\n" + guidance
    messages = [
        {"role": "system", "content": main.SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    original_main_workspace = main.WORKSPACE_DIR
    original_tools_workspace = tools.WORKSPACE_DIR
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    error: str | None = None
    try:
        main.WORKSPACE_DIR = root
        tools.WORKSPACE_DIR = root
        approval = (lambda *_args: False) if task["scenario"] == "permission_denied" else (lambda *_args: True)
        with (
            patch.object(main, "ask", side_effect=replies[1:]),
            patch.dict(main.os.environ, {"CONTEXT_MODE": "WRITE_ONLY"}),
            redirect_stdout(captured_out),
            redirect_stderr(captured_err),
        ):
            main.run_agent_loop(
                None,
                "benchmark-scripted-model",
                messages,
                replies[0],
                set(),
                approval,
                trace=trace,
                required_test=(
                    (contract.test_command.command, tuple(contract.test_command.args), contract.test_command.cwd)
                    if contract
                    else None
                ),
                contract=contract,
                task_state=state,
            )
    except Exception as exc:  # keep infrastructure failures in the denominator
        error = f"{type(exc).__name__}: {exc}"
    finally:
        main.WORKSPACE_DIR = original_main_workspace
        tools.WORKSPACE_DIR = original_tools_workspace
    trace_summary = trace.summary()
    verdict: dict[str, Any] | None = None
    if contract is not None:
        try:
            verifier_started = time.perf_counter()
            verdict = acceptance.verify_contract(
                contract,
                root,
                before,
                task_state=state,
                agent_final_answer_present=trace.final_answer is not None,
                agent_ran_required_test=_required_test_was_run(trace_summary, contract),
                max_steps_reached=trace.max_steps_reached,
                runtime_exception=getattr(state, "unresolved_runtime_error", None),
                command_runner=tools.run_command,
            )
            record_verifier = getattr(trace, "record_verifier", None)
            if record_verifier is not None:
                record_verifier(verdict, duration_seconds=time.perf_counter() - verifier_started)
                trace_summary = trace.summary()
        except Exception as exc:
            error = error or f"{type(exc).__name__}: {exc}"
    changed = acceptance.changed_files(before, acceptance.snapshot_workspace(root))
    return RuntimeRun(trace_summary, verdict, error, captured_out.getvalue(), captured_err.getvalue(), changed)


def _prepare_long_file(root: Path) -> None:
    path = root / "docs" / "long_notes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"section-{index:03d}: repository navigation and controlled tool use." for index in range(1, 121)]
    lines[70] = "section-071: marker-071 describes percentage totals and their verifier evidence."
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _status_counts(trace: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in trace.get("events", []):
        status = event.get("result_status")
        if not status or event.get("action") != "tool_call":
            continue
        counts[str(status)] = counts.get(str(status), 0) + 1
    return dict(sorted(counts.items()))


def _scripted_estimate(replies: list[main.ModelReply], prompt: str) -> int:
    return sum(_estimate_reply_tokens(reply, prompt) for reply in replies)


def _scenario_evidence(task: dict[str, Any], run: RuntimeRun) -> bool:
    """Check the safety or recovery primitive, separately from task completion."""
    counts = _status_counts(run.trace)
    scenario = task["scenario"]
    if scenario in _NAV_SCENARIOS:
        return not run.changed_files
    if scenario in {"wrong_path", "permission_denied"}:
        return not run.changed_files and counts.get("policy_rejected", 0) > 0
    if scenario == "illegal_modification":
        return bool(run.verdict and run.verdict.get("unexpected_changes")) and counts.get("finish_rejected", 0) > 0
    if scenario == "impossible":
        return not run.changed_files and counts.get("command_failed", 0) > 0 and counts.get("finish_rejected", 0) > 0
    if scenario in {"retry_repair", "recovery"}:
        return counts.get("command_failed", 0) > 0 and run.trace.get("successful_commands", 0) > 0
    if scenario == "recovery_grace":
        state = run.trace.get("recovery_state") or {}
        return run.trace.get("recovery_grace") == "FAIL" and state.get("stage") == "FINISHED"
    if scenario == "finish_gate":
        return counts.get("finish_rejected", 0) > 0 and counts.get("finish_accepted", 0) == 1
    if scenario == "duplicate_success":
        return counts.get("duplicate_blocked", 0) > 0
    return True


def _public_fixture_paths(value: Any, workspace: Path) -> Any:
    """Keep portable fixture evidence without publishing the host user path."""
    if isinstance(value, str):
        for path, label in ((workspace, "<workspace>"), (workspace.parent, "<fixture_root>")):
            for spelling in (str(path), path.as_posix()):
                value = value.replace(spelling, label).replace(json.dumps(spelling)[1:-1], label)
        return value
    if isinstance(value, dict):
        return {key: _public_fixture_paths(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_fixture_paths(item, workspace) for item in value]
    return value


def run_one(task: dict[str, Any], repetition: int) -> dict[str, Any]:
    started = time.perf_counter()
    scenario = str(task["scenario"])
    with tempfile.TemporaryDirectory(prefix=f"mini-agent-benchmark-{task['id']}-") as directory:
        workspace = Path(directory) / "workspace"
        build_fixture(workspace)
        if scenario == "long_file":
            _prepare_long_file(workspace)
        fixture_task = str(task["fixture_task"])
        contract = None
        if scenario not in _NAV_SCENARIOS:
            contract = acceptance.CodingTaskContract.from_dict(coding_contract(fixture_task))
        replies = _build_replies(workspace, task, contract)
        run = _runtime_case(workspace, task, replies, contract)
        # ``verdict`` already compares against the pre-run snapshot. The
        # changed files below are only a compact local artifact summary.
        changed = sorted(run.changed_files)
        accepted = (
            bool(run.verdict and run.verdict.get("accepted"))
            if contract is not None
            else _navigation_accepted(task, run.trace) and not changed and run.error is None
        )
        unexpected = list(run.verdict.get("unexpected_changes", [])) if run.verdict else []
        valid = run.error is None
        positive = bool(task["expected_acceptance"])
        evidence_matches = _scenario_evidence(task, run)
        if scenario == "wrong_path":
            outside = workspace.parent / ("benchmark-outside.txt" if task["id"] == "path-01" else "benchmark-absolute.txt")
            evidence_matches = evidence_matches and not outside.exists()
        prompt = str(task["prompt"])
        trace = run.trace
        record = {
            "task_id": task["id"],
            "category": task["category"],
            "fixture_task": fixture_task,
            "scenario": scenario,
            "repetition": repetition,
            "accepted": accepted,
            "expected_acceptance": positive,
            "deterministic_acceptance": accepted,
            "deterministic_match": valid and accepted == positive and evidence_matches,
            "scenario_evidence_matches": evidence_matches,
            "manual_review_required": bool(task.get("manual_review")),
            "manual_review_reason": task.get("manual_review_reason"),
            "manual_review_pass": None,
            "model_calls": trace.get("model_calls", 0),
            "tool_calls": trace.get("tool_calls", 0),
            "total_tokens": trace.get("total_tokens"),
            "scripted_token_estimate": _scripted_estimate(replies, prompt),
            "max_steps_reached": bool(trace.get("max_steps_reached")),
            "unexpected_modification": bool(unexpected),
            "changed_files": changed,
            "unexpected_files": unexpected,
            "runtime_error": not valid,
            "runtime_error_detail": run.error,
            "test_attempts": trace.get("run_command_calls", 0),
            "test_failures": trace.get("failed_commands", 0),
            # A recovery case is observable at the runtime boundary as a
            # failed required test followed by a successful rerun. Keep this
            # derived marker independent of any internal recovery label.
            "recovery_used": bool(
                trace.get("recovery_grace")
                or trace.get("recovery_state")
                or (
                    trace.get("failed_commands", 0) > 0
                    and any(
                        event.get("tool") == "run_command"
                        and event.get("result_status") == "success"
                        and str(event.get("exit_code")) == "0"
                        for event in trace.get("events", [])
                    )
                )
            ),
            "recovery_grace": trace.get("recovery_grace"),
            "recovery_state": trace.get("recovery_state"),
            "finish_attempts": trace.get("finish_task_calls", 0),
            "finish_rejections": trace.get("finish_rejections", 0),
            "tool_statuses": _status_counts(trace),
            "events": trace.get("events", []),
            "verifier": (
                {key: run.verdict.get(key) for key in ("accepted", "artifact_passed", "interaction_completed", "final_test_status", "reasons")}
                if run.verdict
                else None
            ),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "source": "production_runtime_scripted",
        }
        return _public_fixture_paths(record, workspace)


def render_report(payload: dict[str, Any]) -> str:
    metrics = payload["metrics"]
    rows = [
        "# Benchmark v1 report",
        "",
        "This report was generated by the production Agent Loop with provider-free scripted replies. It is a runtime regression signal, not a live model quality claim.",
        "",
        f"- Tasks: `{metrics['tasks']}` rows (`{payload['repetitions']}` repetition(s) per selected task).",
        f"- Deterministic oracle matches: `{metrics['deterministic_match_rate']}`.",
        f"- Positive acceptance rate: `{metrics['positive_acceptance_rate']}` (intentionally rejected guard cases are excluded).",
        "- Manual review: deterministic checks do not complete the semantic review queue; pending cases remain explicitly marked.",
        "",
        render_metrics_table(metrics),
        "",
        "`Median Tokens` and `P95 Tokens` are `null` when scripted replies omit provider usage. `Median Scripted Tokens` is a separate diagnostic estimate and is not a provider cost claim.",
        "",
        "## Acceptance interpretation",
        "",
        "`Acceptance Rate` is computed over every attempted row, including runtime errors and intentionally rejected negative controls. `Positive Acceptance Rate` only includes rows marked `expected_acceptance=true`. Neither metric is a claim about a live provider or semantic quality.",
        "",
        "## Task coverage",
        "",
        "| Category | Rows | Accepted | Expected accepted | Manual review |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    categories: dict[str, list[dict[str, Any]]] = {}
    for row in payload["results"]:
        categories.setdefault(str(row["category"]), []).append(row)
    for category, category_rows in sorted(categories.items()):
        rows.append(
            f"| {category} | {len(category_rows)} | "
            f"{sum(bool(item['accepted']) for item in category_rows)} | "
            f"{sum(bool(item['expected_acceptance']) for item in category_rows)} | "
            f"{sum(bool(item['manual_review_required']) for item in category_rows)} |"
        )
    rows.extend(
        [
            "",
            "## Status counts",
            "",
            "The JSON result retains per-task status counts, event traces, changed files, and runtime errors so a failed row can be replayed locally.",
            "",
            "```json",
            json.dumps(
                {
                    "status_counts": {
                        status: sum(int(row.get("tool_statuses", {}).get(status, 0)) for row in payload["results"])
                        for status in sorted(
                            {
                                status
                                for row in payload["results"]
                                for status in row.get("tool_statuses", {})
                            }
                        )
                    }
                },
                indent=2,
            ),
            "```",
            "",
        ]
    )
    return "\n".join(rows)


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--task-id", action="append", help="run only this task; repeat the option")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path, help="write JSON results and a sibling Markdown report")
    parser.add_argument("--list", action="store_true", help="list task IDs and exit")
    parser.add_argument("--json", action="store_true", help="print the full JSON payload")
    args = parser.parse_args(argv)
    tasks = load_manifest(args.manifest)
    if args.list:
        for task in tasks:
            print(f"{task['id']}\t{task['category']}\t{task['scenario']}")
        return 0
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    selected = tasks
    if args.task_id:
        wanted = set(args.task_id)
        selected = [task for task in tasks if task["id"] in wanted]
        missing = sorted(wanted - {task["id"] for task in selected})
        if missing:
            parser.error(f"unknown task id(s): {', '.join(missing)}")
    results = [
        run_one(task, repetition)
        for repetition in range(1, args.repetitions + 1)
        for task in selected
    ]
    payload = {
        "benchmark": "v1",
        "source": "production_runtime_scripted",
        "provider_used": False,
        "api_key_required": False,
        "manifest": (
            args.manifest.resolve().relative_to(ROOT).as_posix()
            if args.manifest.resolve().is_relative_to(ROOT)
            else "<external-manifest>"
        ),
        "repetitions": args.repetitions,
        "selected_tasks": len(selected),
        "results": results,
        "metrics": aggregate(results),
    }
    report = render_report(payload)
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        report_path = output.with_suffix(".md")
        report_path.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote {output}")
        print(f"wrote {report_path}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(report)
    failed_rows = [
        row
        for row in results
        if bool(row.get("runtime_error")) or not bool(row.get("deterministic_match"))
    ]
    return 1 if failed_rows else 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
