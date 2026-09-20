"""Phase 6.5：单个 Eval 任务的执行器。

由 run_eval.py 调起，用法：
    .venv\Scripts\python.exe eval\run_task.py <tasks.json> <task_id> > eval\results.json

本文件只做三件事：把任务喂给 Agent → 收集指标 → 按规则判断 success。
不替换 Agent 的行为逻辑，
`build_client` / `ask` / `run_agent_loop` 全部照原样调用，
Agent 在这里的表现和你在终端里手打完全一样。

每题使用独立进程，在模块加载前通过 AGENT_WORKSPACE 选择临时工作目录。
"""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from openai import APIError

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))  # tests/ 和 eval/ 都不是包，要能 import 项目根模块

from config import MAX_AGENT_STEPS, WORKSPACE_DIR, load_config  # noqa: E402
import main  # noqa: E402

# 工具链里参数最多留这么多字符：write_file 的 arguments 可能很长，
# 全塞进 results.json 会让结果文件没法读。
TOOL_CHAIN_ARGS_LIMIT = 240

# 从最终回答里找「像文件名」的串。扩展名刻意限定为纯字母，
# 否则 "Python 3.11" 里的 3.11 会被当成文件名，造成误判。
FILENAME_PATTERN = re.compile(r"[A-Za-z0-9_./\-]+\.[A-Za-z]{1,6}")


def evaluate_task_metrics(model_replies: list[main.ModelReply], messages: list[dict]) -> dict:
    """从「每轮模型的返回值」和「最终历史记录」里还原一个任务的完整指标。

    工具调用是照着历史**回放**出来的，不是现场数：main.run_agent_loop 把
    「模型提了调用」和「调用结果」严格成对写进历史（这是服务商不 400 的前提），
    而且重复调用的判定只依赖两个输入——工具指纹、已执行过的集合——
    所以走一遍历史就能复现当时每一次调用是「真执行」「被拦」还是「失败」。

    回放的顺序必须和 main 一致：先看指纹在不在集合里，再更新集合。
    失败调用不记入集合，否则模型换参数重试会被当成重复。
    """
    final_answer = get_final_answer(messages)
    executed: set[tuple[str, str]] = set()
    executed_count = blocked_count = failed_count = denied_count = 0
    tool_chain: list[dict] = []
    tools_used: set[str] = set()
    tool_results = [m for m in messages if m.get("role") == "tool"]
    result_index = 0

    for message in messages:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue

        for call in message["tool_calls"]:
            tools_used.add(call["function"]["name"])

            fingerprint = main.call_fingerprint(_fake_call(call["function"]))
            was_blocked = fingerprint in executed

            result_index += 1
            result = tool_results[result_index - 1].get("content", "") if result_index <= len(tool_results) else ""
            was_failed = result.startswith(main.TOOL_FAILURE_PREFIX)
            was_denied = result.startswith(main.APPROVAL_DENIED_PREFIX)

            if was_blocked:
                blocked_count += 1
                status = "blocked"
            elif was_denied:
                denied_count += 1
                status = "denied"
            else:
                executed_count += 1
                if was_failed:
                    failed_count += 1
                    executed.discard(fingerprint)  # 失败不进检测表，和 main 一致
                    status = "failed"
                else:
                    executed.add(fingerprint)
                    status = "executed"

            args = call["function"].get("arguments") or ""
            if len(args) > TOOL_CHAIN_ARGS_LIMIT:
                args = args[:TOOL_CHAIN_ARGS_LIMIT] + "…"
            tool_chain.append(
                {"tool": call["function"]["name"], "args": args, "status": status, "result_chars": len(result)}
            )

    model_calls = len(model_replies)
    requested_total = sum(_call_count(reply.message) for reply in model_replies)
    # 撞到上限的判据：问满了模型，而且历史里没有留下最终回答。
    # run_agent_loop 达到 MAX_AGENT_STEPS 时明确「不执行、不写历史」，
    # 所以撞上限的历史一定以工具结果结尾，而不是 assistant 最终回答。
    max_steps_hit = model_calls >= MAX_AGENT_STEPS and final_answer is None

    return {
        "model_calls": model_calls,
        "finish_reason": [main.format_field(reply.finish_reason) for reply in model_replies],
        "prompt_tokens": sum(reply.prompt_tokens or 0 for reply in model_replies),
        "completion_tokens": sum(reply.completion_tokens or 0 for reply in model_replies),
        "total_tokens": sum(reply.total_tokens or 0 for reply in model_replies),
        "tool_calls_requested": requested_total,
        "tool_calls_executed": executed_count,
        "tool_calls_failed": failed_count,
        "tool_calls_denied": denied_count,
        "duplicate_calls_blocked": blocked_count,
        # 模型提了但没进历史的调用数：只有撞上限时最后一次会大于 0
        "tool_call_dropped": requested_total - executed_count - blocked_count - denied_count,
        "max_steps_hit": max_steps_hit,
        "final_answer_present": final_answer is not None,
        # 每轮单独记 prompt_tokens：这是看上下文如何逐轮变长的证据
        "turn_prompt_tokens": [reply.prompt_tokens for reply in model_replies],
        "tools_used": sorted(tools_used),
        "tool_chain": tool_chain,
    }


