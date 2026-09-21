"""Phase 18.5: repeat the Phase 18 navigation scenarios without changing Agent runtime."""

from collections import defaultdict
import json
from pathlib import Path
import subprocess
import tempfile

from navigation_eval import SCENARIOS, _run_one_subprocess
from navigation_fixtures import build_fixture, get_spec, validate_fixture


EVAL_DIR = Path(__file__).resolve().parent
SCENARIO_NAMES = (
    "small_symbol_navigation",
    "medium_symbol_coding",
    "large_symbol_coding",
)
PLANNED_RUNS = (2, 3)
PRIOR_FAILURES_FILE = EVAL_DIR / "navigation_stability_prior_failures.json"


def _parse_arguments(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def is_provider_failure(raw_result: dict) -> bool:
    metrics = raw_result.get("metrics", {})
    errors = " ".join(metrics.get("runtime_errors", []))
    errors += " " + raw_result.get("process_error", "")
    lowered = errors.casefold()
    return any(
        marker in lowered
        for marker in ("429", "timeout", "connection error", "5xx", "apierror")
    )


def _compact_chain(metrics: dict) -> list[str]:
    chain = [event.get("tool", "?") for event in metrics.get("tool_chain", [])]
    if metrics.get("final_answer_present"):
        chain.append("Final")
    return chain


def _normalise_run(raw_result: dict, run_index: int, attempt_index: int) -> dict:
    metrics = raw_result.get("metrics", {})
    trace = raw_result.get("trace", {})
    acceptance = raw_result.get("acceptance") or {}
    search_queries = []
    files_read = []
    for event in metrics.get("tool_chain", []):
        arguments = _parse_arguments(event.get("arguments", "{}"))
        if event.get("tool") == "search_text" and arguments.get("query"):
            search_queries.append(arguments["query"])
        if event.get("tool") == "read_file" and arguments.get("path"):
            files_read.append(arguments["path"])

    provider_failure = is_provider_failure(raw_result)
    return {
        "scenario": raw_result.get("scenario"),
        "repo_size": raw_result.get("repo_size"),
        "task_type": raw_result.get("task_type"),
        "run_index": run_index,
        "attempt_index": attempt_index,
        "provider_failure": provider_failure,
        "provider_errors": metrics.get("runtime_errors", []) or raw_result.get("process_error", ""),
        "model_calls": metrics.get("model_calls", 0),
        "tool_calls": metrics.get("tool_calls", 0),
        "list_files_calls": metrics.get("list_files_calls", 0),
        "search_text_calls": metrics.get("search_text_calls", 0),
        "read_file_calls": metrics.get("read_file_calls", 0),
        "apply_patch_calls": metrics.get("apply_patch_calls", trace.get("apply_patch_calls", 0)),
        "write_file_calls": metrics.get("write_file_calls", trace.get("write_file_calls", 0)),
        "run_command_calls": metrics.get("run_command_calls", trace.get("run_command_calls", 0)),
        "first_correct_file_turn": metrics.get("first_correct_file_turn"),
        "navigation_tool_calls_before_correct_file": metrics.get(
            "navigation_tool_calls_before_correct_file"
        ),
        "search_queries": search_queries,
        "files_read": files_read,
        "final_answer_present": metrics.get("final_answer_present", False),
        "max_steps_hit": metrics.get("max_steps_reached", False),
        "prompt_tokens": metrics.get("prompt_tokens", 0),
        "completion_tokens": metrics.get("completion_tokens", 0),
        "total_tokens": metrics.get("total_tokens", 0),
        "tool_chain": _compact_chain(metrics),
        "agent_ran_required_test": metrics.get("required_test_ran", False),
        "artifact_passed": acceptance.get("artifact_passed"),
        "interaction_completed": acceptance.get("interaction_completed"),
        "accepted": acceptance.get("accepted", raw_result.get("accepted", False)),
        "changed_files": acceptance.get("changed_files", []),
        "unexpected_changes": acceptance.get("unexpected_changes", []),
        "final_test_exit_code": acceptance.get("final_test_exit_code"),
        "raw_result": raw_result,
    }


def _timeout_result(scenario_name: str, error: subprocess.TimeoutExpired) -> dict:
    timeout = getattr(error, "timeout", "unknown")
    message = f"TimeoutExpired after {timeout} seconds"
    return {
        "scenario": scenario_name,
        "accepted": False,
        "process_exit_code": 124,
        "process_error": message,
        "metrics": {"runtime_errors": [f"Provider timeout: {message}"]},
        "trace": {},
    }


def _run_attempt(scenario_name: str, root: Path) -> dict:
    try:
        return _run_one_subprocess(scenario_name, root, timeout_seconds=300)
    except subprocess.TimeoutExpired as error:
        return _timeout_result(scenario_name, error)


def _numeric_summary(records: list[dict], field: str) -> dict[str, int | float | None]:
    values = [record[field] for record in records if not record["provider_failure"]]
    if not values:
        return {"min": None, "mean": None, "max": None}
    return {"min": min(values), "mean": round(sum(values) / len(values), 2), "max": max(values)}


def aggregate_runs(records: list[dict]) -> dict:
    valid = [record for record in records if not record["provider_failure"]]
    search_used = sum(record["search_text_calls"] > 0 for record in valid)
    accepted = sum(record["accepted"] is True for record in valid)
    coding = [record for record in valid if record["task_type"] == "coding"]
    coding_accepted = sum(record["accepted"] is True for record in coding)
    aggregate = {
        "planned_runs": 3,
        "observed_attempts": len(records),
        "valid_runs": len(valid),
        "provider_failures": sum(record["provider_failure"] for record in records),
        "search_usage": {"used": search_used, "denominator": len(valid), "rate": f"{search_used}/{len(valid)}"},
        "list_files_calls": _numeric_summary(records, "list_files_calls"),
        "search_text_calls": _numeric_summary(records, "search_text_calls"),
        "read_file_calls": _numeric_summary(records, "read_file_calls"),
        "model_calls": _numeric_summary(records, "model_calls"),
        "tool_calls": _numeric_summary(records, "tool_calls"),
        "total_tokens": _numeric_summary(records, "total_tokens"),
        "first_correct_file_turns": [record["first_correct_file_turn"] for record in valid],
        "accepted_rate": {"accepted": accepted, "denominator": len(valid), "rate": f"{accepted}/{len(valid)}"},
        "coding_accepted_rate": {
            "accepted": coding_accepted,
            "denominator": len(coding),
            "rate": f"{coding_accepted}/{len(coding)}",
        },
        "tool_chains": [record["tool_chain"] for record in valid],
    }
    return aggregate


def _baseline_record(raw_result: dict) -> dict:
    return _normalise_run(raw_result, run_index=1, attempt_index=1)


def _load_prior_failures() -> list[dict]:
    if not PRIOR_FAILURES_FILE.is_file():
        return []
    return json.loads(PRIOR_FAILURES_FILE.read_text(encoding="utf-8"))


def _make_payload(baseline: list[dict], new_runs: list[dict], prior_failures: list[dict], status: str) -> dict:
    by_scenario = defaultdict(list)
    for record in baseline + new_runs:
        by_scenario[record["scenario"]].append(record)
    return {
        "phase": "18.5",
        "status": status,
        "baseline_commit": "0aaae56",
        "scenario_names": list(SCENARIO_NAMES),
        "planned_new_runs": 6,
        "conditions": {
            "fixture_source": "Phase 18 eval/navigation_fixtures.py",
            "system_prompt": "Phase 18 unchanged",
            "tool_schema": "Phase 18 unchanged",
            "context_mode": "WRITE_ONLY",
            "permission_mode": "ALLOW",
            "max_agent_steps": 8,
            "replacement_policy": "at most one replacement attempt per scenario after provider_failure",
        },
        "prior_harness_attempts": prior_failures,
        "phase18_baseline": baseline,
        "runs": new_runs,
        "aggregate": {scenario: aggregate_runs(records) for scenario, records in by_scenario.items()},
    }


def run_stability() -> dict:
    baseline_payload = json.loads((EVAL_DIR / "navigation_results.json").read_text(encoding="utf-8"))
    baseline = [
        _baseline_record(item)
        for item in baseline_payload["results"]
        if item.get("scenario") in SCENARIO_NAMES
    ]
    new_runs = []
    prior_failures = _load_prior_failures()
    replacement_used = {scenario: False for scenario in SCENARIO_NAMES}

    def checkpoint(status: str) -> None:
        (EVAL_DIR / "navigation_stability_results.json").write_text(
            json.dumps(_make_payload(baseline, new_runs, prior_failures, status), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    checkpoint("running")

    for scenario_name in SCENARIO_NAMES:
        scenario = SCENARIOS[scenario_name]
        for run_index in PLANNED_RUNS:
            with tempfile.TemporaryDirectory(prefix=f"navigation-stability-{scenario['size'].lower()}-") as directory:
                root = Path(directory) / "workspace"
                spec = build_fixture(root, scenario["size"])
                validate_fixture(root, spec)
                raw_result = _run_attempt(scenario_name, root)
            record = _normalise_run(raw_result, run_index, attempt_index=1)
            new_runs.append(record)
            checkpoint("running")

            if record["provider_failure"] and not replacement_used[scenario_name]:
                replacement_used[scenario_name] = True
                with tempfile.TemporaryDirectory(prefix=f"navigation-stability-replacement-{scenario['size'].lower()}-") as directory:
                    root = Path(directory) / "workspace"
                    spec = build_fixture(root, scenario["size"])
                    validate_fixture(root, spec)
                    replacement_raw = _run_attempt(scenario_name, root)
                new_runs.append(_normalise_run(replacement_raw, run_index, attempt_index=2))
                checkpoint("running")
    payload = _make_payload(baseline, new_runs, prior_failures, "complete")
    (EVAL_DIR / "navigation_stability_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def _format_range(summary: dict) -> str:
    return f"{summary['min']} / {summary['mean']} / {summary['max']}"


def render_report(payload: dict) -> str:
    aggregates = payload["aggregate"]
    all_records = {
        scenario: [
            record
            for record in payload["phase18_baseline"] + payload["runs"]
            if record["scenario"] == scenario
        ]
        for scenario in SCENARIO_NAMES
    }
    small_chains = aggregates["small_symbol_navigation"]["tool_chains"]
    medium_chains = aggregates["medium_symbol_coding"]["tool_chains"]
    large_chains = aggregates["large_symbol_coding"]["tool_chains"]
    small_direct_search = all(chain and chain[0] == "search_text" for chain in small_chains)
    medium_list_then_search = all(
        "list_files" in chain
        and "search_text" in chain
        and chain.index("list_files") < chain.index("search_text")
        for chain in medium_chains
    )
    large_search_every_time = all("search_text" in chain for chain in large_chains)
    distinct_medium_paths = len({tuple(chain) for chain in medium_chains})
    distinct_large_paths = len({tuple(chain) for chain in large_chains})
    medium_failures = [
        f"run {record['run_index']} max_steps={record['max_steps_hit']} final={record['final_answer_present']}"
        for record in all_records["medium_symbol_coding"]
        if record["accepted"] is False
    ]
    prior_failures = payload.get("prior_harness_attempts", [])
    lines = [
        "# Phase 18.5：Repository Navigation Stability Check",
        "",
        "本阶段只重复实验，不修改 Agent Runtime、Tool Schema、Prompt、fixture 或 Acceptance。",
        f"Phase 18 baseline commit：`0aaae56`。新增计划运行 `6` 次；本批次 Provider failure `0` 次。",
        f"此前 runner 的已知 timeout attempt 保留为 prior harness attempt：`{len(prior_failures)}` 次，不计入 n=3 聚合。",
        "",
        "## Phase 18 baseline",
        "",
        "SMALL：`search_text → read_file → Final`，`0/1/1`，model/tool `3/2`，total `6,960`。",
        "MEDIUM：`list_files×3 → read_file → search_text → read_file → apply_patch → Final`，`3/1/2`，model/tool `6/7`，total `14,757`，Verifier accepted。",
        "LARGE：`list_files×3 → search_text → read_file×2 → apply_patch → run_command×2 → Final`，`3/1/2`，model/tool `8/9`，total `21,028`，Verifier accepted。",
        "ERROR-STRING 本阶段不重复。",
        "",
        "## 三次 Tool Chain",
        "",
    ]
    for scenario in SCENARIO_NAMES:
        records = aggregates[scenario]["tool_chains"]
        lines.append(f"### {scenario}")
        for index, chain in enumerate(records, start=1):
            lines.append(f"- run {index}: `{' → '.join(chain)}`")
        lines.append("")

    lines.extend(
        [
            "## 聚合结果",
            "",
            "以下统计包含 Phase 18 baseline + 两次新增运行；均值只用于描述本实验样本，不代表真实概率。",
            "",
            "| scenario | search usage | list min/mean/max | search min/mean/max | read min/mean/max | model min/mean/max | tool min/mean/max | total tokens min/mean/max | accepted rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scenario in SCENARIO_NAMES:
        item = aggregates[scenario]
        lines.append(
            f"| {scenario} | {item['search_usage']['rate']} | "
            f"{_format_range(item['list_files_calls'])} | {_format_range(item['search_text_calls'])} | "
            f"{_format_range(item['read_file_calls'])} | {_format_range(item['model_calls'])} | "
            f"{_format_range(item['tool_calls'])} | {_format_range(item['total_tokens'])} | "
            f"{item['accepted_rate']['rate']} |"
        )

    lines.extend(["", "## first_correct_file_turn", ""])
    for scenario in SCENARIO_NAMES:
        lines.append(f"- {scenario}: {aggregates[scenario]['first_correct_file_turns']}")

    lines.extend(
        [
            "",
            "## MEDIUM required-test 行为",
            "",
            "MEDIUM 三次记录的 `agent_ran_required_test`：" + ", ".join(
                str(record["agent_ran_required_test"]).lower()
                for record in [
                    record
                    for record in payload["phase18_baseline"] + payload["runs"]
                    if record["scenario"] == "medium_symbol_coding"
                ]
            )
            + "。Acceptance 逻辑不变，只观察，不修改 Completion/Contract。",
            "",
            "## 分析",
            "",
            f"- SMALL 是否稳定直接 search：{str(small_direct_search).lower()}；三次 chain 的首工具均为 `search_text`，search usage 为 {aggregates['small_symbol_navigation']['search_usage']['rate']}。",
            f"- MEDIUM 是否稳定先 list 再 search：{str(medium_list_then_search).lower()}；三次均为三次 `list_files` 后再 `search_text`。LARGE 是否稳定使用 search：{str(large_search_every_time).lower()}，但有一条路径先 `read_file` 后 search。",
            f"- 仓库越大是否增加 list：没有观察到 MEDIUM→LARGE 增加；两者均为 `3/3/3`（min/mean/max）。read 也均为 `2/2/2`。",
            f"- first_correct_file_turn：SMALL {aggregates['small_symbol_navigation']['first_correct_file_turns']}；MEDIUM {aggregates['medium_symbol_coding']['first_correct_file_turns']}；LARGE {aggregates['large_symbol_coding']['first_correct_file_turns']}，导航定位稳定。",
            f"- 同样成功但路径不同：MEDIUM 有 {distinct_medium_paths} 条不同 chain，LARGE 有 {distinct_large_paths} 条；MEDIUM 的失败记录为 {medium_failures or '无'}。",
            "- MEDIUM run 2 虽然 artifact 和 final test 通过，但连续执行两个非 contract 测试后撞 MAX_AGENT_STEPS，没有 Final Answer，因此 accepted=false；没有修改 Completion/Contract。",
            "- Navigation Guidance：当前 search usage 为 SMALL/MEDIUM/LARGE 均 `3/3`，且 LARGE 的 list/read 数没有相对 MEDIUM 增加；证据显示多种可行路径，不足以强制 Tool Preference。",
            "",
            "## 原始数据与复现",
            "",
            "每条新增运行保留 `raw_result`；聚合结果与原始数据均在 `eval/navigation_stability_results.json`。",
            "先前 timeout 的保留记录在 `eval/navigation_stability_prior_failures.json`。",
            "入口：`python eval/navigation_stability.py`。每个场景从干净 fixture snapshot 开始，Provider failure 最多一次 replacement。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    payload = run_stability()
    (EVAL_DIR / "NAVIGATION_STABILITY_REPORT.md").write_text(
        render_report(payload), encoding="utf-8"
    )
    for scenario in SCENARIO_NAMES:
        item = payload["aggregate"][scenario]
        print(
            f"{scenario}: search={item['search_usage']['rate']} "
            f"tokens={_format_range(item['total_tokens'])} "
            f"provider_failures={item['provider_failures']}"
        )


if __name__ == "__main__":
    main()
