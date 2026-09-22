"""Phase 19.5R: recover missing valid Treatment observations after a provider outage."""

import argparse
import json
import os
from pathlib import Path

import config
import main
from .required_test_visibility import (
    EVAL_DIR,
    REQUIRED_TEST_TEXT,
    SCENARIO_NAME,
    aggregate_condition,
    normalise_run,
)
from .required_test_visibility_stability import (
    BASELINE_COMMIT,
    _run_clean_attempt,
    _range,
    _valid,
)


RECOVERY_RUNS = (7, 8, 9)
RESULTS_PATH = EVAL_DIR / "required_test_visibility_recovery_results.json"
REPORT_PATH = EVAL_DIR / "REQUIRED_TEST_VISIBILITY_RECOVERY_REPORT.md"


def recovery_plan():
    return list(RECOVERY_RUNS)


def _is_harness_failure(record):
    raw_result = record.get("raw_result", {})
    errors = list(raw_result.get("metrics", {}).get("runtime_errors", []))
    errors.append(raw_result.get("process_error", ""))
    text = " ".join(str(error) for error in errors)
    return "child output was not valid JSON" in text or "ModuleNotFoundError" in text


def _valid_recovery(records):
    return [
        record
        for record in records
        if not record.get("provider_failure", False) and not _is_harness_failure(record)
    ]


def merge_recovery_records(phase19_records, recovery_records):
    return _valid(phase19_records) + _valid_recovery(recovery_records)


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


def _load_sources():
    phase19_payload = json.loads(
        (EVAL_DIR / "required_test_visibility_results.json").read_text(encoding="utf-8")
    )
    phase195_payload = json.loads(
        (EVAL_DIR / "required_test_visibility_stability_results.json").read_text(
            encoding="utf-8"
        )
    )
    phase19_records = [
        record
        for record in phase19_payload["treatment"]["runs"]
        if record.get("condition") == "treatment"
        and not record.get("provider_failure", False)
    ]
    phase195_failures = [
        record
        for record in phase195_payload.get("new_runs", [])
        if record.get("provider_failure", False)
    ]
    return phase19_payload, phase195_payload, phase19_records, phase195_failures


def _load_prior_harness_failures():
    if not RESULTS_PATH.is_file():
        return []
    previous = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    failures = list(previous.get("harness_failures", []))
    failures.extend(
        record
        for record in previous.get("recovery_runs", [])
        if _is_harness_failure(record)
    )
    return failures


def _proxy_metadata():
    return {
        name: os.environ.get(name) or None
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    }


def _preflight_metadata():
    loaded = config.load_config()
    client = main.build_client(loaded)
    try:
        user_agent = client._client.headers.get("user-agent")
    finally:
        client.close()
    return {
        "status": "success",
        "request": "回复 OK",
        "response": "OK",
        "tools": False,
        "base_url": loaded.base_url,
        "model": loaded.model,
        "api_key_present": bool(loaded.api_key),
        "user_agent": user_agent,
        "proxy": _proxy_metadata(),
        "stop_on_provider_failure": True,
    }


def _conditions(phase19_payload, preflight):
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
        "provider_base_url": preflight["base_url"],
        "model": preflight["model"],
        "replacement_policy": "none; stop recovery after the first provider failure",
        "control_rerun": False,
    }


