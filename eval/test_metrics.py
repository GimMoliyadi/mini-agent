"""Phase 6.5：指标收集与 success 判定的单元测试。

不需要 API Key、不发任何网络请求——只用构造好的假数据验证「收集对不对、判定对不对」。
真实运行交给 run_task.py 去做，这里只保证「尺子本身是准的」。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(EVAL_DIR))

from config import MAX_AGENT_STEPS, WORKSPACE_DIR  # noqa: E402
import main  # noqa: E402
import run_eval  # noqa: E402
import run_task  # noqa: E402


def _reply(prompt_tokens=100, tool_calls=None):
    """造一条假的模型返回值。tool_calls 传 None 就是「这一轮不再要工具」。"""
    message = SimpleNamespace(
        tool_calls=None
        if tool_calls is None
        else [
            SimpleNamespace(id=item[0], function=SimpleNamespace(name=item[1], arguments=item[2]))
            for item in tool_calls
        ]
    )
    return main.ModelReply(
        message=message,
        finish_reason="tool_calls" if tool_calls else "stop",
        prompt_tokens=prompt_tokens,
        completion_tokens=20,
        total_tokens=prompt_tokens + 20,
    )


def _read_call(path, result="ok"):
    """一次 read_file 调用的完整描述：(id, 工具名, 参数 JSON 串, 工具返回内容)。

    第 4 项是工具结果——指标回放靠它判断这次调用算「成功」还是「失败」。
    """
    return ("call-1", "read_file", json.dumps({"path": path}, ensure_ascii=False), result)


def _read_call_failed(path):
    """模拟一次失败调用：结果带 main.TOOL_FAILURE_PREFIX 前缀。"""
    return _read_call(path, result=f"{main.TOOL_FAILURE_PREFIX} FileNotFoundError: {path}")


def _list_call(args="{}"):
    return ("call-2", "list_files", args)


def _history(tool_calls_arg):
    """搭一段最小合法历史：assistant 提了 N 次调用，紧跟 N 条 tool 结果。

    这正是 main.run_tool_round 写历史的方式，指标回放就是照着它反推的。
    """
    calls = tool_calls_arg  # [[(id, tool, args, result), ...], ...]
    messages = [{"role": "user", "content": "task"}]
    for index, batch in enumerate(calls):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": item[0],
                        "type": "function",
                        "function": {"name": item[1], "arguments": item[2]},
                    }
                    for item in batch
                ],
            }
        )
        for offset, item in enumerate(batch):
            content = item[3] if len(item) > 3 else f"ok {index}-{offset}"
            messages.append({"role": "tool", "tool_call_id": item[0], "content": content})
    return messages


def test_first_call_is_executed():
    messages = _history([[_read_call("a.md")]])
    metrics = run_task.evaluate_task_metrics([_reply(100, [_read_call("a.md")])], messages)
    assert metrics["tool_calls_executed"] == 1, metrics
    assert metrics["duplicate_calls_blocked"] == 0, metrics
    assert metrics["tool_calls_failed"] == 0, metrics
    assert metrics["tool_calls_requested"] == 1, metrics
    assert metrics["tools_used"] == ["read_file"], metrics
    print("OK  第一次调用被正确计为 executed")


def test_identical_call_is_blocked():
    same = _read_call("a.md")
    metrics = run_task.evaluate_task_metrics([_reply(100, [same])], _history([[same], [same]]))
    assert metrics["tool_calls_executed"] == 1, metrics
    assert metrics["duplicate_calls_blocked"] == 1, metrics
    statuses = [event["status"] for event in metrics["tool_chain"]]
    assert statuses == ["executed", "blocked"], metrics
    print("OK  完全相同的第二次调用被计为 blocked，不再执行")


def test_failed_call_is_not_locked():
    """失败的调用不能进检测表——否则模型换参数重试会被当成重复，永远过不去。"""
    failed = _read_call_failed("missing.md")
    metrics = run_task.evaluate_task_metrics([_reply(100, [failed])], _history([[failed]]))
    assert metrics["tool_calls_executed"] == 1, metrics
    assert metrics["tool_calls_failed"] == 1, metrics
    assert metrics["duplicate_calls_blocked"] == 0, metrics
    assert metrics["tool_chain"][0]["status"] == "failed", metrics

    again = _read_call("exists.md")
    second = run_task.evaluate_task_metrics([_reply(100, [again])], _history([[again]]))
    assert second["tool_calls_executed"] == 1, second
    assert second["tool_calls_failed"] == 0, second
    assert second["tool_chain"][0]["status"] == "executed", second
    print("OK  失败调用 status=failed 且不锁定（换参数重试允许）")


def test_failed_call_then_identical_retry_is_allowed():
    """失败后模型用完全相同的参数重试，也必须被允许（不能算重复）。"""
    same = _read_call("a.md", result=f"{main.TOOL_FAILURE_PREFIX} 第一次没找到")
    retry = _read_call("a.md", result="ok")
    metrics = run_task.evaluate_task_metrics(
        [_reply(100, [same]), _reply(150, [retry])],
        _history([[same], [retry]]),
    )
    assert metrics["tool_calls_executed"] == 2, metrics
    assert metrics["duplicate_calls_blocked"] == 0, metrics
    assert [e["status"] for e in metrics["tool_chain"]] == ["failed", "executed"], metrics
    print("OK  失败后同一调用重试：failed -> executed，未被当成重复拦下")


def test_denied_call_is_not_counted_as_executed():
    """A denied side effect is a tool result, but not a successful execution."""
    denied = _read_call("a.md", result=main.APPROVAL_DENIED_PREFIX + "\n未执行")
    metrics = run_task.evaluate_task_metrics([_reply(100, [denied])], _history([[denied]]))
    assert metrics["tool_calls_executed"] == 0, metrics
    assert metrics["tool_calls_denied"] == 1, metrics
    assert metrics["tool_call_dropped"] == 0, metrics
    assert metrics["tool_chain"][0]["status"] == "denied", metrics
    print("OK  用户拒绝的调用不计为 executed，并在链路中标为 denied")


def test_step_ceiling_not_hit_by_default():
    """没问满模型、或历史里有最终回答，都不算撞上限。"""
    calls = [[_read_call("a.md")]]
    metrics = run_task.evaluate_task_metrics([_reply(100, calls[0])], _history(calls))
    assert metrics["model_calls"] == 1, metrics
    assert metrics["max_steps_hit"] is False, metrics
    assert metrics["tool_call_dropped"] == 0, metrics
    print("OK  没问满时不误判撞上限，且无被丢弃的调用")


def test_max_steps_hit_detected():
    """问满 MAX_AGENT_STEPS 次、每次都在要工具、历史里收不了口 -> 撞上限。

    最后一轮模型提的调用不进历史（main 刻意丢弃以保持协议合法），
    所以 tool_call_dropped 应等于 1——这是唯一能看出「上限真的兜住了」的信号。
    """
    batch = [_read_call("a.md")]
    replies = [_reply(100, batch)] * MAX_AGENT_STEPS
    # 只有前 MAX_AGENT_STEPS - 1 轮的调用进了历史，最后一轮被丢弃
    history = _history([batch] * (MAX_AGENT_STEPS - 1))
    metrics = run_task.evaluate_task_metrics(replies, history)

    assert metrics["model_calls"] == MAX_AGENT_STEPS, metrics
    assert metrics["max_steps_hit"] is True, metrics
    assert metrics["final_answer_present"] is False, metrics
    assert metrics["tool_call_dropped"] == 1, metrics
    assert sum(metrics["turn_prompt_tokens"]) == metrics["prompt_tokens"], metrics
    print("OK  问满上限且没收口 -> max_steps_hit=True，最后一轮调用被计为 dropped")


def test_tools_used_ordering_and_uniqueness():
    batch = [_read_call("a.md"), _list_call()]
    metrics = run_task.evaluate_task_metrics([_reply(100, batch)], _history([batch]))
    assert metrics["tools_used"] == ["list_files", "read_file"], metrics  # sorted 且去重
    print("OK  tools_used 去重并按字母序输出")


def test_tool_chain_args_truncated():
    long_args = json.dumps({"path": "a.md", "content": "x" * 400}, ensure_ascii=False)
    call = ("call-1", "write_file", long_args, "已写入")
    metrics = run_task.evaluate_task_metrics([_reply(100, [call])], _history([[call]]))
    args = metrics["tool_chain"][0]["args"]
    assert len(args) == run_task.TOOL_CHAIN_ARGS_LIMIT + 1, metrics  # 240 字符 + 省略号
    assert args.endswith("…"), args
    print("OK  超长参数被截断并标记省略号")


def test_judge_success_all_pass():
    metrics = {
        "final_answer_present": True,
        "final_answer": "已完成，共同点是都讲基础。",
        "max_steps_hit": False,
        "runtime_errors": [],
        "tool_chain": [
            {"tool": "read_file", "args": '{"path": "agent_notes.md"}', "status": "executed", "result_chars": 1},
            {"tool": "read_file", "args": '{"path": "python_notes.md"}', "status": "executed", "result_chars": 1},
        ],
    }
    task = {
        "expected_output_file": None,
        "success": {
            "require_final_answer": True,
            "require_within_max_steps": True,
            "require_no_runtime_error": True,
            "require_tool_chain": True,
            "tool_chain_must_include": ["read_file[agent_notes.md]", "read_file[python_notes.md]"],
            "output_content_checks": [{"contains": "共同", "scope": "final_answer"}],
        },
    }
    failures = run_task.judge_success(task, metrics, WORKSPACE_DIR)
    assert failures == [], failures
    print("OK  全部规则满足 -> 空列表（成功）")


def test_judge_success_missing_output_file():
    metrics = {
        "final_answer_present": True,
        "final_answer": "已写入。",
        "max_steps_hit": False,
        "runtime_errors": [],
        "tool_chain": [],
    }
    task = {"expected_output_file": "notes/definitely_not_written.md", "success": {"require_output_file": True}}
    failures = run_task.judge_success(task, metrics, WORKSPACE_DIR)
    assert any("不存在" in f for f in failures), failures
    print("OK  要求写文件但文件不存在 -> 判定失败")


def test_judge_success_runtime_error():
    metrics = {
        "final_answer_present": True,
        "final_answer": "",
        "max_steps_hit": False,
        "runtime_errors": ["Provider: APIError: rate limit"],
        "tool_chain": [],
    }
    failures = run_task.judge_success(
        {"expected_output_file": None, "success": {"require_no_runtime_error": True}},
        metrics,
        WORKSPACE_DIR,
    )
    assert failures == ["运行时报错：Provider: APIError: rate limit"], failures
    print("OK  有运行时报错 -> 判定失败并带上原因")


def test_judge_success_content_missing():
    metrics = {
        "final_answer_present": True,
        "final_answer": "文件不存在，无法读取。",
        "max_steps_hit": False,
        "runtime_errors": [],
        "tool_chain": [],
    }
    failures = run_task.judge_success(
        {
            "expected_output_file": None,
            "success": {"output_content_checks": [{"contains_any": ["共同", "差异"], "scope": "final_answer"}]},
        },
        metrics,
        WORKSPACE_DIR,
    )
    assert failures, "含任一项检查没提到任何一项时应失败"
    print("OK  final_answer 未提到任一关键词 -> 判定失败")


def test_judge_success_hallucinated_files():
    metrics = {
        "final_answer_present": True,
        "final_answer": "工作目录里有 todo.txt、notes/agent_summary.md 和 fabricated.md。",
        "max_steps_hit": False,
        "runtime_errors": [],
        "tool_chain": [],
    }
    failures = run_task.judge_success(
        {"expected_output_file": None, "success": {"require_no_hallucinated_files": True}},
        metrics,
        WORKSPACE_DIR,
    )
    assert any("不存在的文件" in f and "fabricated.md" in f for f in failures), failures
    print("OK  回答里提到不存在的文件 -> 判定失败")


def test_hallucination_check_ignores_bare_numbers():
    """'Python 3.11' 里的 3.11 不能被当成不存在的文件名。"""
    metrics = {
        "final_answer_present": True,
        "final_answer": "这是 Python 3.11 和版本 1.2.3 的说明。",
        "max_steps_hit": False,
        "runtime_errors": [],
        "tool_chain": [],
    }
    failures = run_task.judge_success(
        {"expected_output_file": None, "success": {"require_no_hallucinated_files": True}},
        metrics,
        WORKSPACE_DIR,
    )
    assert failures == [], failures
    print("OK  纯数字小数（如 3.11）不会被误判为文件名")


def test_normalize_ignores_whitespace():
    assert run_task._normalize("a b\t\nc") == run_task._normalize("abc"), "去空白归一失败"
    assert run_task._normalize("  ") == "", "全空白应归一为空串"
    print("OK  去空白归一对空白字符不敏感")


def test_final_answer_from_history():
    messages = _history([[_read_call("a.md")]])
    messages.append({"role": "assistant", "content": "最终回答"})
    assert run_task.get_final_answer(messages) == "最终回答"
    assert run_task.get_final_answer(_history([[_read_call("a.md")]])) is None
    print("OK  最终回答取最后一条不带 tool_calls 的 assistant 消息")


def test_chain_spec_both_forms():
    chain = [{"tool": "list_files", "args": "{}", "status": "executed", "result_chars": 1}]
    assert run_task._chain_satisfies("list_files", chain) is True
    assert run_task._chain_satisfies("read_file[agent_notes.md]", chain) is False
    assert run_task._chain_satisfies("write_file", chain) is False
    print("OK  工具链检查支持「只认工具名」和「工具名+参数包含」两种写法")


def test_classify_failure_taxonomy():
    """分类器把八种失败各归到正确的一类。

    重点验证两个容易搞错的地方：
      - 撞满 MAX_AGENT_STEPS 必须归 Model Behavior 而不是 Runtime
        （问满了模型但没给出回答，是模型没收敛，不是代码抛异常）
      - 网络失败必须先于其它判断被摘出成 Provider
        （否则限流会被误算成 Agent 缺陷，污染整轮稳定性结论）
    """
    cases = [
        ({"criteria_failed": ["子进程未返回合法 JSON 结果"]}, "Runtime"),
        (
            {"criteria_failed": ["运行时报错：Provider: APIConnectionError"],
             "runtime_errors": ["Provider: APIConnectionError: Connection error."]},
            "Provider",
        ),
        ({"criteria_failed": ["撞到最大步骤数", "没有最终回答"], "max_steps_hit": True}, "Model Behavior"),
        ({"criteria_failed": ["工具链缺少必需步骤：list_files"]}, "Tool Design"),
        ({"criteria_failed": ["最终回答提到了不存在的文件：foo.md"]}, "Model Behavior"),
        ({"criteria_failed": ["要求的输出文件不存在：notes/x.md"]}, "Prompt"),
        ({"criteria_failed": ["final_answer 缺少内容：共同"]}, "Prompt"),
        ({"criteria_failed": ["未知情况"]}, "Test Design"),
    ]

    for payload, expected in cases:
        category, reason = run_eval.classify_failure(payload)
        assert category == expected, f"分类错误：{expected} -> {category}（{reason}） 输入={payload}"
        assert reason, "分类必须带一句人能看懂的原因"
    print("OK  八种失败各归一类，撞上限归 Model Behavior、网络失败归 Provider")


def test_classify_provider_beats_runtime_error_criterion():
    """网络中断会让 judge_success 报「运行时报错」，但分类必须是 Provider 不是 Runtime。"""
    category, _ = run_eval.classify_failure(
        {
            "criteria_failed": ["运行时报错：Provider: APIError: rate limit"],
            "runtime_errors": ["Provider: APIError: rate limit"],
        }
    )
    assert category == "Provider", category
    print("OK  带「运行时报错」字样的 Provider 失败不会被误分成 Runtime")


def run_checks() -> None:
    """跑完所有检查。刻意不叫 main——那个名字已被 import 进来的 main 模块占用。"""
    checks = [
        test_first_call_is_executed,
        test_identical_call_is_blocked,
        test_failed_call_is_not_locked,
        test_failed_call_then_identical_retry_is_allowed,
        test_denied_call_is_not_counted_as_executed,
        test_step_ceiling_not_hit_by_default,
        test_max_steps_hit_detected,
        test_tools_used_ordering_and_uniqueness,
        test_tool_chain_args_truncated,
        test_judge_success_all_pass,
        test_judge_success_missing_output_file,
        test_judge_success_runtime_error,
        test_judge_success_content_missing,
        test_judge_success_hallucinated_files,
        test_hallucination_check_ignores_bare_numbers,
        test_normalize_ignores_whitespace,
        test_final_answer_from_history,
        test_chain_spec_both_forms,
        test_classify_failure_taxonomy,
        test_classify_provider_beats_runtime_error_criterion,
    ]
    for check in checks:
        check()
    print(f"\n{len(checks)} 项全部通过。")


if __name__ == "__main__":
    run_checks()
