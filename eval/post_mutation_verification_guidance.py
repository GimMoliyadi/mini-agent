"""Phase 20: evaluate one post-mutation verification guidance condition."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import config
import main as runtime_main

from .navigation_eval import SCENARIOS
from .navigation_fixtures import build_fixture, coding_contract, get_spec, validate_fixture
from .navigation_metrics import calculate_navigation_metrics
from .navigation_stability import is_provider_failure
from .post_mutation_verification import analyse_record
from .required_test_visibility import (
    EVAL_DIR,
    EXACT_REQUIRED_TEST,
    REQUIRED_TEST_TEXT,
    SCENARIO_NAME,
    _message_run_commands,
    normalise_run,
)


GUIDANCE_TEXT = (
    "After your final code modification, run the required test command to verify the final workspace state before finishing."
)
PLANNED_TREATMENT_RUNS = (1, 2, 3)
RESULTS_PATH = EVAL_DIR / "post_mutation_verification_guidance_results.json"
REPORT_PATH = EVAL_DIR / "POST_MUTATION_VERIFICATION_GUIDANCE_REPORT.md"


def build_guided_treatment_system_prompt():
    from main import SYSTEM_PROMPT

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Required test command: {REQUIRED_TEST_TEXT}\n\n{GUIDANCE_TEXT}"
    )


def _parse_arguments(raw):
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _timeout_result(error):
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


def _run_child():
    import acceptance

    scenario = SCENARIOS[SCENARIO_NAME]
    root = Path(os.environ["AGENT_WORKSPACE"]).resolve()
    spec = get_spec(scenario["size"])
    validate_fixture(root, spec)
    contract_data = coding_contract(spec)
    messages = [
        {"role": "system", "content": build_guided_treatment_system_prompt()},
        {"role": "user", "content": scenario["task"]},
    ]
    model_replies = []
    trace = runtime_main.CodingTaskTrace()
    runtime_errors = []
    client = None
    before = acceptance.snapshot_workspace(root)
    real_ask = runtime_main.ask

    def recording_ask(llm_client, model_name, history):
        reply = real_ask(llm_client, model_name, history)
        model_replies.append(reply)
        return reply

    runtime_main.ask = recording_ask
    try:
        loaded = runtime_main.load_config()
        client = runtime_main.build_client(loaded)
        first_reply = runtime_main.ask(client, loaded.model, messages)
        runtime_main.log_reply(1, first_reply)
        required_test = (
            EXACT_REQUIRED_TEST["command"],
            EXACT_REQUIRED_TEST["args"],
            EXACT_REQUIRED_TEST["cwd"],
        )
        runtime_main.run_agent_loop(
            client,
            loaded.model,
            messages,
            first_reply,
            set(),
            runtime_main.always_allow,
            trace=trace,
            required_test=required_test,
        )
        model_name = loaded.model
    except BaseException as exc:
        runtime_errors.append(f"{type(exc).__name__}: {exc}")
        model_name = os.environ.get("OPENAI_MODEL", "unknown")
    finally:
        runtime_main.ask = real_ask
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
    observed_run_commands = _message_run_commands(messages, trace.events)
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
        "guidance_visible": True,
        "guidance_text": GUIDANCE_TEXT,
        "metrics": metrics,
        "trace": trace.summary(),
        "observed_run_commands": observed_run_commands,
        "acceptance": acceptance_result,
        "accepted": acceptance_result["accepted"],
        "process_exit_code": 0 if not runtime_errors else 1,
    }


def _run_attempt(root):
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
                str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "eval.post_mutation_verification_guidance",
                "--child",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
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


def _normalise_attempt(raw_result, run_index):
    raw_result["provider_failure"] = is_provider_failure(raw_result)
    record = normalise_run(raw_result, run_index, "treatment")
    record["experiment_condition"] = "treatment_guidance"
    record["guidance_text"] = GUIDANCE_TEXT
    record["forensics"] = analyse_record(record)
    return record


def _run_clean_treatment(run_index):
    with tempfile.TemporaryDirectory(prefix="post-mutation-guidance-medium-") as directory:
        root = Path(directory) / "workspace"
        spec = build_fixture(root, "MEDIUM")
        validate_fixture(root, spec)
        raw_result = _run_attempt(root)
    return _normalise_attempt(raw_result, run_index)


def _rate(used, denominator):
    return {"used": used, "denominator": denominator, "rate": f"{used}/{denominator}"}


def aggregate_guidance_records(records):
    valid = [record for record in records if not record.get("provider_failure", False)]
    forensics = [record["forensics"] for record in valid]
    denominator = len(forensics)
    return {
        "planned_runs": len(PLANNED_TREATMENT_RUNS),
        "observed_attempts": len(records),
        "valid_runs": denominator,
        "provider_failures": sum(record.get("provider_failure", False) for record in records),
        "artifact_passed": _rate(sum(bool(item["artifact_passed"]) for item in forensics), denominator),
        "interaction_completed": _rate(sum(bool(item["interaction_completed"]) for item in forensics), denominator),
        "accepted": _rate(sum(bool(item["accepted"]) for item in forensics), denominator),
        "exact_test_executed": _rate(sum(bool(item["exact_test_turns"]) for item in forensics), denominator),
        "exact_test_passed": _rate(
            sum(any(code == "0" for code in item["exact_test_exit_codes"]) for item in forensics),
            denominator,
        ),
        "post_mutation_exact_test_passed": _rate(
            sum(item["post_mutation_required_test_passed"] for item in forensics), denominator
        ),
        "agent_self_verified": _rate(sum(item["agent_self_verified"] for item in forensics), denominator),
        "completion_hint": _rate(sum(item["completion_hint_triggered"] for item in forensics), denominator),
        "final_answer": _rate(sum(item["final_answer_present"] for item in forensics), denominator),
        "max_steps": _rate(
            sum(not bool(item["interaction_completed"]) for item in forensics), denominator
        ),
        "model_calls": [record.get("model_calls", 0) for record in valid],
        "tool_calls": [record.get("tool_calls", 0) for record in valid],
        "total_tokens": [record.get("total_tokens", 0) for record in valid],
    }


def _load_control_records():
    phase19 = json.loads(
        (EVAL_DIR / "required_test_visibility_results.json").read_text(encoding="utf-8")
    )
    recovery = json.loads(
        (EVAL_DIR / "required_test_visibility_recovery_results.json").read_text(
            encoding="utf-8"
        )
    )
    records = [
        record
        for record in phase19["treatment"]["runs"]
        if not record.get("provider_failure", False)
    ]
    records.extend(
        record
        for record in recovery["recovery_runs"]
        if not record.get("provider_failure", False)
    )
    for record in records:
        record["forensics"] = analyse_record(record)
        record["experiment_condition"] = "control_existing_treatment"
    return records


def _has_fail_patch_final(record):
    forensics = record["forensics"]
    if not record.get("final_answer_present"):
        return False
    failed_test = any(code != "0" for code in forensics["exact_test_exit_codes"])
    return bool(failed_test and forensics["last_mutation_turn"] is not None)


def _payload(control, treatment, status):
    return {
        "phase": "20",
        "status": status,
        "control_source": [
            "eval/required_test_visibility_results.json",
            "eval/required_test_visibility_recovery_results.json",
        ],
        "conditions": {
            "fixture": "MEDIUM Phase 18 fixture",
            "required_test_visibility": REQUIRED_TEST_TEXT,
            "guidance": GUIDANCE_TEXT,
            "runtime_unchanged": True,
            "tool_schema_unchanged": True,
            "contract_unchanged": True,
            "completion_hint_unchanged": True,
            "max_agent_steps": 8,
            "context_mode": os.environ.get("CONTEXT_MODE", "WRITE_ONLY"),
            "permission_mode": "ALLOW",
            "control_rerun": False,
        },
        "control": {
            "condition": "control_existing_treatment",
            "runs": control,
            "aggregate": aggregate_guidance_records(control),
        },
        "treatment": {
            "condition": "treatment_guidance",
            "runs": treatment,
            "aggregate": aggregate_guidance_records(treatment),
        },
        "pattern_counts": {
            "control_test_fail_patch_final": sum(_has_fail_patch_final(record) for record in control),
            "treatment_test_fail_patch_final": sum(_has_fail_patch_final(record) for record in treatment),
        },
    }


def _write_payload(payload):
    RESULTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_rate(aggregate, key):
    return aggregate[key]["rate"]


def _render_report(payload):
    control = payload["control"]
    treatment = payload["treatment"]
    lines = [
        "# Phase 20：Post-Mutation Verification Guidance Experiment",
        "",
        "Control 直接复用 Phase 19 + Recovery 的 5 个有效 Treatment，未重新调用模型。Treatment 只新增一条",
        f"事实性行为要求：`{GUIDANCE_TEXT}`。required test visibility、Runtime、Tool Schema、Contract、Completion Hint、",
        "MAX_AGENT_STEPS、Context 和 Permission 保持不变。",
        "",
        "## 结果",
        "",
        "| condition | run | last mutation | post-mutation exact test | self verified | hint | final | accepted |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in (control, treatment):
        for record in group["runs"]:
            forensic = record["forensics"]
            lines.append(
                f"| {group['condition']} | {record.get('run')} | {forensic['last_mutation_turn']} | "
                f"{str(forensic['post_mutation_required_test_passed']).lower()} | "
                f"{str(forensic['agent_self_verified']).lower()} | "
                f"{str(forensic['completion_hint_triggered']).lower()} | "
                f"{str(forensic['final_answer_present']).lower()} | "
                f"{str(forensic['accepted']).lower()} |"
            )
    lines.extend(
        [
            "",
            "## Treatment aggregate",
            "",
            f"- valid runs：{treatment['aggregate']['valid_runs']}；provider failures：{treatment['aggregate']['provider_failures']}。",
            f"- agent_self_verified：{_render_rate(treatment['aggregate'], 'agent_self_verified')}；post-mutation exact test：{_render_rate(treatment['aggregate'], 'post_mutation_exact_test_passed')}。",
            f"- exact test passed：{_render_rate(treatment['aggregate'], 'exact_test_passed')}；Hint：{_render_rate(treatment['aggregate'], 'completion_hint')}；Final：{_render_rate(treatment['aggregate'], 'final_answer')}；accepted：{_render_rate(treatment['aggregate'], 'accepted')}。",
            f"- model calls：{treatment['aggregate']['model_calls']}；tool calls：{treatment['aggregate']['tool_calls']}；tokens：{treatment['aggregate']['total_tokens']}。",
            "",
            "## Pattern analysis",
            "",
            f"- Control test FAIL → patch → Final：{payload['pattern_counts']['control_test_fail_patch_final']}。",
            f"- Treatment test FAIL → patch → Final：{payload['pattern_counts']['treatment_test_fail_patch_final']}。",
            "- 核心观察指标是最终修改后的成功 required test 与 agent_self_verified，不改变 accepted 定义。",
            "- 结果只描述本实验观察，不作统计显著性或因果结论。",
            "",
            "## Conclusion",
            "",
            "本实验只检验 Verification Freshness Guidance；不关闭 Completion Hint、不实现 ablation、不修改 Runtime。",
            "是否正式纳入 Coding Task Guidance，需根据本轮与既有 5 个 Control 的对照观察决定。",
            "",
            "## Raw result",
            "",
            "完整结果保存在 `eval/post_mutation_verification_guidance_results.json`。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_phase20():
    control = _load_control_records()
    treatment = []
    _write_payload(_payload(control, treatment, "running"))
    for run_index in PLANNED_TREATMENT_RUNS:
        treatment.append(_run_clean_treatment(run_index))
        _write_payload(_payload(control, treatment, "running"))
    payload = _payload(control, treatment, "complete")
    _write_payload(payload)
    REPORT_PATH.write_text(_render_report(payload), encoding="utf-8")
    return payload


def cli_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
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
    payload = run_phase20()
    print(json.dumps({"status": payload["status"], "treatment_runs": len(payload["treatment"]["runs"])}, ensure_ascii=False))


if __name__ == "__main__":
    cli_main()
