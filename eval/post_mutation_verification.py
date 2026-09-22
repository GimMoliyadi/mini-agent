"""Phase 19.6: forensic analysis of verification freshness after mutation."""

import argparse
import json
from pathlib import Path

from .required_test_visibility import EVAL_DIR, EXACT_REQUIRED_TEST


RESULTS_PATH = EVAL_DIR / "post_mutation_verification_results.json"
REPORT_PATH = EVAL_DIR / "POST_MUTATION_VERIFICATION_REPORT.md"


def _is_exact_required_test(command):
    return (
        command.get("command") == EXACT_REQUIRED_TEST["command"]
        and tuple(command.get("args", [])) == EXACT_REQUIRED_TEST["args"]
        and command.get("cwd", ".") == EXACT_REQUIRED_TEST["cwd"]
    )


def _successful_mutation(event):
    return (
        event.get("action") == "tool_call"
        and event.get("tool") in {"apply_patch", "write_file"}
        and event.get("executed") is True
        and event.get("classification") == "PRODUCTIVE"
    )


def _rate(used, denominator):
    return {"used": used, "denominator": denominator, "rate": f"{used}/{denominator}"}


def analyse_record(record):
    events = record.get("raw_result", {}).get("trace", {}).get("events", [])
    mutations = [event for event in events if _successful_mutation(event)]
    mutation_turns = [event.get("turn") for event in mutations if event.get("turn") is not None]
    last_mutation_turn = max(mutation_turns) if mutation_turns else None

    exact_commands = [
        command
        for command in record.get("run_commands", [])
        if _is_exact_required_test(command)
    ]
    exact_commands.sort(key=lambda command: command.get("turn") or -1)
    exact_test_turns = [command.get("turn") for command in exact_commands]
    exact_test_exit_codes = [command.get("exit_code") for command in exact_commands]
    successful_exact = [
        command for command in exact_commands if command.get("exit_code") == "0"
    ]
    successful_turns = [command.get("turn") for command in successful_exact]
    last_required_test_turn = exact_test_turns[-1] if exact_test_turns else None
    last_successful_exact_test_turn = max(successful_turns) if successful_turns else None
    exact_test_before_last_mutation = bool(
        last_mutation_turn is not None
        and any(turn is not None and turn <= last_mutation_turn for turn in exact_test_turns)
    )
    test_evidence_expired = bool(
        last_mutation_turn is not None
        and last_successful_exact_test_turn is not None
        and last_successful_exact_test_turn <= last_mutation_turn
    )
    post_mutation_passed = bool(
        last_mutation_turn is not None
        and any(
            command.get("turn") is not None
            and command.get("turn") > last_mutation_turn
            and command.get("exit_code") == "0"
            for command in exact_commands
        )
    )

    return {
        "condition": record.get("condition", "treatment"),
        "run": record.get("run"),
        "tool_chain": record.get("tool_chain", []),
        "last_mutation_turn": last_mutation_turn,
        "mutation_tools": [event.get("tool") for event in mutations],
        "exact_test_turns": exact_test_turns,
        "exact_test_exit_codes": exact_test_exit_codes,
        "last_required_test_turn": last_required_test_turn,
        "last_successful_exact_test_turn": last_successful_exact_test_turn,
        "exact_test_before_last_mutation": exact_test_before_last_mutation,
        "test_evidence_expired": test_evidence_expired,
        "post_mutation_required_test_passed": post_mutation_passed,
        "agent_self_verified": post_mutation_passed,
        "final_answer_present": record.get("final_answer_present", False),
        "final_turn": record.get("final_answer_turn"),
        "completion_hint_triggered": record.get("completion_hint_triggered", False),
        "completion_hint_turn": record.get("completion_hint_turn"),
        "artifact_passed": record.get("artifact_passed"),
        "interaction_completed": record.get("interaction_completed"),
        "accepted": record.get("accepted", False),
    }


def aggregate_records(records):
    valid = [record for record in records if not record.get("provider_failure", False)]
    analysed = [analyse_record(record) for record in valid]
    denominator = len(analysed)
    return {
        "valid_runs": denominator,
        "final_answer": _rate(sum(item["final_answer_present"] for item in analysed), denominator),
        "accepted": _rate(sum(item["accepted"] for item in analysed), denominator),
        "exact_test_executed": _rate(sum(bool(item["exact_test_turns"]) for item in analysed), denominator),
        "exact_test_passed": _rate(
            sum(any(code == "0" for code in item["exact_test_exit_codes"]) for item in analysed),
            denominator,
        ),
        "post_mutation_exact_test_passed": _rate(
            sum(item["post_mutation_required_test_passed"] for item in analysed), denominator
        ),
        "agent_self_verified": _rate(sum(item["agent_self_verified"] for item in analysed), denominator),
        "completion_hint": _rate(sum(item["completion_hint_triggered"] for item in analysed), denominator),
        "artifact_passed": _rate(sum(bool(item["artifact_passed"]) for item in analysed), denominator),
        "interaction_completed": _rate(
            sum(bool(item["interaction_completed"]) for item in analysed), denominator
        ),
        "max_steps": _rate(
            sum(not bool(item["interaction_completed"]) for item in analysed), denominator
        ),
        "test_evidence_expired": sum(item["test_evidence_expired"] for item in analysed),
    }