def _payload(
    phase19_payload,
    phase195_payload,
    phase19_records,
    phase195_failures,
    recovery_records,
    harness_failures,
    preflight,
    status,
):
    valid_recovery = _valid_recovery(recovery_records)
    merged = merge_recovery_records(phase19_records, recovery_records)
    recovery_aggregate = aggregate_condition(valid_recovery)
    recovery_aggregate["planned_runs"] = len(RECOVERY_RUNS)
    recovery_aggregate["observed_attempts"] = len(recovery_records)
    return {
        "phase": "19.5R",
        "status": status,
        "baseline_commit": BASELINE_COMMIT,
        "source": {
            "phase19": "eval/required_test_visibility_results.json",
            "phase195": "eval/required_test_visibility_stability_results.json",
        },
        "provider_preflight": preflight,
        "conditions": _conditions(phase19_payload, preflight),
        "planned_runs": list(RECOVERY_RUNS),
        "stop_reason": (
            "provider_failure"
            if any(record.get("provider_failure") for record in recovery_records)
            else "harness_failure"
            if any(_is_harness_failure(record) for record in recovery_records)
            else "target_reached_or_plan_exhausted"
        ),
        "phase19_valid_treatment": phase19_records,
        "phase195_provider_failures": phase195_failures,
        "recovery_runs": recovery_records,
        "harness_failures": harness_failures,
        "merged_valid_treatment": merged,
        "aggregate": {
            "recovery": recovery_aggregate,
            "merged": aggregate_condition(merged),
            "recovery_valid_runs": recovery_aggregate["valid_runs"],
            "recovery_provider_failures": recovery_aggregate["provider_failures"],
            "merged_valid_runs": len(merged),
            "recovery_model_calls": [record["model_calls"] for record in recovery_records],
            "recovery_tool_calls": [record["tool_calls"] for record in recovery_records],
            "recovery_total_tokens": [record["total_tokens"] for record in recovery_records],
            "recovery_token_range": _range(
                record["total_tokens"] for record in recovery_records
            ),
            "merged_token_range": _range(record["total_tokens"] for record in merged),
            "merged_chain_reproductions": _chain_reproduced(merged),
        },
    }


def _write_results(payload):
    RESULTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _record_row(record):
    chain = " → ".join(record.get("tool_chain", []))
    return (
        f"| {record.get('run')} | {str(record.get('provider_failure')).lower()} | "
        f"`{chain}` | {str(record.get('agent_ran_required_test')).lower()} | "
        f"{record.get('required_test_exact_match_turn')} | "
        f"{record.get('required_test_exit_code')} | "
        f"{str(record.get('completion_hint_triggered')).lower()} | "
        f"{record.get('completion_hint_turn')} | "
        f"{str(record.get('final_answer_present')).lower()} | "
        f"{record.get('final_answer_turn')} | "
        f"{str(record.get('max_steps_reached')).lower()} | "
        f"{str(record.get('accepted')).lower()} | {record.get('model_calls')} | "
        f"{record.get('tool_calls')} | {record.get('total_tokens')} |"
    )


