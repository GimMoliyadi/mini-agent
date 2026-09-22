"""Phase 19.5: repeat the Phase 19 Treatment condition only."""

import argparse
import json
from pathlib import Path
import tempfile

from .navigation_fixtures import build_fixture, get_spec, validate_fixture
from .navigation_stability import is_provider_failure
from .required_test_visibility import (
    EVAL_DIR,
    EXACT_REQUIRED_TEST,
    REQUIRED_TEST_TEXT,
    SCENARIO_NAME,
    _run_attempt,
    aggregate_condition,
    normalise_run,
)


PLANNED_RUNS = (4, 5, 6)
BASELINE_COMMIT = "e6ed38a"
RESULTS_PATH = EVAL_DIR / "required_test_visibility_stability_results.json"
REPORT_PATH = EVAL_DIR / "REQUIRED_TEST_VISIBILITY_STABILITY_REPORT.md"


def planned_attempts(run_indices, failed_runs=None):
    """Describe the allowed first attempt and optional replacement attempt."""
    failed_runs = set(failed_runs or ())
    attempts = []
    for run_index in run_indices:
        attempts.append((run_index, 1))
        if run_index in failed_runs:
            attempts.append((run_index, 2))
    return attempts


def _valid(records):
    return [record for record in records if not record.get("provider_failure", False)]


def merge_valid_treatment(phase19_records, new_records):
    """Merge behavior-bearing records while retaining provider failures separately."""
    return _valid(phase19_records) + _valid(new_records)


def _chain_reproduced(records):
    return sum(
        bool(
            record.get("agent_ran_required_test")
            and record.get("completion_hint_triggered")
            and record.get("final_answer_present")
            and record.get("accepted")
        )
        for record in records
    )


def _range(values):
    values = list(values)
    if not values:
        return {"min": None, "max": None}
    return {"min": min(values), "max": max(values)}


def aggregate_stability(phase19_records, new_records):
    new_aggregate = aggregate_condition(new_records)
    new_aggregate["planned_runs"] = len(PLANNED_RUNS)
    new_aggregate["observed_attempts"] = len(new_records)
    merged_records = merge_valid_treatment(phase19_records, new_records)
    merged_aggregate = aggregate_condition(merged_records)
    return {
        "planned_new_runs": len(PLANNED_RUNS),
        "observed_new_attempts": len(new_records),
        "new_valid_runs": new_aggregate["valid_runs"],
        "new_provider_failures": new_aggregate["provider_failures"],
        "merged_valid_runs": len(merged_records),
        "new": new_aggregate,
        "merged": merged_aggregate,
        "new_model_calls": [record["model_calls"] for record in _valid(new_records)],
        "new_tool_calls": [record["tool_calls"] for record in _valid(new_records)],
        "new_total_tokens": [record["total_tokens"] for record in _valid(new_records)],
        "new_attempt_model_calls": [record["model_calls"] for record in new_records],
        "new_attempt_tool_calls": [record["tool_calls"] for record in new_records],
        "new_attempt_total_tokens": [record["total_tokens"] for record in new_records],
        "new_attempt_token_range": _range(
            record["total_tokens"] for record in new_records
        ),
        "new_token_range": _range(
            record["total_tokens"] for record in _valid(new_records)
        ),
        "merged_token_range": _range(
            record["total_tokens"] for record in merged_records
        ),
        "merged_chain_reproductions": _chain_reproduced(merged_records),
        "new_chain_reproductions": _chain_reproduced(_valid(new_records)),
    }


def _load_phase19_records():
    payload = json.loads(
        (EVAL_DIR / "required_test_visibility_results.json").read_text(encoding="utf-8")
    )
    treatment = [
        record
        for record in payload["treatment"]["runs"]
        if record.get("condition") == "treatment"
        and record.get("scenario", SCENARIO_NAME) == SCENARIO_NAME
    ]
    return payload, _valid(treatment), [record for record in treatment if record.get("provider_failure")]


def _run_clean_attempt(run_index, attempt_index):
    with tempfile.TemporaryDirectory(prefix="required-test-visibility-stability-medium-") as directory:
        root = Path(directory) / "workspace"
        spec = build_fixture(root, "MEDIUM")
        validate_fixture(root, spec)
        raw_result = _run_attempt(root)
    raw_result["attempt_index"] = attempt_index
    raw_result["provider_failure"] = is_provider_failure(raw_result)
    return normalise_run(raw_result, run_index, "treatment")