def _load_valid_treatments():
    phase19 = json.loads(
        (EVAL_DIR / "required_test_visibility_results.json").read_text(encoding="utf-8")
    )
    recovery = json.loads(
        (EVAL_DIR / "required_test_visibility_recovery_results.json").read_text(
            encoding="utf-8"
        )
    )
    records = []
    records.extend(
        record
        for record in phase19["treatment"]["runs"]
        if not record.get("provider_failure", False)
    )
    records.extend(
        record
        for record in recovery["recovery_runs"]
        if not record.get("provider_failure", False)
    )
    return records


def build_payload():
    records = _load_valid_treatments()
    analysed = [analyse_record(record) for record in records]
    return {
        "phase": "19.6",
        "status": "complete",
        "sources": [
            "eval/required_test_visibility_results.json",
            "eval/required_test_visibility_recovery_results.json",
        ],
        "definitions": {
            "last_mutation_turn": "last successful apply_patch or write_file tool call",
            "last_required_test_turn": "last exact Contract required-test execution",
            "post_mutation_required_test_passed": "exact test exit 0 with test turn greater than last mutation turn",
            "agent_self_verified": "post_mutation_required_test_passed",
            "accepted_unchanged": True,
        },
        "runs": analysed,
        "aggregate": aggregate_records(records),
        "conclusions": {
            "run9_pre_mutation_test": True,
            "run9_agent_self_verified": False,
            "dominant_issue": "verification_freshness",
            "completion_hint_ablation_ready": False,
            "statistical_significance_claim": False,
        },
    }


def _chain(record):
    return " → ".join(record["tool_chain"])


def render_report(payload):
    aggregate = payload["aggregate"]
    lines = [
        "# Phase 19.6：Post-Mutation Verification Forensics",
        "",
        "本阶段只分析 Phase 19 的 2 个有效 Treatment 与 Phase 19.5R 的 3 个有效 Treatment。",
        "不修改 Runtime、Prompt、Tool Schema、Contract、Completion Hint、Verifier 或 MAX_AGENT_STEPS。",
        "",
        "## Run-level evidence",
        "",
        "| run | tool chain | last mutation | exact turns / exits | last successful exact | final | post-mutation pass | hint | artifact | interaction | self-verified | accepted |",
        "|---:|---|---:|---|---:|---|---|---|---|---|---|---|",
    ]
    for record in payload["runs"]:
        exits = ", ".join(
            f"{turn}/{code}"
            for turn, code in zip(record["exact_test_turns"], record["exact_test_exit_codes"])
        ) or "none"
        lines.append(
            f"| {record['run']} | `{_chain(record)}` | {record['last_mutation_turn']} | "
            f"{exits} | {record['last_successful_exact_test_turn']} | "
            f"{record['final_turn']} | {str(record['post_mutation_required_test_passed']).lower()} | "
            f"{str(record['completion_hint_triggered']).lower()} | "
            f"{str(record['artifact_passed']).lower()} | "
            f"{str(record['interaction_completed']).lower()} | "
            f"{str(record['agent_self_verified']).lower()} | "
            f"{str(record['accepted']).lower()} |"
        )

    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- Final：{aggregate['final_answer']['rate']}；accepted：{aggregate['accepted']['rate']}。",
            f"- exact test executed：{aggregate['exact_test_executed']['rate']}；exact test passed：{aggregate['exact_test_passed']['rate']}。",
            f"- post-mutation exact test passed：{aggregate['post_mutation_exact_test_passed']['rate']}；agent_self_verified：{aggregate['agent_self_verified']['rate']}。",
            f"- Completion Hint：{aggregate['completion_hint']['rate']}；artifact：{aggregate['artifact_passed']['rate']}；interaction：{aggregate['interaction_completed']['rate']}。",
            f"- stale PASS → later mutation evidence：{aggregate['test_evidence_expired']} runs。",
            "",
            "## Run 9",
            "",
            "Run 9 的 exact required test 发生在 Turn 4，exit code 为 1；最后一次 apply_patch 发生在 Turn 5。",
            "因此 Agent 在最终修改后没有自验证。最终 Verifier 仍确认 artifact 正确，Agent 也正常 Final 并被 accepted；",
            "这不等同于 Agent 自验证成功。",
            "",
            "## Interpretation",
            "",
            "Phase 19.6 保持 accepted 的既有定义：Verifier 仍是最终真实性来源，不要求 agent_self_verified=true 才 accepted。",
            "当前主要观察问题更接近 Verification Freshness，而不是 Completion：Run 9 有 Final 且 accepted，但没有最后修改后的成功 exact test。",
            "‘required test visible + 最终修改后重新验证’值得作为后续受控实验候选；本阶段不实现 Guidance 或 ablation。",
            "",
            "## Raw source",
            "",
            "结果保存在 `eval/post_mutation_verification_results.json`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    payload = build_payload()
    RESULTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_PATH.write_text(render_report(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "runs": len(payload["runs"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
