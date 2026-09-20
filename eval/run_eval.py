"""Phase 6.5：Eval 总控。

用法：
    .venv\Scripts\python.exe eval\run_eval.py eval\tasks.json

每题使用源工作目录的独立临时副本，通过 AGENT_WORKSPACE 传给子进程。
每完成一题保存运行记录及输出内容，源目录不修改。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))  # eval/ 不是包，得能看到项目根模块

import config  # noqa: E402

RESULTS_FILE = EVAL_DIR / "results.json"
RUN_TASK_SCRIPT = EVAL_DIR / "run_task.py"
PYTHON_BIN = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

def classify_failure(result: dict) -> tuple[str, str]:
    """把失败归到六类之一。只分类，不改代码——用户要求先分清再动手。

    判据按优先级排，每条规则都写清楚「为什么排在这个位置」：

    1. Runtime —— 只有执行器自己崩了（没吐合法 JSON）才算。这是唯一的「真代码 Bug」路径。
    2. Provider —— 必须紧跟着摘出来。限流/断网会让任务看起来像 Agent 不稳定，
       不先排除，整轮 Eval 的稳定性结论都会被网络抖动污染。
    3. max_steps_hit —— 归 Model Behavior，**不归 Runtime**。
       Phase 5.5 那次撞上限已经确认归因是「模型没收敛」，不是代码抛异常；
       这里如果归成 Runtime，会跟 Phase 5.5 的结论自相矛盾。
    4. Tool Design → Prompt → Model Behavior → Test Design，剩下的按可操作性排序。
    """
    failed = "".join(result.get("criteria_failed", []))

    if "子进程未返回合法 JSON" in failed:
        return "Runtime", "执行器崩溃或未返回结果（真代码 Bug）"
    if any(e.startswith("Configuration:") for e in result.get("runtime_errors", [])):
        return "Configuration", "客户端配置或依赖不可用"
    if result.get("runtime_errors"):
        return "Provider", "外部请求失败（限流/超时/网络），非 Agent 问题"
    if result.get("max_steps_hit"):
        return "Model Behavior", f"模型没收敛到最终回答（问满 {config.MAX_AGENT_STEPS} 轮）"
    if "工具链缺少必需步骤" in failed:
        return "Tool Design", "工具没有提供任务所需的能力"
    if "目录清单遗漏文件" in failed:
        return "Model Behavior", "模型未完整列出工作目录文件"
    if "不存在的文件" in failed:
        return "Model Behavior", "模型编造了不存在的文件"
    if "没有最终回答" in failed:
        return "Model Behavior", "模型没有给出最终回答"
    if "输出文件不存在" in failed or "输出文件是空的" in failed:
        return "Prompt", "模型没按任务要求写出文件"
    if "内容" in failed or "未提到" in failed:
        return "Prompt", "回答内容不符合任务要求"
    return "Test Design", "判定规则可能太严或写错了"


def run_one(task_id: str, tasks_path: Path, workspace_dir: Path | None = None) -> dict:
    """起一个子进程跑单个任务，把它的 JSON 结果收回来。"""
    command = [
        str(PYTHON_BIN),
        str(RUN_TASK_SCRIPT),
        str(tasks_path),
        task_id,
    ]
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "TOOL_APPROVAL_MODE": "ALLOW",
    }
    if workspace_dir is not None:
        environment["AGENT_WORKSPACE"] = str(workspace_dir)
    try:
        completed = subprocess.run(command, cwd=str(PROJECT_ROOT), capture_output=True,
                                   env=environment, timeout=900)
    except subprocess.TimeoutExpired:
        completed = subprocess.CompletedProcess(command, 124, b"", b"Task timeout after 900 seconds")

    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        # 子进程崩了或者打印了非 JSON 内容：把它原样记下来，别静默吞掉
        result = {
            "task_id": task_id,
            "success": False,
            "criteria_failed": ["子进程未返回合法 JSON 结果"],
            "runtime_errors": ["Harness: subprocess failed", stderr[:1000]],
            "model_calls": 0,
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "max_steps_hit": False,
            "duplicate_calls_blocked": 0,
            "final_answer_present": False,
            "tool_calls_executed": 0,
            "tool_calls_requested": 0,
            "tool_calls_failed": 0,
            "tool_call_dropped": 0,
            "tools_used": [],
            "tool_chain": [],
            "turn_prompt_tokens": [],
            "finish_reason": [],
            "output_file_expected": False,
            "output_file_exists": False,
            "output_file_path": None,
            "final_answer": None,
            "needs_manual_review": False,
            "task": "",
            "name": task_id,
            "notes": "",
        }

    return {"exit_code": completed.returncode, "stderr": stderr[:2000], "result": result}


def summarize(results: list[dict]) -> dict:
    total = len(results)
    successes = sum(1 for r in results if r["metrics"]["success"])
    calls = sum(r["metrics"]["model_calls"] for r in results)
    tokens = sum(r["metrics"]["total_tokens"] for r in results)
    prompt_tokens = sum(r["metrics"]["prompt_tokens"] for r in results)

    return {
        "total_tasks": total,
        "successes": successes,
        "failures": total - successes,
        "success_rate": round(successes / total, 3) if total else None,
        "final_answer_present_count": sum(
            1 for r in results if r["metrics"]["final_answer_present"]
        ),
        "max_steps_hit_count": sum(1 for r in results if r["metrics"]["max_steps_hit"]),
        "duplicate_calls_blocked_total": sum(
            r["metrics"]["duplicate_calls_blocked"] for r in results
        ),
        "total_model_calls": calls,
        "avg_model_calls": round(calls / total, 2) if total else None,
        "total_prompt_tokens": prompt_tokens,
        "total_total_tokens": tokens,
        "avg_total_tokens": round(tokens / total, 1) if total else None,
        "provider_failures": sum(
            1 for r in results if r.get("failure_category") == "Provider"
        ),
        "runtime_failures": sum(r.get("failure_category") == "Runtime" for r in results),
        "configuration_failures": sum(r.get("failure_category") == "Configuration" for r in results),
        "needs_manual_review_count": sum(
            1 for r in results if r["metrics"]["needs_manual_review"]
        ),
        "context_growth_observations": [
            {"task_id": r["task_id"], "turn_prompt_tokens": r["metrics"]["turn_prompt_tokens"]}
            for r in results
        ],
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python eval\\run_eval.py <tasks.json>")

    tasks_path = Path(sys.argv[1]).resolve()
    if not tasks_path.is_file():
        raise SystemExit(f"[失败] 任务文件不存在：{tasks_path}")

    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    if not tasks:
        raise SystemExit("[失败] 任务文件是空的")

    workspace_dir = config.WORKSPACE_DIR
    print(f"[Eval] 任务数：{len(tasks)}    模型：{config.load_config().model}")
    run_dir = EVAL_DIR / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir.mkdir(parents=True)

    results = []
    started_at = datetime.now().isoformat(timespec="seconds")
    for index, task in enumerate(tasks, 1):
        task_id = task["task_id"]
        print(f"[{index}/{len(tasks)}] 开始 task_id={task_id}（{task.get('name', '')}）")
        print("    请求模型中…", flush=True)
        with tempfile.TemporaryDirectory(prefix="agent-eval-") as temporary:
            isolated = Path(temporary) / "workspace"
            shutil.copytree(workspace_dir, isolated)
            outcome = run_one(task_id, tasks_path, isolated)
        result = outcome["result"]

        category, reason = ("", "")
        if not result["success"]:
            category, reason = classify_failure(result)

        print(f"    结果：success={result['success']}  模型调用 {result['model_calls']} 次"
              f"  总 token {result['total_tokens']}"
              f"  重复调用被拦 {result['duplicate_calls_blocked']}"
              f"  撞上限={result['max_steps_hit']}"
              f"  finish={result['finish_reason']}")
        if result["criteria_failed"]:
            for failure in result["criteria_failed"]:
                print(f"    - {failure}")
        if category:
            print(f"    分类：{category} —— {reason}")

        results.append(
            {
                "task_id": task_id,
                "name": result.get("name", task.get("name", "")),
                "task": result.get("task", task["task"]),
                "review_focus": task.get("review_focus", ""),
                "needs_manual_review": result["needs_manual_review"],
                "failure_category": category,
                "failure_reason": reason,
                "process_exit_code": outcome["exit_code"],
                "stderr": outcome["stderr"],
                "metrics": result,
            }
        )
        checkpoint = {
            "started_at": started_at, "model": config.load_config().model,
            "status": "running", "planned_tasks": len(tasks),
            "summary": summarize(results), "results": results,
            "workspace_final_state": "隔离运行，源工作目录未修改",
        }
        checkpoint_path = run_dir / "results.json"
        pending = checkpoint_path.with_suffix(".tmp")
        pending.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(checkpoint_path)

    final_state = "隔离运行，源工作目录未修改"

    payload = {
        "started_at": started_at,
        "model": config.load_config().model,
        "summary": summarize(results),
        "results": results,
        "workspace_final_state": final_state,
        "status": "complete",
        "planned_tasks": len(tasks),
    }
    (run_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    RESULTS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = payload["summary"]
    print()
    print(f"[Eval] 完成：{summary['successes']}/{summary['total_tasks']} 成功"
          f"（{summary['success_rate']:.0%}）")
    print(f"       平均模型调用 {summary['avg_model_calls']} 次/任务，"
          f"平均 total_tokens {summary['avg_total_tokens']}")
    print(f"       撞上限 {summary['max_steps_hit_count']} 次，"
          f"重复调用被拦合计 {summary['duplicate_calls_blocked_total']} 次，"
          f"Provider 失败 {summary['provider_failures']} 次")
    print(f"       结果已写入 {RESULTS_FILE}")
    print(f"       工作目录：{final_state}")
    if summary["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
