"""Phase 19: measure required-test visibility without changing Agent Runtime."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from .navigation_eval import SCENARIOS  # noqa: E402
from .navigation_fixtures import build_fixture, coding_contract, get_spec, validate_fixture  # noqa: E402
from .navigation_metrics import calculate_navigation_metrics  # noqa: E402
from .navigation_stability import is_provider_failure  # noqa: E402


SCENARIO_NAME = "medium_symbol_coding"
PLANNED_TREATMENT_RUNS = 3
BASELINE_COMMIT = "8c0620d"
EXACT_REQUIRED_TEST = {
    "command": "python",
    "args": ("-m", "unittest", "discover", "-s", "tests", "-p", "test_discount.py", "-q"),
    "cwd": ".",
}
REQUIRED_TEST_TEXT = "python -m unittest discover -s tests -p test_discount.py -q"


def build_treatment_system_prompt() -> str:
    """Expose only the contract command fact; do not add completion guidance."""
    from main import SYSTEM_PROMPT

    return f"{SYSTEM_PROMPT}\n\nRequired test command: {REQUIRED_TEST_TEXT}"


def _parse_arguments(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _test_count(result: str) -> int | None:
    match = re.search(r"Ran\s+(\d+)\s+tests?\b", result or "")
    return int(match.group(1)) if match else None


def _is_exact_required_test(command: dict) -> bool:
    return (
        command.get("command") == EXACT_REQUIRED_TEST["command"]
        and tuple(command.get("args", [])) == EXACT_REQUIRED_TEST["args"]
        and command.get("cwd", ".") == EXACT_REQUIRED_TEST["cwd"]
    )


def _message_run_commands(messages: list[dict], trace_events: list[dict]) -> list[dict]:
    """Extract full run_command results from canonical history in a child run."""
    calls = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        results = messages[index + 1 : index + 1 + len(tool_calls)]
        for call, result in zip(tool_calls, results):
            function = call.get("function", {})
            if function.get("name") != "run_command" or result.get("role") != "tool":
                continue
            arguments = _parse_arguments(function.get("arguments", "{}"))
            content = result.get("content", "")
            calls.append(
                {
                    "command": arguments.get("command"),
                    "args": arguments.get("args", []),
                    "cwd": arguments.get("cwd", "."),
                    "exit_code": next(
                        (
                            line.removeprefix("Exit code: ")
                            for line in content.splitlines()
                            if line.startswith("Exit code: ")
                        ),
                        None,
                    ),
                    "result": content,
                    "test_count": _test_count(content),
                    "ineffective_test_attempt": _test_count(content) == 0,
                    "completion_hint": "[Completion status]" in content,
                }
            )

    trace_commands = [
        event
        for event in trace_events
        if event.get("action") == "tool_call" and event.get("tool") == "run_command"
    ]
    for command, event in zip(calls, trace_commands):
        command["turn"] = event.get("turn")
        command["classification"] = event.get("classification")
    return calls


def _fallback_run_commands(raw_result: dict) -> list[dict]:
    """Recover command arguments from an existing Phase 18.5 trace."""
    process_stderr = raw_result.get("process_stderr", "")
    test_counts = [
        int(match.group(1))
        for match in re.finditer(r"Ran\s+(\d+)\s+tests?\b", process_stderr)
    ]
    commands = []
    for index, event in enumerate(
        event
        for event in raw_result.get("metrics", {}).get("tool_chain", [])
        if event.get("tool") == "run_command"
    ):
        arguments = _parse_arguments(event.get("arguments", "{}"))
        test_count = test_counts[index] if index < len(test_counts) else None
        commands.append(
            {
                "turn": event.get("turn"),
                "command": arguments.get("command"),
                "args": arguments.get("args", []),
                "cwd": arguments.get("cwd", "."),
                "exit_code": event.get("exit_code"),
                "result": event.get("result", ""),
                "test_count": test_count,
                "ineffective_test_attempt": test_count == 0,
                "completion_hint": False,
                "classification": "SUCCESSFUL_COMMAND"
                if event.get("exit_code") == "0"
                else "FAILED_COMMAND",
            }
        )
    return commands


def _compact_chain(raw_result: dict) -> list[str]:
    chain = [event.get("tool", "?") for event in raw_result.get("metrics", {}).get("tool_chain", [])]
    if raw_result.get("metrics", {}).get("final_answer_present"):
        chain.append("Final")
    return chain


def normalise_run(raw_result: dict, run_index: int, condition: str) -> dict:
    metrics = raw_result.get("metrics", {})
    trace = raw_result.get("trace", {})
    acceptance = raw_result.get("acceptance") or {}
    commands = raw_result.get("observed_run_commands") or _fallback_run_commands(raw_result)
    exact_matches = [command for command in commands if _is_exact_required_test(command)]
    hint_commands = [command for command in commands if command.get("completion_hint")]
    final_turns = [
        event.get("turn")
        for event in trace.get("events", [])
        if event.get("action") == "final_answer"
    ]
    provider_failure = bool(raw_result.get("provider_failure", is_provider_failure(raw_result)))
    return {
        "condition": condition,
        "scenario": raw_result.get("scenario", SCENARIO_NAME),
        "repo_size": raw_result.get("repo_size", "MEDIUM"),
        "task_type": raw_result.get("task_type", "coding"),
        "run": run_index,
        "attempt_index": raw_result.get("attempt_index", 1),
        "provider_failure": provider_failure,
        "provider_errors": metrics.get("runtime_errors", []) or raw_result.get("process_error", ""),
        "required_test_visible": condition == "treatment",
        "required_test": {
            "command": EXACT_REQUIRED_TEST["command"],
            "args": list(EXACT_REQUIRED_TEST["args"]),
            "cwd": EXACT_REQUIRED_TEST["cwd"],
        },
        "model_calls": metrics.get("model_calls", 0),
        "tool_calls": metrics.get("tool_calls", 0),
        "list_files_calls": metrics.get("list_files_calls", 0),
        "search_text_calls": metrics.get("search_text_calls", 0),
        "read_file_calls": metrics.get("read_file_calls", 0),
        "apply_patch_calls": metrics.get("apply_patch_calls", trace.get("apply_patch_calls", 0)),
        "write_file_calls": metrics.get("write_file_calls", trace.get("write_file_calls", 0)),
        "run_command_calls": metrics.get("run_command_calls", trace.get("run_command_calls", 0)),
        "tool_chain": _compact_chain(raw_result),
        "run_commands": commands,
        "agent_ran_required_test": bool(
            exact_matches and any(command.get("exit_code") == "0" for command in exact_matches)
        ),
        "required_test_exact_match_turn": exact_matches[0].get("turn") if exact_matches else None,
        "required_test_exit_code": exact_matches[0].get("exit_code") if exact_matches else None,
        "completion_hint_triggered": bool(hint_commands),
        "completion_hint_turn": hint_commands[0].get("turn") if hint_commands else None,
        "ineffective_test_attempt": any(
            command.get(
                "ineffective_test_attempt",
                _test_count(command.get("result", "")) == 0,
            )
            for command in commands
        ),
        "final_answer_present": bool(metrics.get("final_answer_present")),
        "final_answer_turn": final_turns[0] if final_turns else None,
        "max_steps_reached": bool(metrics.get("max_steps_reached", trace.get("max_steps_reached", False))),
        "artifact_passed": acceptance.get("artifact_passed"),
        "interaction_completed": acceptance.get("interaction_completed"),
        "accepted": acceptance.get("accepted", raw_result.get("accepted", False)),
        "changed_files": acceptance.get("changed_files", []),
        "unexpected_changes": acceptance.get("unexpected_changes", []),
        "final_test_exit_code": acceptance.get("final_test_exit_code"),
        "prompt_tokens": metrics.get("prompt_tokens", 0),
        "completion_tokens": metrics.get("completion_tokens", 0),
        "total_tokens": metrics.get("total_tokens", 0),
        "raw_result": raw_result,
    }


def _rate(records: list[dict], field: str) -> dict:
    valid = [record for record in records if not record["provider_failure"]]
    used = sum(bool(record.get(field)) for record in valid)
    return {"used": used, "denominator": len(valid), "rate": f"{used}/{len(valid)}"}


def aggregate_condition(records: list[dict]) -> dict:
    valid = [record for record in records if not record["provider_failure"]]
    return {
        "planned_runs": len(records),
        "valid_runs": len(valid),
        "provider_failures": sum(record["provider_failure"] for record in records),
        "exact_required_test": _rate(valid, "agent_ran_required_test"),
        "completion_hint": _rate(valid, "completion_hint_triggered"),
        "final_answer": _rate(valid, "final_answer_present"),
        "max_steps": _rate(valid, "max_steps_reached"),
        "accepted": _rate(valid, "accepted"),
        "ineffective_zero_test_attempts": sum(
            bool(record.get("ineffective_test_attempt")) for record in valid
        ),
        "model_calls": [record["model_calls"] for record in valid],
        "tool_calls": [record["tool_calls"] for record in valid],
        "total_tokens": [record["total_tokens"] for record in valid],
    }


def _run_child() -> dict:
    import acceptance
    import main

    scenario = SCENARIOS[SCENARIO_NAME]
    root = Path(os.environ["AGENT_WORKSPACE"]).resolve()
    spec = get_spec(scenario["size"])
    validate_fixture(root, spec)
    contract_data = coding_contract(spec)
    messages = [
        {"role": "system", "content": build_treatment_system_prompt()},
        {"role": "user", "content": scenario["task"]},
    ]
    model_replies = []
    trace = main.CodingTaskTrace()
    runtime_errors = []
    client = None
    before = acceptance.snapshot_workspace(root)
    real_ask = main.ask

    def recording_ask(llm_client, model_name, history):
        reply = real_ask(llm_client, model_name, history)
        model_replies.append(reply)
        return reply

    main.ask = recording_ask
    try:
        config = main.load_config()
        client = main.build_client(config)
        first_reply = main.ask(client, config.model, messages)
        main.log_reply(1, first_reply)
        required_test = (
            EXACT_REQUIRED_TEST["command"],
            EXACT_REQUIRED_TEST["args"],
            EXACT_REQUIRED_TEST["cwd"],
        )
        main.run_agent_loop(
            client,
            config.model,
            messages,
            first_reply,
            set(),
            main.always_allow,
            trace=trace,
            required_test=required_test,
        )
        model_name = config.model
    except BaseException as exc:
        runtime_errors.append(f"{type(exc).__name__}: {exc}")
        model_name = os.environ.get("OPENAI_MODEL", "unknown")
    finally:
        main.ask = real_ask
        if client is not None:
            client.close()

    metrics = calculate_navigation_metrics(model_replies, messages, spec.target_file)
    metrics["runtime_errors"] = runtime_errors
    metrics["finish_reason"] = [getattr(reply, "finish_reason", None) for reply in model_replies]
    metrics["max_steps_reached"] = trace.max_steps_reached
    metrics["apply_patch_calls"] = trace.apply_patch_calls
    metrics["write_file_calls"] = trace.write_file_calls
    metrics["run_command_calls"] = trace.run_command_calls
    metrics["final_answer_present"] = metrics["final_answer"] is not None
    messages_commands = _message_run_commands(messages, trace.events)
    acceptance_result = acceptance.verify_contract(
        acceptance.CodingTaskContract.from_dict(contract_data),
        root,
        before,
        agent_final_answer_present=metrics["final_answer_present"],
        agent_ran_required_test=False,
        max_steps_reached=trace.max_steps_reached,
        runtime_exception=runtime_errors[0] if runtime_errors else None,
    )
    return {
        "scenario": SCENARIO_NAME,
        "repo_size": spec.name,
        "task_type": scenario["task_type"],
        "task": scenario["task"],
        "ground_truth_file": spec.target_file,
        "fixture": spec.as_dict(),
        "model": model_name,
        "required_test_visible": True,
        "visibility_text": REQUIRED_TEST_TEXT,
        "metrics": metrics,
        "trace": trace.summary(),
        "observed_run_commands": messages_commands,
        "acceptance": acceptance_result,
        "accepted": acceptance_result["accepted"],
        "process_exit_code": 0 if not runtime_errors else 1,
    }


def _timeout_result(error: subprocess.TimeoutExpired) -> dict:
    timeout = getattr(error, "timeout", "unknown")
    message = f"TimeoutExpired after {timeout} seconds"
    return {
        "scenario": SCENARIO_NAME,
        "accepted": False,
        "process_exit_code": 124,
        "process_error": message,
        "metrics": {"runtime_errors": [f"Provider timeout: {message}"]},
        "trace": {},
    }


def _run_attempt(root: Path) -> dict:
    environment = {
        **os.environ,
        "AGENT_WORKSPACE": str(root),
        "TOOL_APPROVAL_MODE": "ALLOW",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        completed = subprocess.run(
            [
                str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "eval.required_test_visibility",
                "--child",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            env=environment,
            timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        return _timeout_result(error)

    try:
        result = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {
            "scenario": SCENARIO_NAME,
            "accepted": False,
            "process_exit_code": completed.returncode,
            "process_error": completed.stderr.decode("utf-8", "replace")[-4000:],
            "metrics": {"runtime_errors": ["child output was not valid JSON"]},
        }
    result["process_exit_code"] = completed.returncode
    result["process_stderr"] = completed.stderr.decode("utf-8", "replace")[-4000:]
    return result


def _payload(control: list[dict], treatment: list[dict], status: str) -> dict:
    return {
        "phase": "19",
        "status": status,
        "baseline_commit": BASELINE_COMMIT,
        "control_source": "eval/navigation_stability_results.json MEDIUM runs 1-3",
        "planned_treatment_runs": PLANNED_TREATMENT_RUNS,
        "conditions": {
            "fixture": "MEDIUM Phase 18 fixture",
            "user_task": SCENARIOS[SCENARIO_NAME]["task"],
            "system_prompt": "main.SYSTEM_PROMPT unchanged plus one factual treatment line",
            "visibility_text": REQUIRED_TEST_TEXT,
            "runtime_unchanged": True,
            "tool_schema_unchanged": True,
            "max_agent_steps": 8,
            "context_mode": os.environ.get("CONTEXT_MODE", "WRITE_ONLY"),
            "permission_mode": "ALLOW",
            "real_run_cap": 3,
            "provider_failure_policy": "record failure; do not exceed the three-run cap",
        },
        "control": {
            "condition": "control",
            "required_test_visible": False,
            "runs": control,
            "aggregate": aggregate_condition(control),
        },
        "treatment": {
            "condition": "treatment",
            "required_test_visible": True,
            "runs": treatment,
            "aggregate": aggregate_condition(treatment),
        },
    }


def _format_rate(value: dict) -> str:
    return value["rate"]


def render_report(payload: dict) -> str:
    control = payload["control"]
    treatment = payload["treatment"]
    lines = [
        "# Phase 19：Required-Test Visibility Experiment",
        "",
        "本阶段只修改评测 harness，不修改 Agent Runtime、Tool Schema、Completion Hint、Acceptance、",
        "MAX_AGENT_STEPS、Permission、Sandbox 或 Context。Treatment 只向模型提供一条事实：",
        f"`Required test command: {REQUIRED_TEST_TEXT}`。没有增加‘必须 Final’或‘优先执行’等指导。",
        "",
        "## 实验条件",
        "",
        f"- baseline：`{payload['baseline_commit']}`；Control 直接复用 Phase 18.5 MEDIUM 三次记录。",
        f"- Treatment：MEDIUM 干净 fixture × {payload['planned_treatment_runs']}；每次独立临时 workspace。",
        f"- Treatment provider failure：{treatment['aggregate']['provider_failures']}；Provider failure 不计入行为分母。",
        "- n=3 只用于观察，不作统计显著性结论。",
        "",
        "## 结果总表",
        "",
        "| condition | run | exact test | hint | final | max steps | accepted | model calls | tokens |",
        "|---|---:|---|---|---|---|---|---:|---:|",
    ]
    for group in (control, treatment):
        for record in group["runs"]:
            lines.append(
                f"| {record['condition']} | {record['run']} | "
                f"{str(record['agent_ran_required_test']).lower()} | "
                f"{str(record['completion_hint_triggered']).lower()} | "
                f"{str(record['final_answer_present']).lower()} | "
                f"{str(record['max_steps_reached']).lower()} | "
                f"{str(record['accepted']).lower()} | "
                f"{record['model_calls']} | {record['total_tokens']} |"
            )

    lines.extend(
        [
            "",
            "## 聚合结果",
            "",
            "| condition | exact test | hint | final | max steps | accepted | zero-test attempts | model calls | tool calls | tokens |",
            "|---|---:|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )
    for group in (control, treatment):
        aggregate = group["aggregate"]
        lines.append(
            f"| {group['condition']} | {_format_rate(aggregate['exact_required_test'])} | "
            f"{_format_rate(aggregate['completion_hint'])} | {_format_rate(aggregate['final_answer'])} | "
            f"{_format_rate(aggregate['max_steps'])} | {_format_rate(aggregate['accepted'])} | "
            f"{aggregate['ineffective_zero_test_attempts']} | "
            f"{aggregate['model_calls']} | {aggregate['tool_calls']} | {aggregate['total_tokens']} |"
        )

    lines.extend(["", "## Control Tool Chain", ""])
    for record in control["runs"]:
        lines.append(f"- run {record['run']}: `{' → '.join(record['tool_chain'])}`")
    lines.extend(["", "## Treatment Tool Chain", ""])
    for record in treatment["runs"]:
        lines.append(f"- run {record['run']}: `{' → '.join(record['tool_chain'])}`")
        for command in record["run_commands"]:
            args = " ".join(command.get("args", []))
            lines.append(
                f"  - Turn {command.get('turn')}: `{command.get('command')} {args}` "
                f"exit={command.get('exit_code')} tests={command.get('test_count')} "
                f"hint={str(command.get('completion_hint')).lower()}"
            )

    provider_failures = [
        record
        for group in (control, treatment)
        for record in group["runs"]
        if record["provider_failure"]
    ]
    if provider_failures:
        lines.extend(["", "## Provider failure records", ""])
        for record in provider_failures:
            lines.append(
                f"- {record['condition']} run {record['run']}: "
                f"{record['provider_errors']}；不计入 Model Behavior。"
            )

    control_rate = control["aggregate"]
    treatment_rate = treatment["aggregate"]
    lines.extend(
        [
            "",
            "## 观察结论",
            "",
            f"- Control exact required test：{_format_rate(control_rate['exact_required_test'])}；Treatment：{_format_rate(treatment_rate['exact_required_test'])}。",
            f"- Control Completion Hint：{_format_rate(control_rate['completion_hint'])}；Treatment：{_format_rate(treatment_rate['completion_hint'])}。",
            f"- Control Final：{_format_rate(control_rate['final_answer'])}；Treatment：{_format_rate(treatment_rate['final_answer'])}。",
            f"- Control accepted：{_format_rate(control_rate['accepted'])}；Treatment：{_format_rate(treatment_rate['accepted'])}。",
            "- 这些是 n=3 的行为观察，不能单独证明因果关系。",
            "- required test 可见性改善收口的判断只在 exact test、Hint、Final 和 Acceptance 同时观察后成立；若 exact test 增加但仍无 Final，问题更接近 Completion Control。",
            "",
            "## 人话解释",
            "",
            "required test 是 Contract 的事实，因为 Verifier 会独立使用它重跑最终测试。此前模型看不到它，是因为 Phase 18 navigation harness 只提供通用 System Prompt 和用户任务，没有注入 Contract 指定命令。模型因此可能自行选择 `unittest discover` 或模块路径测试；exit code 0 只表示进程成功退出，不保证实际运行了测试。Completion Hint 只认 exact command，是为了把‘成功执行了 Contract 要求的验证’与普通命令区分开。Verifier 仍必须独立重跑，避免相信 Agent 自己的旧结果。visible test 只是把事实告诉模型，forced test 则会替模型执行或限制选择；Phase 19 只测前者。真实 Coding Agent 也需要从任务规格获得可执行的验证命令，但仍应保留 LLM 决策和独立验收。",
            "",
            "## 原始结果",
            "",
            "完整 Control/Treatment raw result 保存在 `eval/required_test_visibility_results.json`。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_phase19() -> dict:
    stability = json.loads(
        (EVAL_DIR / "navigation_stability_results.json").read_text(encoding="utf-8")
    )
    control = [
        normalise_run(item["raw_result"], item["run_index"], "control")
        for item in stability["phase18_baseline"] + stability["runs"]
        if item.get("scenario") == SCENARIO_NAME
    ]
    treatment = []

    def checkpoint(status: str) -> None:
        payload = _payload(control, treatment, status)
        (EVAL_DIR / "required_test_visibility_results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    checkpoint("running")
    for run_index in range(1, PLANNED_TREATMENT_RUNS + 1):
        with tempfile.TemporaryDirectory(prefix="required-test-visibility-medium-") as directory:
            root = Path(directory) / "workspace"
            spec = build_fixture(root, "MEDIUM")
            validate_fixture(root, spec)
            raw_result = _run_attempt(root)
        raw_result["provider_failure"] = is_provider_failure(raw_result)
        treatment.append(normalise_run(raw_result, run_index, "treatment"))
        checkpoint("running")

    payload = _payload(control, treatment, "complete")
    (EVAL_DIR / "required_test_visibility_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EVAL_DIR / "REQUIRED_TEST_VISIBILITY_REPORT.md").write_text(
        render_report(payload), encoding="utf-8"
    )
    return payload


def refresh_existing_results() -> dict:
    """Re-normalise saved raw runs without starting another Agent process."""
    current = json.loads(
        (EVAL_DIR / "required_test_visibility_results.json").read_text(encoding="utf-8")
    )
    control = [
        normalise_run(record["raw_result"], record["run"], "control")
        for record in current["control"]["runs"]
    ]
    treatment = [
        normalise_run(record["raw_result"], record["run"], "treatment")
        for record in current["treatment"]["runs"]
    ]
    payload = _payload(control, treatment, current.get("status", "complete"))
    (EVAL_DIR / "required_test_visibility_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EVAL_DIR / "REQUIRED_TEST_VISIBILITY_REPORT.md").write_text(
        render_report(payload), encoding="utf-8"
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.child:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            result = _run_child()
        finally:
            sys.stdout = real_stdout
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.refresh:
        payload = refresh_existing_results()
        print(json.dumps({"status": payload["status"], "refreshed": True}, ensure_ascii=False))
        return
    payload = run_phase19()
    print(json.dumps({"status": payload["status"], "treatment_runs": len(payload["treatment"]["runs"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
