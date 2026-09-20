"""Run the bounded 3-task x 3-mode Context Management comparison."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
import run_eval  # noqa: E402


MODES = ("OFF", "WRITE_ONLY", "FULL")
TASK_IDS = ("task_1", "task_2", "task_3")
OUTPUT_JSON = EVAL_DIR / "context_ab_results.json"
OUTPUT_MARKDOWN = EVAL_DIR / "CONTEXT_AB.md"


def _task_map(tasks_path: Path) -> dict[str, dict]:
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    return {task["task_id"]: task for task in tasks}


def _run_task(task_id: str, tasks_path: Path, workspace: Path, mode: str) -> dict:
    previous = os.environ.get("CONTEXT_MODE")
    os.environ["CONTEXT_MODE"] = mode
    try:
        return run_eval.run_one(task_id, tasks_path, workspace)
    finally:
        if previous is None:
            os.environ.pop("CONTEXT_MODE", None)
        else:
            os.environ["CONTEXT_MODE"] = previous


def _record(mode: str, task: dict, outcome: dict) -> dict:
    result = outcome["result"]
    metrics = result
    category, reason = ("", "")
    if not result.get("success"):
        category, reason = run_eval.classify_failure(result)
    return {
        "mode": mode,
        "task_id": task["task_id"],
        "name": task.get("name", ""),
        "success": bool(metrics.get("success")),
        "final_answer_present": bool(metrics.get("final_answer_present")),
        "model_calls": metrics.get("model_calls", 0),
        "tools_used": metrics.get("tools_used", []),
        "tool_calls": metrics.get("tool_calls_requested", 0),
        "tool_calls_failed": metrics.get("tool_calls_failed", 0),
        "max_steps_hit": bool(metrics.get("max_steps_hit")),
        "prompt_tokens": metrics.get("prompt_tokens", 0),
        "completion_tokens": metrics.get("completion_tokens", 0),
        "total_tokens": metrics.get("total_tokens", 0),
        "provider_errors": sum(
            1 for error in metrics.get("runtime_errors", []) if error.startswith("Provider:")
        ),
        "runtime_errors": metrics.get("runtime_errors", []),
        "failure_category": category,
        "failure_reason": reason,
        "criteria_failed": metrics.get("criteria_failed", []),
        "final_answer": metrics.get("final_answer"),
        "output_file_exists": metrics.get("output_file_exists", False),
    }


def _markdown(records: list[dict]) -> str:
    lines = [
        "# Context Management 真实 A/B",
        "",
        "每个任务在 OFF / WRITE_ONLY / FULL 各运行一次；共 9 次任务。",
        "三种模式使用相同模型、System Prompt、Tool Schema、任务文本和 workspace snapshot。",
        "",
        "| task | mode | success | Final Answer | model calls | tool calls | max steps | prompt | completion | total | Provider errors |",
        "|---|---|---|---|---:|---:|---|---:|---:|---:|---:|",
    ]
    for item in records:
        lines.append(
            f'| {item["task_id"]} | {item["mode"]} | {item["success"]} '
            f'| {item["final_answer_present"]} | {item["model_calls"]} '
            f'| {item["tool_calls"]} | {item["max_steps_hit"]} '
            f'| {item["prompt_tokens"]:,} | {item["completion_tokens"]:,} '
            f'| {item["total_tokens"]:,} | {item["provider_errors"]} |'
        )
    lines.extend(
        [
            "",
            "A = task_1 摘要写入；B = task_2 双文件比较；C = task_3 列目录。",
            "一次运行不能证明统计显著性；结果只用于观察这三个模式在同一组任务上的行为和成本差异。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    tasks_path = EVAL_DIR / "tasks.json"
    task_map = _task_map(tasks_path)
    missing = [task_id for task_id in TASK_IDS if task_id not in task_map]
    if missing:
        raise SystemExit(f"任务文件缺少固定 A/B/C 任务：{missing}")

    records = []
    with tempfile.TemporaryDirectory(prefix="context-ab-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        shutil.copytree(config.WORKSPACE_DIR, snapshot)
        for mode in MODES:
            for task_id in TASK_IDS:
                isolated = Path(temporary) / f"{mode.lower()}-{task_id}"
                shutil.copytree(snapshot, isolated)
                print(f"[Context A/B] mode={mode} task={task_id}", flush=True)
                outcome = _run_task(task_id, tasks_path, isolated, mode)
                records.append(_record(mode, task_map[task_id], outcome))

    payload = {
        "model": config.load_config().model,
        "modes": list(MODES),
        "tasks": list(TASK_IDS),
        "runs": 9,
        "results": records,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MARKDOWN.write_text(_markdown(records), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