def _conditions(phase19_payload):
    original = phase19_payload.get("conditions", {})
    return {
        "fixture": original.get("fixture", "MEDIUM Phase 18 fixture"),
        "user_task": original.get("user_task"),
        "system_prompt": "Phase 19 Treatment unchanged",
        "visibility_text": REQUIRED_TEST_TEXT,
        "runtime_unchanged": True,
        "system_prompt_unchanged": True,
        "tool_schema_unchanged": True,
        "completion_hint_unchanged": True,
        "contract_unchanged": True,
        "max_agent_steps": original.get("max_agent_steps", 8),
        "context_mode": original.get("context_mode", "WRITE_ONLY"),
        "permission_mode": original.get("permission_mode", "ALLOW"),
        "provider_model": "Phase 19 inherited provider/model/configuration",
        "replacement_policy": "at most one replacement attempt per planned run",
        "control_rerun": False,
    }


def _payload(phase19_payload, phase19_valid, phase19_failures, new_runs, status):
    return {
        "phase": "19.5",
        "status": status,
        "baseline_commit": BASELINE_COMMIT,
        "source": "eval/required_test_visibility_results.json",
        "conditions": _conditions(phase19_payload),
        "planned_runs": list(PLANNED_RUNS),
        "phase19_valid_treatment": phase19_valid,
        "phase19_provider_failures": phase19_failures,
        "new_runs": new_runs,
        "merged_valid_treatment": merge_valid_treatment(phase19_valid, new_runs),
        "aggregate": aggregate_stability(phase19_valid, new_runs),
    }


def _write_results(payload):
    RESULTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _record_chain(record):
    return " → ".join(record.get("tool_chain", []))


def _bool(value):
    return str(bool(value)).lower()


def _record_row(record):
    return (
        f"| {record.get('run')} | {record.get('attempt_index')} | "
        f"{_bool(record.get('provider_failure'))} | `{_record_chain(record)}` | "
        f"{_bool(record.get('agent_ran_required_test'))} | "
        f"{record.get('required_test_exact_match_turn')} | "
        f"{record.get('required_test_exit_code')} | "
        f"{_bool(record.get('completion_hint_triggered'))} | "
        f"{record.get('completion_hint_turn')} | "
        f"{_bool(record.get('final_answer_present'))} | "
        f"{record.get('final_answer_turn')} | {_bool(record.get('accepted'))} | "
        f"{_bool(record.get('max_steps_reached'))} | "
        f"{record.get('model_calls')} | {record.get('tool_calls')} | "
        f"{record.get('total_tokens')} |"
    )