def judge_success(task: dict, metrics: dict, workspace_dir: Path) -> list[str]:
    """按任务声明的规则判断算不算成功，返回失败原因列表（空列表 = 成功）。

    这里只用「机器能验证」的规则：文件存不存在、最终回答在不在、有没有撞步数上限、
    有没有运行时报错、文本含不含指定词。
    摘要写得好不好、比较说得对不对这类主观指标**不在这份清单里**，
    一律留给 needs_manual_review 人工看。

    内容检查都先做「去空白」归一：模型写标点或换行的习惯不该影响判定。
    """
    criteria = task.get("success", {})
    failures: list[str] = []

    if criteria.get("require_final_answer") and not metrics["final_answer_present"]:
        failures.append("没有最终回答")
    if criteria.get("require_within_max_steps") and metrics["max_steps_hit"]:
        failures.append(f"撞到最大步骤数 {MAX_AGENT_STEPS}")
    if criteria.get("require_no_runtime_error") and metrics.get("runtime_errors"):
        failures.append("运行时报错：" + "；".join(metrics["runtime_errors"]))

    expected_output = task.get("expected_output_file")
    output_path = workspace_dir / expected_output if expected_output else None
    final_answer = metrics.get("final_answer") or ""

    if criteria.get("require_output_file"):
        if output_path is None or not output_path.is_file():
            failures.append(f"要求的输出文件不存在：{expected_output}")
        elif output_path.stat().st_size == 0:
            failures.append("要求的输出文件是空的")

    if criteria.get("require_tool_chain"):
        missing = [
            spec for spec in criteria.get("tool_chain_must_include", []) if not _chain_satisfies(spec, metrics["tool_chain"])
        ]
        if missing:
            failures.append("工具链缺少必需步骤：" + ", ".join(missing))

    if criteria.get("require_no_hallucinated_files"):
        listed = {path.name for path in workspace_dir.rglob("*") if path.is_file()}
        bogus = sorted(
            token for token in FILENAME_PATTERN.findall(final_answer) if Path(token).name not in listed
        )
        if bogus:
            failures.append("最终回答提到了不存在的文件：" + ", ".join(bogus))

    for check in criteria.get("output_content_checks", []):
        scope = check.get("scope", "final_answer")
        raw = (output_path.read_text(encoding="utf-8") if output_path and output_path.is_file() else "") if scope == "output_file" else final_answer
        haystack = _normalize(raw)
        if "equals" in check and raw != check["equals"]:
            failures.append(f"{scope} 内容与要求不完全一致")

        target = check.get("contains")
        if target and _normalize(target) not in haystack:
            failures.append(f"{scope} 缺少内容：{target}")

        alternatives = check.get("contains_any")
        if alternatives and not any(_normalize(item) in haystack for item in alternatives):
            failures.append(f"{scope} 未提到任何一项：{alternatives}")

    if criteria.get("require_complete_file_list"):
        missing = sorted(p.relative_to(workspace_dir).as_posix() for p in workspace_dir.rglob("*")
                         if p.is_file() and p.name not in final_answer)
        if missing:
            failures.append("目录清单遗漏文件：" + ", ".join(missing))
    return failures


def get_final_answer(messages: list[dict]) -> str | None:
    """最终回答 = 历史里最后一条不带 tool_calls 的 assistant 消息；没有就是没收口。"""
    for message in reversed(messages):
        if message.get("role") == "assistant" and not message.get("tool_calls"):
            return message.get("content") or ""
    return None


