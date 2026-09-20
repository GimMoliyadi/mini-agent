"""重复调用检测 + Agent 循环的单元测试（Phase 6）。

这一组测的是「任务做完了 Agent 却不知道自己该停」这件事的防线上，
属于 Python 侧（Runtime）的那一层：重复调用检测。

三条必须同时成立：
  1. 同一工具 + 规范化后完全相同的参数 → 第二次不真正执行
  2. 拦下后仍然按协议回喂一条正常工具结果（assistant tool_calls + tool 结果成对）
  3. 不同的调用绝不能被误判成重复

另外要守住一条反面：失败过的调用不进检测表，
否则模型换个参数重试会被当成「重复」而永远过不去。

这里必须 import main，而 main 依赖 openai，所以要用 venv 里的 Python：
    .venv\Scripts\python.exe tests\test_loop.py
（tests\test_sandbox.py 只用标准库，系统 python 也能跑。）
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# tests/ 不是包，直接跑这个脚本时 sys.path[0] 是 tests/ 自己，
# 看不到项目根目录下的主模块。这里补进去，才能 import 到 main 和 tools。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import main  # noqa: E402
import tools  # noqa: E402


def check_fingerprint_equivalence() -> None:
    """语义相同的两次调用必须得到同一个指纹。

    协议规定 arguments 是 JSON 字符串，而「写法不同、含义相同」很常见：
    键序换了、多了空格、数字加了小数点。字符串直接比会漏掉它们——
    Phase 5.5 真模型实测就出现过：两次 write_file 内容逐字节相同，
    只是 "path" 和 "content" 的先后换了位置。
    """
    same_meaning = [
        '{"path": "a.md", "content": "v1"}',
        '{"content": "v1", "path": "a.md"}',
        '  { "path" : "a.md" , "content" : "v1" }  ',
        '{"path":"a.md","content":"v1"}',
    ]
    fingerprints = {main.call_fingerprint(_fake_call("write_file", args)) for args in same_meaning}
    assert len(fingerprints) == 1, f"同一个调用的指纹不唯一：{fingerprints}"
    print(f"OK  键序/空格不同的同一调用 → 同一个指纹（{len(same_meaning)} 种写法）")


def check_fingerprint_not_equal() -> None:
    """不同的调用绝不能被误判成重复。这些是必须放行的正常行为。"""
    pairs = [
        # read_file 读不同的文件
        ("read_file", '{"path": "a.md"}'),
        ("read_file", '{"path": "b.md"}'),
    ]
    # write_file 写同一个文件但内容不同 = 模型在改，不是重复
    pairs += [
        ("write_file", '{"path": "a.md", "content": "版本1"}'),
        ("write_file", '{"path": "a.md", "content": "版本2"}'),
    ]
    # list_files 看不同目录
    pairs += [
        ("list_files", '{"path": "."}'),
        ("list_files", '{"path": "notes"}'),
    ]
    # 参数完全相同但工具不同
    pairs += [
        ("read_file", '{"path": "a.md"}'),
        ("write_file", '{"path": "a.md"}'),
    ]

    for (n1, a1), (n2, a2) in zip(pairs[::2], pairs[1::2]):
        first = main.call_fingerprint(_fake_call(n1, a1))
        second = main.call_fingerprint(_fake_call(n2, a2))
        assert first != second, f"不同调用被误判成同一个：{n1}({a1}) vs {n2}({a2})"
        print(f"OK  未误判：{n1}({a1}) vs {n2}({a2})")

    # 参数不是合法 JSON：拿不准就放行，不能因为解析失败而崩，
    # 也不能把它和某个合法调用混成一类
    bad = main.call_fingerprint(_fake_call("read_file", "{path: not-json"))
    assert bad == ("read_file", "{path: not-json"), f"坏参数指纹不对：{bad}"
    assert bad != main.call_fingerprint(_fake_call("read_file", "{}"))
    print("OK  参数不是合法 JSON 时不抛异常，退回原始字符串（宁放行不拦错）")


def check_write_context_compaction() -> None:
    """Keep the two newest rounds full and compact only recoverable old payloads."""
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "inspect files"}]
    raw_arguments = []
    for index in range(3):
        read_arguments = f'{{"path": "note_{index}.md"}}'
        write_arguments = '{"path": "summary.md", "content": "' + ("x" * 200) + '"}'
        raw_arguments.append((read_arguments, write_arguments))
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"read_{index}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": read_arguments,
                            },
                        },
                        {
                            "id": f"write_{index}",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": write_arguments,
                            },
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read_{index}",
                    "content": f"read content {index} " + ("r" * 180),
                },
                {
                    "role": "tool",
                    "tool_call_id": f"write_{index}",
                    "content": f"write confirmation {index}",
                },
            ]
        )

    context = main.build_model_context(messages, mode="FULL")
    assistant_messages = [m for m in context if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_messages) == 3
    assert "historical read compacted" in context[2 + 1]["content"]
    assert "historical read compacted" not in context[-2]["content"]
    assert "previous write content omitted" in assistant_messages[0]["tool_calls"][1]["function"]["arguments"]
    assert assistant_messages[1]["tool_calls"][1]["function"]["arguments"] == raw_arguments[1][1]
    assert assistant_messages[2]["tool_calls"][1]["function"]["arguments"] == raw_arguments[2][1]
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == raw_arguments[0][0]
    assert _pairing_legal(context), "bounded model context must keep tool pairing legal"

    failed = [dict(message) for message in messages]
    failed[3] = dict(failed[3])
    failed[3]["content"] = f"{main.TOOL_FAILURE_PREFIX} PermissionError"
    failed_context = main.build_model_context(failed, mode="FULL")
    assert failed_context[3]["content"].startswith(main.TOOL_FAILURE_PREFIX)
    print(
        "OK  recent rounds stay full, old reads become references, "
        "and canonical/failed history stay intact"
    )


def check_all_context_modes() -> None:
    """All context modes preserve protocol pairing and their promised scope."""
    messages: list[dict] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "compare and save"},
    ]
    write_arguments = []
    read_contents = []
    for index in range(3):
        read_arguments = json.dumps({"path": f"note_{index}.md"})
        write_content = f"summary {index} " + ("x" * 200)
        write_argument = json.dumps(
            {"path": "summary.md", "content": write_content}, ensure_ascii=False
        )
        write_arguments.append(write_argument)
        read_contents.append(f"read content {index} " + ("r" * 180))
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"read_{index}",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": read_arguments},
                        },
                        {
                            "id": f"write_{index}",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": write_argument},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": f"read_{index}", "content": read_contents[-1]},
                {
                    "role": "tool",
                    "tool_call_id": f"write_{index}",
                    "content": f"已写入 summary.md（{len(write_content)} 字节）",
                },
            ]
        )

    original = json.dumps(messages, ensure_ascii=False, sort_keys=True)

    def rounds(context: list[dict]) -> list[tuple[dict, list[dict]]]:
        result = []
        for index, message in enumerate(context):
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if calls:
                result.append((message, context[index + 1 : index + 1 + len(calls)]))
        return result

    for mode in ("OFF", "WRITE_ONLY", "FULL"):
        context = main.build_model_context(messages, mode=mode)
        assert _pairing_legal(context), f"{mode} 破坏了 tool_call/tool result 配对"
        assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == original

        for assistant, results in rounds(context):
            for call in assistant["tool_calls"]:
                json.loads(call["function"]["arguments"])
            assert [result["tool_call_id"] for result in results] == [
                call["id"] for call in assistant["tool_calls"]
            ]

        context_rounds = rounds(context)
        write_args = [
            json.loads(call["function"]["arguments"])["content"]
            for assistant, _ in context_rounds
            for call in assistant["tool_calls"]
            if call["function"]["name"] == "write_file"
        ]
        read_results = [
            result["content"]
            for assistant, results in context_rounds
            for call, result in zip(assistant["tool_calls"], results)
            if call["function"]["name"] == "read_file"
        ]

        if mode == "OFF":
            assert write_args == [json.loads(item)["content"] for item in write_arguments]
            assert read_results == read_contents
        elif mode == "WRITE_ONLY":
            assert all("previous write content omitted" in item for item in write_args)
            assert read_results == read_contents
        else:
            assert "previous write content omitted" in write_args[0]
            assert write_args[1:] == [json.loads(item)["content"] for item in write_arguments[1:]]
            assert "historical read compacted" in read_results[0]
            assert read_results[1:] == read_contents[1:]

    print("OK  OFF / WRITE_ONLY / FULL 均保持协议合法且只执行各自的压缩范围")


def check_duplicate_interception() -> None:
    """第一次正常执行、第二次被拦下且不真正执行、历史协议始终合法。"""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        # 沙盒指向临时目录，测试不碰真实的 demo_workspace。
        # main 也绑定了 config 的 WORKSPACE_DIR，一并改掉，保持两边一致。
        tools.WORKSPACE_DIR = workspace
        main.WORKSPACE_DIR = workspace

        target = workspace / "repeat.txt"
        messages: list[dict] = []
        executed: set[tuple[str, str]] = set()

        arguments = '{"path": "repeat.txt", "content": "第一次写的内容"}'

        # 1) 第一次：正常执行，真的落盘
        run_once(messages, executed, "call_1", "write_file", arguments)
        first_result = messages[-1]["content"]
        assert not first_result.startswith(main.TOOL_FAILURE_PREFIX), first_result
        assert "新文件" in first_result, f"第一次应当是新文件：{first_result}"
        assert target.is_file() and target.read_text(encoding="utf-8") == "第一次写的内容"
        print("OK  第一次调用正常执行并落盘")

        # 2) 第二次：完全相同的工具 + 完全相同的参数 → 拦下
        run_once(messages, executed, "call_2", "write_file", arguments)
        second_result = messages[-1]["content"]
        assert second_result == main.DUPLICATE_NOTICE, f"第二次没被拦下：{second_result}"
        assert "拦截" in second_result
        print("OK  第二次相同调用被拦下，回喂的是重复提示")

        # 3) 拦下之后仍然可以正常收口：模型换个动作照样能执行
        run_once(messages, executed, "call_3", "list_files", "{}")
        assert "[d]" not in messages[-1]["content"] and "[f] repeat.txt" in messages[-1]["content"]
        print("OK  被拦下后 Agent 仍能正常执行其它工具")

        # 4) 历史必须是协议合法的：每条 assistant tool_calls 都有配对的 tool 结果
        assert _pairing_legal(messages), "工具调用和工具结果没有成对出现"
        print("OK  历史协议合法（成对 + 顺序正确）")


def check_failed_call_not_locked() -> None:
    """失败过的调用不进检测表：模型换参数重试必须允许。

    反例：如果失败也算「已成功执行过」，模型第一次读错文件名后
    改个路径再试一次，会被当成重复调用拦下来，永远过不去。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tools.WORKSPACE_DIR = Path(tmp)
        main.WORKSPACE_DIR = Path(tmp)

        messages: list[dict] = []
        executed: set[tuple[str, str]] = set()

        arguments = '{"path": "nope.txt"}'
        run_once(messages, executed, "call_1", "read_file", arguments)
        first = messages[-1]["content"]
        assert first.startswith(main.TOOL_FAILURE_PREFIX), f"应当报工具失败：{first}"
        assert len(executed) == 0, f"失败调用不应被记录：{executed}"

        run_once(messages, executed, "call_2", "read_file", arguments)
        second = messages[-1]["content"]
        assert second.startswith(main.TOOL_FAILURE_PREFIX), f"失败不该被锁定成重复：{second}"
        print("OK  失败的调用不进检测表，重试不被误判为重复")


