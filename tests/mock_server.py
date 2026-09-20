"""本地 OpenAI 兼容的假服务器，用来在没有 API Key 的情况下验证接线是否正确。

它解决一个真实问题：当请求失败时，你分不清是
  「Key 错了」/「网络不通」/「我的代码写错了」
把 OPENAI_BASE_URL 指向本地这个假服务器，前两个原因就被排除掉了。

它只做一件事：收到 POST /chat/completions，回一个结构完全符合规范的假回复。
纯标准库，不需要装任何包。

它有两种回复方式：
  1. 请求里带了 tools 参数，且你的输入命中某个触发词
     → 回一个「工具调用」，模拟模型决定去用某个工具。
  2. 请求里已经有一条 role="tool" 的结果
     → 回普通文本，并把那份结果**原样带进回复**——
       这样你能肉眼确认「工具结果真的回到了模型手里」。

用法：
    .venv\Scripts\python.exe tests\mock_server.py
    然后把 .env 改成：
        OPENAI_API_KEY=any-value
        OPENAI_BASE_URL=http://127.0.0.1:8765/v1
        OPENAI_MODEL=mock-model
    再跑 .venv\Scripts\python.exe main.py

    输入里带上触发词就能切换分支：
        列文件                → 正常列出工作目录
        todo.txt              → 正常读到文件
        越界                  → 模拟模型试图读 ../.env，被沙盒拒绝
        不存在                → 模拟读一个没有的文件
        坏参数                → 模拟模型给出了非法 JSON 参数
        大文件 / big_notes.txt → 读一个超长文件，验证结果会被截断
        写个测试文件          → 正常写进工作目录
        写越界                → 模拟模型试图写 ../evil.txt，被沙盒拒绝
        写绝对路径            → 模拟模型试图写 C:/evil.txt，被沙盒拒绝
        假工具                → 模拟模型调用一个不存在的工具名
        总是调用              → 拿到结果后仍继续要工具，验证最大步骤数保护
        中途崩                → 工具跑完之后那次请求故意 500，验证半截回合不会弄坏历史
        对比                  → 连续读两个文件再总结，验证 Agent 循环真的能跑多步
        agent_summary         → 多工具任务：list_files → read_file → write_file → 汇报
        重复调用 + 文件名       → 对同一文件提两次完全相同的调用，
                                  验证第二次被重复检测拦下（Phase 6）
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8765

# 程序只会请求这一个路径
CHAT_PATH = "/chat/completions"

# 你的输入里出现左边的词，mock 就模拟一次工具调用。
# 元组是 (触发词, 工具名, arguments 的 JSON 字符串)。
#
# 顺序敏感：更具体的词必须排在更短的词前面。
# 例如「写越界」里含「越界」，如果「越界」在前，写越界会被误判成「读 ../.env」。
MOCK_CALL_TRIGGERS = (
    ("列文件", "list_files", "{}"),
    ("写越界", "write_file", '{"path": "../evil.txt", "content": "不该写到这里"}'),
    ("写绝对路径", "write_file", '{"path": "C:/evil.txt", "content": "不该写到这里"}'),
    ("写个测试文件", "write_file", '{"path": "notes/mock_wrote.txt", "content": "mock 写的测试文件"}'),
    ("假工具", "ghost_tool", '{"path": "whatever"}'),
    ("todo.txt", "read_file", '{"path": "todo.txt"}'),
    ("agent_notes.md", "read_file", '{"path": "agent_notes.md"}'),
    ("python_notes.md", "read_file", '{"path": "python_notes.md"}'),
    ("big_notes.txt", "read_file", '{"path": "big_notes.txt"}'),
    ("越界", "read_file", '{"path": "../.env"}'),
    ("不存在", "read_file", '{"path": "nope.txt"}'),
)

# 你的输入里出现左边的词，mock 就故意模拟一次「坏的工具调用」。
# 用来验证模型的输出坏了时，会话不会被炸掉。
MOCK_BROKEN_ARGUMENTS = ("坏参数", "read_file", "{path: not-json")

# 出现这个词时，mock 拿到工具结果后仍然继续要工具，
# 用来验证 Agent 循环不会自己滚成无限循环、一定会被最大步骤数拦下来。
ALWAYS_ASK_FLAG = "总是调用"

# 出现这个词时，mock 在「工具已执行、结果已回传」之后的那次请求上故意返回 500。
# 专门用来验证「半截回合」不会把对话历史弄坏。
MIDROUND_FAILURE_FLAG = "中途崩"

# 出现这个词时，mock 模拟一个**多步任务**：先读 COMPARE_PAIR 里的第一个文件，
# 下一轮再读第二个，两本都读过了才给最终总结。
# 这是验证 Agent 循环的关键场景——真实模型会自己决定「读完一本，还要读另一本」，
# 这里用「历史里已经读过哪些文件」来驱动下一步，而不是靠你的输入。
COMPARE_FLAG = "对比"
COMPARE_PAIR = ("agent_notes.md", "python_notes.md")

# 出现这个词时，mock 模拟一个「用户只给目标、不给文件名」的多工具任务：
# 先列目录（探索）→ 挑一个文件读（判断 + 读取）→ 把摘要写回工作目录（写入）→ 汇报。
# 四个分支的判断依据全部来自历史里已经出现过哪些工具调用，不是来自你的输入——
# 这样「下一步做什么」是随历史推进自动换的，正好是真实 Agent 的形状。
# mock 在这里扮演模型，所以由它决定读哪个文件；main.py 里没有任何文件名。
SUMMARY_FLAG = "agent_summary"
SUMMARY_SOURCE = "agent_notes.md"
SUMMARY_OUTPUT = "notes/agent_summary.md"
SUMMARY_TEXT = (
    "# Agent 学习摘要\n\n"
    "本摘要由 Agent 读取 agent_notes.md 后整理生成。\n\n"
    "- Agent 的核心是一个循环：问模型 → 执行工具 → 结果回喂 → 再问\n"
    "- 模型只负责决定下一步，真正的读写由 Python 执行\n"
    "- 工具结果必须原样带回模型，否则它无从判断\n"
)

# 出现这个词时，mock 扮演一个「明知故问」的模型：对同一文件提两次
# 完全相同的工具调用。用来验证重复调用保护——第一次正常执行，
# 第二次应该被 main.py 拦下、拿到 [重复调用被拦截]，而不是再跑一遍。
# 和对比/摘要一样要搭配文件名触发词使用，例如「重复调用一下 todo.txt」。
DUPLICATE_FLAG = "重复调用"

# 工具调用 id 的前缀。每轮请求发一个唯一 id（见 _next_call_id），
# 同一个任务里跑多步时，历史里会有好几个 tool_calls，id 不能重复。
CALL_ID_PREFIX = "call_mock_"


def _history_calls(payload: dict) -> list[tuple[str, dict]]:
    """这一份历史里已经出现过哪些工具调用，按出现顺序返回 (工具名, 参数字典)。

    放在模块级而不是写成 staticmethod：它只吃 payload、用不到 self，
    而「多文件对比」和「多工具摘要」两个场景都要靠它判断下一步该做什么。

    参数不是合法 JSON 时按空字典处理——那是「坏参数」分支在故意造的情况，
    这里不该因为它而抛异常，否则整个请求会被打断。
    """
    calls = []
    for message in payload.get("messages", []):
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or ""
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = {}
            calls.append((function.get("name", "?"), parsed))
    return calls


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 用 endswith 而不是 == ：SDK 会保留 base_url 里的 /v1，
        # 实际到达的路径是 /v1/chat/completions 而不是 /chat/completions。
        # 用 == 会让所有请求都 404（实测踩过）。
        if not self.path.rstrip("/").endswith(CHAT_PATH):
            # 下面的说明文字必须是纯 ASCII：
            # send_error() 生成的响应头走 latin-1 编码，塞中文会直接 UnicodeEncodeError，
            # 导致服务器处理该请求时崩溃、客户端收到残缺响应（实测表现为 502）。
            self.send_error(404, "mock server only supports " + CHAT_PATH)
            return

        payload = json.loads(self.rfile.read(self._content_length()) or b"{}")

        # 协议校验：assistant 的 tool_calls 和 tool 结果必须成对出现，而且顺序要对。
        # 真实服务商不满足这个条件会直接 400。用它来证明「请求中途失败后
        # 把半截历史清掉」这件事真的做对了——不满足时这里就会返回 400。
        if not self._check_history(payload):
            return

        tool_names = [
            tool.get("function", {}).get("name", "?") for tool in payload.get("tools") or []
        ]
        if tool_names:
            # 这一行日志是验证「工具清单真的发出去了」的关键证据。
            self._log("收到工具清单：" + ", ".join(tool_names))

        last_user = self._last_user_message(payload)
        requested = self._requested_call(last_user)
        # 只看「这一条提问之后」的工具结果。历史是跨轮累加的，
        # 上一轮留下的 tool 结果不能算进当前这轮。
        result = self._tool_result_after_user(payload)
        if result:
            # 出现这行，说明上一轮的工具结果确实跟着请求一起发回来了。
            self._log("收到工具结果，准备基于结果回答")

        # 工具已经执行完了，但接下来这次请求故意 500。
        # 用来看程序会不会因为「半截回合」把对话历史搞成协议非法的状态。
        if result and MIDROUND_FAILURE_FLAG in last_user:
            self._send_json(500, {"error": {"message": "mock 故意的中途失败", "type": "mock"}})
            return

        # 四个多步场景必须排在单工具触发词之前：它们的提问里同样写有文件名，
        # 会被当成「读单个文件」匹配上，那样只会读一次就收口，循环跑不起来。
        if SUMMARY_FLAG in last_user:
            body = self._summary_body(payload)
        elif COMPARE_FLAG in last_user:
            body = self._comparison_body(payload)
        elif DUPLICATE_FLAG in last_user and requested:
            body = self._duplicate_body(payload, requested)
        elif requested and (ALWAYS_ASK_FLAG in last_user or not result):
            # 这一轮还没拿到工具结果，就继续要工具。
            # 例外是「总是调用」：拿到结果也继续要，用来验证最大步骤数保护。
            # requested 是 (工具名, arguments)，正好对上 _tool_call_body 的后两个参数
            body = self._tool_call_body(payload, self._next_call_id(payload), *requested)
        elif requested:
            # 这一轮已经跑完工具，就基于结果收口回答
            body = self._plain_body(payload, last_user, result)
        else:
            body = self._plain_body(payload, last_user, result)

        self._send_json(200, body)

    def _content_length(self) -> int:
        return int(self.headers.get("Content-Length", 0))

    def _check_history(self, payload: dict) -> bool:
        """校验 tool_calls 和 tool 结果是否成对、顺序是否正确。

        协议要求：
          1. assistant 提了 tool_calls，就必须紧跟着对应 id 的 tool 结果；
          2. 出现 tool 结果，前面必然有一条待应答的 tool_calls。
        缺任何一半，真实服务商都会直接 400。校验失败时自己把 400 发出去，
        并返回 False 让调用方停下来。
        """
        pending = []  # 已经提出、还没交结果的工具调用 id

        for message in payload.get("messages", []):
            if message.get("role") == "assistant":
                pending.extend(
                    call["id"] for call in message.get("tool_calls") or []
                )
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if call_id not in pending:
                    self._send_json(
                        400,
                        {
                            "error": {
                                "message": "tool 结果前面没有对应的 assistant tool_calls",
                                "type": "invalid_history",
                            }
                        },
                    )
                    return False
                pending.remove(call_id)

        if pending:
            self._send_json(
                400,
                {
                    "error": {
                        "message": "tool_calls 缺对应的 tool 结果：" + ", ".join(pending),
                        "type": "invalid_history",
                    }
                },
            )
            return False

        return True

    @staticmethod
    def _last_user_message(payload: dict) -> str:
        """取最后一条 user 消息，让你能一眼看出程序到底发了什么内容过来。"""
        for message in reversed(payload.get("messages", [])):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    @staticmethod
    def _tool_result_after_user(payload: dict) -> str:
        """取「最后一条 user 消息之后」最近一次工具结果的内容；没有就返回空串。

        为什么要限定在这条提问之后：一次会话连着问好几句话时，历史是累加的，
        上一轮留下的 tool 结果也在这份历史里。拿「整份历史里有没有 tool 结果」
        来判断「这一步是不是刚跑完工具」，会让后面每一轮都以为自己已经拿到结果，
        连工具都不调——上一轮读过的文件内容会被当成这一轮的答案重复吐出来。
        空串表示这一轮还没有工具跑过，该去要工具。
        """
        messages = payload.get("messages", [])
        last_user_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "user"
            ),
            -1,
        )

        for message in reversed(messages[last_user_index + 1 :]):
            if message.get("role") == "tool":
                return message.get("content", "")
        return ""

    @staticmethod
    def _next_call_id(payload: dict) -> str:
        """给本轮的工具调用生成一个唯一 id。

        历史里已经出现过几个 tool_calls，新 id 就是第 n+1 个。
        一个任务里会连续出现好几次工具调用，id 重复会让协议校验失败。
        """
        count = sum(len(m.get("tool_calls") or []) for m in payload.get("messages", []))
        return f"{CALL_ID_PREFIX}{count + 1}"

    def _comparison_body(self, payload: dict) -> dict:
        """模拟一个需要连续读两个文件才能回答的任务。

        判断依据是「历史里已经读过哪些」，不是你的输入：
          - 第一个文件还没读过 → 这一轮去读它
          - 第一个读过了、第二个还没 → 这一轮去读第二个
          - 两个都读过了 → 给最终总结
        这样「下一步读什么」是随着历史推进自动换的，正好对应 Agent 循环的形状。
        """
        first, second = COMPARE_PAIR
        read_paths = [
            args.get("path") for name, args in _history_calls(payload) if name == "read_file"
        ]
        read = set(read_paths)

        if first not in read:
            target = first
        elif second not in read:
            target = second
        else:
            return self._comparison_answer(payload, read_paths)

        return self._tool_call_body(
            payload,
            self._next_call_id(payload),
            "read_file",
            json.dumps({"path": target}),
        )

    def _comparison_answer(self, payload: dict, read_paths: list[str]) -> dict:
        """两个文件都读完后的最终总结，顺便报出历史里实际发生过几次读取。

        这一行「我一共读了 N 个文件」是把「Agent 真的跑了多步」变成可肉眼确认的证据：
        N 必须等于 2，而它完全来自你发回来的历史。
        """
        content = (
            "[mock 回复] 两份笔记都读完了。我一共读了 "
            f"{len(read_paths)} 个文件：" + "、".join(dict.fromkeys(read_paths)) + "。"
        )
        return self._answer_body(payload, content)

    def _summary_body(self, payload: dict) -> dict:
        """模拟「用户只给目标、不给文件名」的多工具任务，四步全由历史驱动。

          历史里没有 list_files → 先去看目录里有什么（探索）
          列过了但目标文件没读   → 挑一个文件读（判断 + 读取）
          读过了但还没写入       → 把整理好的摘要写回工作目录（写入）
          三步都做过             → 汇报

        注意这里没有任何「第 N 步」的硬编码计数：每轮都重新看一遍历史，
        所以就算中间的某一步失败重跑，它仍然知道该补哪一步。
        """
        calls = _history_calls(payload)
        call_names = [name for name, _ in calls]
        read_paths = {args.get("path") for name, args in calls if name == "read_file"}
        written_paths = {args.get("path") for name, args in calls if name == "write_file"}

        if "list_files" not in call_names:
            return self._tool_call_body(payload, self._next_call_id(payload), "list_files", "{}")

        if SUMMARY_SOURCE not in read_paths:
            return self._tool_call_body(
                payload,
                self._next_call_id(payload),
                "read_file",
                json.dumps({"path": SUMMARY_SOURCE}),
            )

        if SUMMARY_OUTPUT not in written_paths:
            return self._tool_call_body(
                payload,
                self._next_call_id(payload),
                "write_file",
                json.dumps({"path": SUMMARY_OUTPUT, "content": SUMMARY_TEXT}),
            )

        return self._summary_answer(payload, calls)

    def _summary_answer(self, payload: dict, calls: list[tuple[str, dict]]) -> dict:
        """全部做完后的最终汇报，把真实调用链报出来作为证据。"""
        chain = " → ".join(f"{name}" for name, _ in calls)
        content = (
            f"[mock 回复] 任务完成，我一共调用了 {len(calls)} 次工具：{chain}。"
            f"摘要已写入 {SUMMARY_OUTPUT}。"
        )
        return self._answer_body(payload, content)

    def _duplicate_body(self, payload: dict, requested: tuple[str, str]) -> dict:
        """模拟模型「明知故问」：对同一文件提两次完全相同的调用，被拦下后才收口。

        这是验证重复调用保护的专用场景。判断依据是「历史里这条调用
        已经出现过几次」，不靠任何外部计数：
          出现过 0 次  → 提调用（第一次，正常执行）
          出现过 1 次  → 再提一模一样的调用（应被 main.py 拦下，不真正执行）
          出现 2 次以上 → 收口给最终回答

        第二次照样要真的提调用——这样才能测到「main.py 会不会拦」，
        而不是 mock 自己假装没提。被拦下的那次也会留下 assistant tool_calls
        和一条 tool 结果，所以协议校验照常生效。
        """
        tool_name, arguments = requested
        wanted = json.loads(arguments)

        calls = _history_calls(payload)
        seen = sum(1 for name, args in calls if name == tool_name and args == wanted)

        if seen <= 1:
            return self._tool_call_body(
                payload, self._next_call_id(payload), tool_name, arguments
            )

        tool_results = sum(
            1 for message in payload["messages"] if message.get("role") == "tool"
        )
        content = (
            f"[mock 回复] 任务完成。我一共提出了 {len(calls)} 次工具调用，"
            f"收到 {tool_results} 条工具结果（其中 1 条是重复调用拦截提示）。"
        )
        return self._answer_body(payload, content)

    @staticmethod
    def _requested_call(text: str) -> tuple[str, str] | None:
        """根据输入决定这一轮要模拟什么样的工具调用。

        返回 (工具名, arguments 的 JSON 字符串)，也就是协议里那两个字段的原样形态；
        返回 None 表示「这一轮不模拟工具调用」，走普通文本回复。
        """
        trigger, tool_name, broken = MOCK_BROKEN_ARGUMENTS
        if trigger in text:
            return tool_name, broken  # 故意不是合法 JSON

        for trigger, tool_name, arguments in MOCK_CALL_TRIGGERS:
            if trigger in text:
                return tool_name, arguments

        return None

    @staticmethod
    def _tool_call_body(payload: dict, call_id: str, tool_name: str, arguments: str) -> dict:
        """模型「决定调用工具」时的回复结构。三个关键字段缺一不可：

        - content 必须是 None：模型这一轮没说话，只提了工具调用
        - finish_reason 必须是 "tool_calls"：告诉客户端这不是自然结束
        - arguments 必须是 **JSON 字符串**而不是 dict，这是协议要求
        最后一项写错，SDK 侧会直接把工具调用解析成空参数。

        tool_name 从参数传进来，不能写死成 read_file：
        Phase 5 有三个工具，mock 必须能模拟模型去用任意一个。

        call_id 必须每次都不一样：同一个任务里会连续出现多次工具调用，
        id 重复会让协议校验（也包括真实服务商）直接 400。
        """
        return {
            "id": "mock-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "mock-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    @staticmethod
    def _answer_body(payload: dict, content: str) -> dict:
        """把一段普通文本包成「模型直接回答」的回复体。

        拆出来是因为有两种回答都长一个样：读到文件内容后的复述，
        以及任务做完后的总结。骨架放一处，别写两遍。
        """
        return {
            "id": "mock-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "mock-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    def _plain_body(self, payload: dict, last_user: str, tool_result: str) -> dict:
        """模型「决定不用工具、直接回答」时的普通文本回复。"""
        if tool_result:
            # 把工具结果原样带进回复，让你能肉眼确认内容真的回到了模型手里。
            snippet = tool_result.replace("\n", " ⏎ ")[:120]
            content = f"[mock 回复] 工具返回了内容：{snippet}"
        else:
            content = f"[mock 回复] 你说的是：{last_user}"

        return self._answer_body(payload, content)

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _log(self, text: str) -> None:
        """访问日志写到 stderr，避免和主程序 stdin/stdout 交互时混在一起。"""
        sys.stderr.write("[mock] " + text + "\n")

    def log_message(self, fmt, *args):
        self._log(fmt % args)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    print(f"mock 服务器已启动：http://127.0.0.1:{port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