def run_task(task: dict, workspace_dir: Path) -> dict:
    """跑一个任务，返回一份完整的结果记录。"""
    config = load_config()
    client = None
    model_replies: list[main.ModelReply] = []
    runtime_errors: list[str] = []
    messages: list[dict] = [{"role": "system", "content": main.SYSTEM_PROMPT}, {"role": "user", "content": task["task"]}]
    executed: set[tuple[str, str]] = set()

    # 用记录器包一层 ask：run_agent_loop 内部自己调的 ask 从外面拿不到，
    # 不包的话第 2 轮以后的 token 用量和 finish_reason 全部丢失。
    # 只记录、不改行为，用完立刻还原。
    real_ask = main.ask

    def recording_ask(llm_client, model_name, history):
        reply = real_ask(llm_client, model_name, history)
        model_replies.append(reply)
        return reply

    main.ask = recording_ask
    # main.log_reply 把 Turn 日志打到 stdout，而本文件最后要用 stdout 输出一份纯 JSON
    # 给 run_eval.py 解析。所以跑 Agent 期间把 stdout 换到 stderr，只留 JSON 在 stdout 上。
    # main.py 没有缓存 stdout 引用，print() 每次调用才去查，换掉是有效的。
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        client = main.build_client(config)
        first_reply = main.ask(client, config.model, messages)
        main.log_reply(1, first_reply)
        # Eval is non-interactive: choosing ALLOW here is explicit and prevents
        # an approval prompt from blocking a child process.
        main.run_agent_loop(
            client, config.model, messages, first_reply, executed, main.always_allow
        )
    except (APIError, ConnectionError, TimeoutError) as exc:
        # 跟外界通信的失败（限流、超时、网络）不算程序 bug：记下来，任务判失败，整轮 Eval 继续
        runtime_errors.append(f"Provider: {type(exc).__name__}: {exc}")
    except (ImportError, ValueError) as exc:
        runtime_errors.append(f"Configuration: {type(exc).__name__}: {exc}")
    finally:
        sys.stdout = real_stdout
        main.ask = real_ask
        if client is not None:
            client.close()

    metrics = evaluate_task_metrics(model_replies, messages)
    metrics["runtime_errors"] = runtime_errors
    metrics["final_answer"] = get_final_answer(messages)

    metrics["output_file_expected"] = bool(task.get("expected_output_file"))
    metrics["output_file_exists"] = bool(
        task.get("expected_output_file") and (workspace_dir / task["expected_output_file"]).is_file()
    )
    metrics["output_file_path"] = task.get("expected_output_file")
    metrics["output_content"] = ((workspace_dir / task["expected_output_file"]).read_text(encoding="utf-8")
                                 if metrics["output_file_exists"] else None)

    criteria_failed = judge_success(task, metrics, workspace_dir)
    metrics["success"] = not criteria_failed
    metrics["criteria_failed"] = criteria_failed
    metrics["needs_manual_review"] = bool(task.get("needs_manual_review"))
    metrics["task_id"] = task["task_id"]
    metrics["name"] = task.get("name", "")
    metrics["task"] = task["task"]
    metrics["notes"] = task.get("review_focus", "")

    return metrics


def _fake_call(function: dict) -> SimpleNamespace:
    """造出 call_fingerprint 需要的那两个字段（历史里存的是字典，不是 openai 对象）。"""
    return SimpleNamespace(
        function=SimpleNamespace(name=function["name"], arguments=function.get("arguments"))
    )


def _call_count(message) -> int:
    """模型这一轮提了几次工具调用；没提要工具就是 0（字段可能整个是 None）。"""
    calls = message.tool_calls
    return len(calls) if calls else 0



def _chain_satisfies(spec: str, tool_chain: list[dict]) -> bool:
    """工具链检查。

    spec 有两种写法：
      "list_files"                  只要出现过这个工具
      "read_file[agent_notes.md]"   还要求参数里含这个串
    """
    tool_name, _, rest = spec.partition("[")
    needle = rest[:-1] if rest else None

    return any(
        event["tool"] == tool_name and (needle is None or needle in event["args"])
        for event in tool_chain
    )


def _normalize(text: str) -> str:
    """去掉全部空白：中文标点和换行写法不同不该影响内容判定。"""
    return "".join(text.split())


def run_cli() -> None:
    """命令行入口。名字不能叫 main——那会和 import 进来的 main 模块撞名。"""
    if len(sys.argv) != 3:
        raise SystemExit("用法：python eval\\run_task.py <tasks.json> <task_id>")

    tasks = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    task = next((item for item in tasks if item["task_id"] == sys.argv[2]), None)
    if task is None:
        raise SystemExit(f"任务文件里没有 task_id={sys.argv[2]}")

    print(json.dumps(run_task(task, WORKSPACE_DIR), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run_cli()