def run_once(
    messages: list[dict],
    executed: set[tuple[str, str]],
    call_id: str,
    tool_name: str,
    arguments: str,
) -> None:
    """用一条假的工具调用消息跑一轮，等价于模型提了一次工具调用。"""
    message = _fake_message(call_id, tool_name, arguments)
    main.run_tool_round(messages, message, executed)


def _fake_message(call_id: str, tool_name: str, arguments: str):
    """构造一个形状和真实回复一致的假消息，用来驱动 run_tool_round。

    只挑 run_tool_round 真正读到的字段：content / tool_calls / id / function.name /
    function.arguments。不引入 openai 的类型对象，因为那些类型没法凭空构造。
    """
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=tool_name, arguments=arguments),
    )
    # 提工具调用时模型没说话，content 就是 None——和真实回复一致。
    return SimpleNamespace(content=None, tool_calls=[call])


def _fake_call(tool_name: str, arguments: str):
    """只造出 call_fingerprint 需要的那两个字段。"""
    return SimpleNamespace(function=SimpleNamespace(name=tool_name, arguments=arguments))


def _pairing_legal(messages: list[dict]) -> bool:
    """history 是否协议合法：tool_calls 和 tool 结果成对且顺序正确。

    和 tests/mock_server.py 里的 _check_history 同一套判定——
    真实服务商不满足这个条件会直接 400，所以这里必须本地也能验。
    """
    pending: list[str] = []

    for message in messages:
        if message["role"] == "assistant":
            pending.extend(call["id"] for call in message.get("tool_calls") or [])
        elif message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return False
            pending.remove(call_id)

    return not pending


def run_all() -> None:
    """逐个跑上面的检查。

    故意不叫 main：本文件 import 了 main.py 那个模块，同名会把它整个盖住，
    一访问 main.call_fingerprint 就是 AttributeError。
    """
    check_fingerprint_equivalence()
    check_fingerprint_not_equal()
    check_write_context_compaction()
    check_all_context_modes()
    check_duplicate_interception()
    check_failed_call_not_locked()
    print("\n重复调用检测测试全部通过。")


if __name__ == "__main__":
    run_all()