def render_report(payload):
    aggregate = payload["aggregate"]
    new = aggregate["new"]
    merged = aggregate["merged"]
    lines = [
        "# Phase 19.5：Required-Test Visibility Treatment Stability",
        "",
        "本阶段只重复 Phase 19 Treatment；不重新运行 Control，也不修改 Runtime、System Prompt、",
        "Tool Schema、Completion Hint、Contract、MAX_AGENT_STEPS、MEDIUM fixture 或实验 Guidance。",
        f"Treatment 继续只暴露事实：`Required test command: {REQUIRED_TEST_TEXT}`。",
        "",
        "## 实验边界",
        "",
        f"- 稳定节点：`{payload['baseline_commit']}`。",
        "- 新计划 run：4、5、6；每次从干净 MEDIUM fixture 开始。",
        "- 每个计划 run 最多一次 replacement；只对 provider failure 使用 replacement。",
        "- Control 直接复用 Phase 19，未重新调用模型。",
        "- provider failure 保留在结果中，但不进入 Model Behavior 分母；不作统计显著性结论。",
        "",
        "## 新增 Treatment 记录",
        "",
        "| run | attempt | provider failure | tool chain | exact test | test turn | exit | hint | hint turn | final | final turn | accepted | max steps | model calls | tool calls | total tokens |",
        "|---:|---:|---|---|---|---:|---|---|---:|---|---:|---|---|---:|---:|---:|",
    ]
    for record in payload["new_runs"]:
        lines.append(_record_row(record))

    lines.extend(
        [
            "",
            "## 新增聚合",
            "",
            f"- planned runs：{aggregate['planned_new_runs']}；observed attempts：{aggregate['observed_new_attempts']}。",
            f"- valid Treatment：{aggregate['new_valid_runs']}；provider failure：{aggregate['new_provider_failures']}。",
            f"- exact required test：{new['exact_required_test']['rate']}；Completion Hint：{new['completion_hint']['rate']}；Final：{new['final_answer']['rate']}；accepted：{new['accepted']['rate']}。",
            f"- MAX_AGENT_STEPS：{new['max_steps']['used']}；ineffective zero-test：{new['ineffective_zero_test_attempts']}。",
            f"- 有效样本 model calls：{aggregate['new_model_calls']}；tool calls：{aggregate['new_tool_calls']}；total tokens：{aggregate['new_token_range']}。",
            f"- 全部新增 attempts（含 provider failure）model calls：{aggregate['new_attempt_model_calls']}；tool calls：{aggregate['new_attempt_tool_calls']}；total tokens：{aggregate['new_attempt_token_range']}。",
            "",
            "## Phase 19 合并观察",
            "",
            f"- Phase 19 原有有效 Treatment：{len(payload['phase19_valid_treatment'])}；本轮新增有效 Treatment：{aggregate['new_valid_runs']}；合并有效样本：{aggregate['merged_valid_runs']}。",
            f"- 合并 exact required test：{merged['exact_required_test']['rate']}；Completion Hint：{merged['completion_hint']['rate']}；Final：{merged['final_answer']['rate']}；accepted：{merged['accepted']['rate']}。",
            f"- `exact test → Hint → Final → accepted` 复现：{aggregate['merged_chain_reproductions']} 次。",
            f"- 合并 MAX_AGENT_STEPS：{merged['max_steps']['used']}；合并 ineffective zero-test：{merged['ineffective_zero_test_attempts']}。",
            f"- 合并 token 范围：{aggregate['merged_token_range']}。",
            "",
            "## Provider failure",
            "",
        ]
    )
    failures = payload["phase19_provider_failures"] + [
        record for record in payload["new_runs"] if record.get("provider_failure")
    ]
    if failures:
        for record in failures:
            lines.append(
                f"- run {record.get('run')} attempt {record.get('attempt_index')}: "
                f"{record.get('provider_errors')}；不计入 Model Behavior。"
            )
    else:
        lines.append("- 无 provider failure。")

    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "本报告只描述有限的真实运行观察，不作统计显著性或因果结论。若合并样本持续复现完整链，",
            "Completion Hint ablation 可作为下一项实验候选；本阶段不实现 ablation，也不进入 Runtime 功能开发。",
            "",
            "## 原始结果",
            "",
            "完整结果保存在 `eval/required_test_visibility_stability_results.json`。",
        ]
    )
    return "\n".join(lines) + "\n"


def run_stability():
    phase19_payload, phase19_valid, phase19_failures = _load_phase19_records()
    new_runs = []

    def checkpoint(status):
        _write_results(
            _payload(
                phase19_payload,
                phase19_valid,
                phase19_failures,
                new_runs,
                status,
            )
        )

    checkpoint("running")
    for run_index in PLANNED_RUNS:
        record = _run_clean_attempt(run_index, 1)
        new_runs.append(record)
        checkpoint("running")
        if record.get("provider_failure"):
            replacement = _run_clean_attempt(run_index, 2)
            new_runs.append(replacement)
            checkpoint("running")

    payload = _payload(
        phase19_payload,
        phase19_valid,
        phase19_failures,
        new_runs,
        "complete",
    )
    _write_results(payload)
    REPORT_PATH.write_text(render_report(payload), encoding="utf-8")
    return payload


def refresh_existing_results():
    """Re-render saved evidence without starting another model process."""
    current = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    phase19_payload, phase19_valid, phase19_failures = _load_phase19_records()
    payload = _payload(
        phase19_payload,
        phase19_valid,
        phase19_failures,
        current.get("new_runs", []),
        current.get("status", "complete"),
    )
    _write_results(payload)
    REPORT_PATH.write_text(render_report(payload), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    payload = refresh_existing_results() if args.refresh else run_stability()
    print(
        json.dumps(
            {
                "status": payload["status"],
                "planned_runs": payload["planned_runs"],
                "observed_attempts": payload["aggregate"]["observed_new_attempts"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