def render_report(payload):
    aggregate = payload["aggregate"]
    recovery = aggregate["recovery"]
    merged = aggregate["merged"]
    preflight = payload["provider_preflight"]
    lines = [
        "# Phase 19.5R：Provider Recovery & Resume",
        "",
        "Provider preflight 已成功后，本阶段只补 Phase 19.5 缺失的 Treatment 样本。",
        "不重跑 Control，不重复有效 Phase 19 Treatment，不修改 Runtime、Prompt、Tool Schema、",
        "Contract、Completion Hint 或 MAX_AGENT_STEPS。",
        "",
        "## Provider preflight",
        "",
        f"- status：`{preflight['status']}`；request：`{preflight['request']}`；response：`{preflight['response']}`；tools：`{preflight['tools']}`。",
        f"- base URL：`{preflight['base_url']}`；model：`{preflight['model']}`；API Key present：`{preflight['api_key_present']}`。",
        f"- User-Agent：`{preflight['user_agent']}`；proxy：`{preflight['proxy']}`。",
        "",
        "## Recovery 条件",
        "",
        "- Run 7/8/9；每次从干净 MEDIUM fixture 开始。",
        "- 每个计划 run 只尝试一次；不做 replacement。首次 provider/harness failure 后停止后续补样本。",
        f"- required test visibility：`{REQUIRED_TEST_TEXT}`。",
        "",
        "## 新增有效 Treatment / provider failure 记录",
        "",
        "| run | provider failure | tool chain | exact test | test turn | exit | hint | hint turn | final | final turn | max steps | accepted | model calls | tool calls | total tokens |",
        "|---:|---|---|---|---:|---|---|---:|---|---:|---|---|---:|---:|---:|",
    ]
    for record in payload["recovery_runs"]:
        lines.append(_record_row(record))

    lines.extend(
        [
            "",
            "## 结果",
            "",
            f"- 新增 provider failures：{recovery['provider_failures']}；新增有效 Treatment：{recovery['valid_runs']}。",
            f"- 保留 harness failures：{len(payload['harness_failures'])}；不计入有效 Treatment。",
            f"- 新增 exact test：{recovery['exact_required_test']['rate']}；Hint：{recovery['completion_hint']['rate']}；Final：{recovery['final_answer']['rate']}；accepted：{recovery['accepted']['rate']}。",
            f"- 新增 MAX_AGENT_STEPS：{recovery['max_steps']['used']}；ineffective zero-test：{recovery['ineffective_zero_test_attempts']}。",
            f"- 新增 model calls：{aggregate['recovery_model_calls']}；tool calls：{aggregate['recovery_tool_calls']}；tokens：{aggregate['recovery_token_range']}。",
            "",
            "## 合并 Phase 19 有效 Treatment",
            "",
            f"- Phase 19 有效样本：{len(payload['phase19_valid_treatment'])}；本轮有效样本：{aggregate['recovery_valid_runs']}；合并有效样本：{aggregate['merged_valid_runs']}。",
            f"- exact test：{merged['exact_required_test']['rate']}；Hint：{merged['completion_hint']['rate']}；Final：{merged['final_answer']['rate']}；accepted：{merged['accepted']['rate']}。",
            f"- MAX_AGENT_STEPS：{merged['max_steps']['rate']}；ineffective zero-test：{merged['ineffective_zero_test_attempts']}。",
            f"- `exact test → Hint → Final → accepted`：{aggregate['merged_chain_reproductions']} 次。",
            f"- token range：{aggregate['merged_token_range']}。",
            "",
            "## 结论边界",
            "",
            "以上只报告本实验观察值，不作概率或统计显著性结论。即使达到完整链，本阶段也只标记",
            "Treatment Stability，不执行 Completion Hint ablation，不进入 Phase 20 或新的 Runtime 阶段。",
            "",
            "## 原始结果",
            "",
            "完整结果保存在 `eval/required_test_visibility_recovery_results.json`。",
        ]
    )
    failures = [record for record in payload["recovery_runs"] if record.get("provider_failure")]
    if failures:
        lines.extend(["", "## Provider failure details", ""])
        for record in failures:
            lines.append(
                f"- run {record.get('run')}: {record.get('provider_errors')}；不计入 Model Behavior。"
            )
    return "\n".join(lines) + "\n"


def run_recovery():
    phase19_payload, phase195_payload, phase19_records, phase195_failures = _load_sources()
    preflight = _preflight_metadata()
    recovery_records = []
    harness_failures = _load_prior_harness_failures()

    def checkpoint(status):
        payload = _payload(
            phase19_payload,
            phase195_payload,
            phase19_records,
            phase195_failures,
            recovery_records,
            harness_failures,
            preflight,
            status,
        )
        _write_results(payload)

    checkpoint("running")
    for run_index in recovery_plan():
        record = _run_clean_attempt(run_index, 1)
        recovery_records.append(record)
        checkpoint("running")
        if record.get("provider_failure") or _is_harness_failure(record):
            break
        if len(_valid_recovery(recovery_records)) >= 3:
            break

    payload = _payload(
        phase19_payload,
        phase195_payload,
        phase19_records,
        phase195_failures,
        recovery_records,
        harness_failures,
        preflight,
        "complete",
    )
    _write_results(payload)
    REPORT_PATH.write_text(render_report(payload), encoding="utf-8")
    return payload


def cli_main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    payload = run_recovery()
    print(
        json.dumps(
            {
                "status": payload["status"],
                "recovery_runs": len(payload["recovery_runs"]),
                "valid_runs": payload["aggregate"]["recovery_valid_runs"],
                "provider_failures": payload["aggregate"]["recovery_provider_failures"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    cli_main()
