# 真模型首次实测记录

日期：2026-09-19
模型：`sensenova-6.8-flash-lite` @ `https://token.sensenova.cn/v1`
配置：`.env`（OpenAI 兼容协议，网关同时支持 Anthropic 协议）
代码：Phase 5 原样，`SYSTEM_PROMPT` / 三份工具说明书 / `run_agent_loop` 一字未改
运行方式：`main.py` + stdin 重定向，全程未修改任何代码

---

## 结论

**没有 Final Answer。** 8 次问模型全部消耗在工具调用上，第 8 步撞上 `MAX_AGENT_STEPS`
被拦下，任务没收口。程序本身零故障——它按设计执行了每一次调用、拦住了越界、
在撞到上限时明确告知并丢弃了那条孤立的工具请求。

对比 mock 侧同一任务：4 次问模型 / 3 轮工具 / 正常收口。
差距全部来自模型自身的规划行为，不是代码。

---

## 逐步记录

共 **8 次问模型、7 次工具执行**。

| # | 工具 | 参数 | 结果 |
|---|---|---|---|
| 1 | `list_files` | `{"path": "."}` | 3 个文件：agent_notes.md / python_notes.md / todo.txt |
| 2 | `read_file` | `{"path": "agent_notes.md"}` | 1363 字节，全量返回 |
| 3 | `write_file` | `path=notes/agent_summary.md`, 1616 字符 | `已写入 agent_summary.md（1616 字节，已写入新文件）` |
| 4 | `list_files` | `{"path": "."}` | 多出 `[d] notes` |
| 5 | `read_file` | `{"path": "notes/agent_summary.md"}` | 读回自己刚写的摘要 |
| 6 | `write_file` | `path=notes/agent_summary.md`, 1533 字符 | `已覆盖已有文件` |
| 7 | `write_file` | 同上路径、**内容逐字节相同** | `已覆盖已有文件` |
| 8 | — | — | 模型仍要求调用工具 → 命中步数上限，停止 |

第 3 步之后任务实质上已经完成，第 4~7 步是纯冗余：重列目录、读回自己的产物、
改写一遍、再改写成完全相同的字节。

### 磁盘校验

```
demo_workspace/notes/agent_summary.md   1533 bytes
CRLF 0 个 / 裸 LF 37 个 / 以 \n 结尾
```

工具报告的 1533 字节与磁盘落盘 1533 字节一致——`newline=""` 的修复在真模型下同样成立。

---

## 逐项核对

| 检查项 | 结果 |
|---|---|
| 猜文件名 / 编造路径 | **无**。第一个动作就是 `list_files`，没有凭记忆拼文件名 |
| 文件选择 | **正确且克制**。只读 `agent_notes.md`，没去读 `python_notes.md` 和 `todo.txt` |
| 重复调用 | **有，且严重**。`write_file` 对同一文件调用 3 次；`list_files` 2 次；读回自己的产物 1 次 |
| 第 7 次调用 | 内容与第 6 次逐字节相同（仅 JSON 键序变了 `"path"` 与 `"content"` 的先后），纯无效写入 |
| 任务收口 | **失败**。第 8 步耗尽，未给出「完成后告诉我你做了什么」的回答 |
| 沙盒 | **未被突破**。三次写入全落在 `demo_workspace/notes/` 内 |
| 覆盖行为 | 按设计工作，明确回报「已覆盖已有文件」，无静默覆盖 |
| 协议合法性 | **无 400 / 无 `[请求失败]`**，7 次请求历史每次都成对合法 |
| 步数上限 | 按设计工作：停在第 8 步、明确告知、丢弃孤立工具请求 |
| 结果截断 | 未触发（1363 字节 < 4000 字符上限） |

---

## 归因

**模型行为，不是代码缺陷。不动代码。**

依据：程序的每一项设计都在按预期工作——沙盒拦截、覆盖策略、长度保护、
协议清理、步数上限、上限处的干净退出，全部符合 Phase 5 的设计说明。
失败的模式是「写完之后不知道该收手」：`write_file` 已经回报了成功和字节数，
模型却继续重列目录、读回自己的产物、再写两遍。

这是 Phase 5 的 `MAX_AGENT_STEPS` 存在的理由——它没有防住「原地打转」，
只兜住了「不会无限跑」。README 已声明这一层刻意不做。

---

## 一处可观察性缺口（仅记录，未修）

`ask()` 只返回 `response.choices[0].message`，把 `finish_reason` 和 `usage` 都丢掉了。
所以本轮**无法报告每步的 `finish_reason`**——只能从「`reply.tool_calls` 非空」反推出
前 7 次都是工具调用、第 8 次也在要工具。

按「先停下记录、不修」的要求，没有动 `main.py`。

---

---

# Phase 6 复测记录

日期：2026-09-19
模型：`sensenova-6.8-flash-lite` @ `https://token.sensenova.cn/v1`（与首轮同一模型、同一网关）
代码：Phase 6（可观测性 + 重复调用检测 + 提示词收敛原则）
任务：与首轮**完全相同**，只有输出路径改成了 `notes/agent_summary_real_v2.md`
运行方式：`cmd /c '.venv\Scripts\python.exe main.py < tests\inputs\real_retest_v2.txt'`

---

## 结论

**有 Final Answer。** 5 次问模型 / 5 次真实工具执行，第 4 步写完文件，
**第 5 步立刻给出最终回答**——没有重列目录、没有读回自己的产物、没有第二次写入。
`MAX_AGENT_STEPS` 没有被碰到（用了 5 / 8）。

对比首轮同一任务：8 次问模型 / 7 次工具执行 / 撞上步数上限 / 无最终回答。

### 首轮 vs 本轮

| | Phase 5.5（首轮） | Phase 6（本轮） |
|---|---|---|
| 问模型次数 | 8 | **5** |
| 真实工具执行 | 7 | **5** |
| `write_file` 次数 | 3（同一路径，第 2、3 次内容逐字节相同） | **1** |
| 读回自己的产物 | 1 次 | **0 次** |
| 重列目录 | 1 次（无新信息） | **0 次** |
| 撞 `MAX_AGENT_STEPS` | 是（第 8 步） | **否（5 / 8）** |
| Final Answer | **无** | **有** |
| 每步 `finish_reason` | 无法报告（`ask()` 丢弃） | **全部有记录** |
| token 用量 | 无法报告 | **每步都有** |

---

## 逐步记录

共 **5 次问模型、5 次真实工具执行、0 次重复调用被拦**。

| Turn | finish_reason | 工具与参数 | prompt / completion / total tokens |
|---|---|---|---|
| 1 | `tool_calls` | `list_files({})` → 3 文件 + 1 目录 | 861 / 29 / 890 |
| 2 | `tool_calls` | `read_file({"path": "agent_notes.md"})`（1363 字节）**+** `list_files({"path": "notes"})` | 944 / 73 / 1017 |
| 3 | `tool_calls` | `read_file({"path": "notes/agent_summary.md"})`（读首轮产物作参考） | 1363 / 58 / 1421 |
| 4 | `tool_calls` | `write_file({"path": "notes/agent_summary_real_v2.md", ...})` → `已写入（1617 字节，已写入新文件）` | 1808 / 508 / 2316 |
| 5 | `stop` | — → **最终回答** | 2312 / 216 / 2528 |

合计：**prompt 7288 / completion 884 / total 8172 tokens**。服务商每次请求都返回了 `usage`，无一项缺失。

`finish_reason` 链：`tool_calls → tool_calls → tool_calls → tool_calls → stop`。
最后一条是 `stop`，说明这一轮模型是自己决定不再要工具的，不是被步数上限截断的。

### 磁盘校验

```
demo_workspace/notes/agent_summary_real_v2.md   1617 bytes
```

工具回报的 1617 字节与磁盘一致。

### 重复调用这一项：本轮没有出现

模型本轮**没有**提出任何重复调用，所以拦截次数是 0，没有触发提示。
这不是被压住了——是模型没有那样做。这一点必须和「检测器没生效」区分开，
下面用 mock 单独验证检测器本身。

---

## 重复调用保护独立测试（mock，不需要 Key）

目的：真模型本轮没触发重复调用，检测器有没有真的生效无法从上面看出。
改用 mock 模型强制它对同一文件提两次完全相同的调用。

输入：`重复调用一下 todo.txt`（`tests/inputs/dup_call.txt`）

```
── Turn 1 ──
  finish_reason : tool_calls
Agent 想调用工具：read_file({"path": "todo.txt"})
Tool 结果 > （真实内容，483 字节）

── Turn 2 ──
  finish_reason : tool_calls
Agent 想调用工具：read_file({"path": "todo.txt"})
（重复调用被拦截，未真正执行）
Tool 结果 > [重复调用被拦截]
这个工具和完全相同的参数刚刚已经成功执行过。
这次调用没有新的信息。
请重新判断用户目标是否已经完成；
如果已经完成，请直接给出最终回答。

── Turn 3 ──
  finish_reason : stop
Agent > [mock 回复] 任务完成。我一共提出了 2 次工具调用，收到 2 条工具结果（其中 1 条是重复调用拦截提示）。
```

mock 服务器日志：3 次 POST，**全部 200，零 400**。
说明拦下那次调用时，历史仍然是协议合法的——`assistant tool_calls` 和 `tool` 结果成对且顺序正确。

单元测试（`tests/test_loop.py`，4 组全过）：

- 键序换了 / 多了空格 / 无空格的同一个调用 → 同一个指纹（4 种写法）
- 不相互误判：`read_file("a.md")` vs `read_file("b.md")`；`write_file` 同路径不同内容；
  `list_files(".")` vs `list_files("notes")`；参数相同但工具不同
- 参数不是合法 JSON → 不抛异常，退回原始字符串（拿不准宁可放行）
- **失败的调用不进检测表**——否则模型改个参数重试会被当成重复，永远过不去

---

## 归因

这次改动拆成两类，不能混在一起讲：

**模型行为改善**（本轮实测直接观察到）：

- 写完文件后立刻收口，没有第二次写入、没有重列目录、没有读回自己的产物。
  首轮这一步之后跟了 4 步纯冗余，本轮是 0 步。
- 读了首轮的产物 `notes/agent_summary.md` 作为参考，避免重复造轮子——
  这是有信息量的读取，不是打转。

**Runtime 保护**（本轮实测未触发，但独立验证过生效）：

- 重复调用检测：mock 强制复现下第一次执行、第二次拦下、第三次正常收口。
- 可观测性：每步的 `finish_reason` 和 token 用量都能拿到，首轮做不到。

**一句话回答**：「做完却不停止」的主要问题已解决——同一任务从 8 步无收口
变成 5 步正常 Final Answer。但这是**单次运行**（n=1）的对比，
没有对照组，所以只能说「这次表现明显更好、且机制上对症」，
不能说「必然每次都这样」。防再犯靠的是三层防线同时存在，
其中第 1 层（模型自己判断）是主要变化点。

---

# Phase 9 真实分段读取验证

日期：2026-09-20
模型：`sensenova-6.8-flash-lite` @ `https://token.sensenova.cn/v1`
任务：阅读 `long_notes.md`，找到 `TARGET_FACT` 的值；允许按需要分段读取。
代理：`127.0.0.1:9674`；未打印 API Key、Authorization 或敏感 Header。

## 结论

**通过。** Provider 请求成功，模型没有被 Python 自动翻页，实际自主提出了 5 次
`read_file`，最后给出 Final Answer；没有撞 `MAX_AGENT_STEPS`，没有 Sandbox、Session、
Context 回归。

## 真实 Tool 链

| Turn | finish_reason | Tool 调用与实际返回范围 | prompt / completion / total |
|---|---|---|---|
| 1 | `tool_calls` | `read_file(path="long_notes.md")` → 1-100，`has_more=true`，next=101 | 1056 / 48 / 1104 |
| 2 | `tool_calls` | 101-200 → 201-300 → 301-400；最后一段 `has_more=true`，next=401 | 2037 / 243 / 2280 |
| 3 | `stop` | 401-450，`has_more=false`，next=null | 5977 / 37 / 6014 |

合计：**3 model calls / 5 tool calls；prompt 9070 / completion 328 / total 9398 tokens**。

## Final Answer

```text
在 `long_notes.md` 的第 377 行找到了：

**TARGET_FACT = "phase9-secret-value"**
```

模型实际看到了完整的 1-100、101-200、201-300、301-400、401-450 连续行段；正常
`read_file` 结果没有触发 `MAX_TOOL_RESULT_CHARS` 的最终截断。真实验证使用修复后的
metadata，`has_more` 与实际返回范围一致。

---

# Phase 10 真实 Tool Permission / Side-effect Approval 验证

日期：2026-09-20
模型：`sensenova-6.8-flash-lite` @ `https://token.sensenova.cn/v1`
任务：把 `notes/approval_test.md` 改成 `NEW`；审批时选择拒绝。
测试文件初始内容：`ORIGINAL`
代理：`127.0.0.1:9674`（本机 `7897` 无监听；未打印 API Key、Authorization 或敏感 Header）
Session：`20260920-202930-fabf1d`

## 结论

**通过。** 模型先读取文件，再提出 `write_file`；Runtime 显示 `OVERWRITE` 并询问审批。
输入 `N` 后没有调用真正的 `write_file`，拒绝结果回传模型，模型正常给出 Final Answer，
没有撞 `MAX_AGENT_STEPS`。文件最终内容仍为 `ORIGINAL`。

## 真实 Tool 链与审批记录

| Turn | Tool Call | 权限 / 操作 | 审批 | Tool actually executed? | Tool Result |
|---|---|---|---|---|---|
| 1 | `read_file({"path": "notes/approval_test.md"})` | `READ_ONLY` | 自动 | 是 | 返回 1 行，内容 `ORIGINAL` |
| 2 | `write_file({"path": "notes/approval_test.md", "content": "NEW"})` | `SIDE_EFFECT` / `OVERWRITE` | DENY | 否 | `[用户拒绝执行]`；`write_file 未执行。文件没有被修改。` |

拒绝后的下一次模型请求收到合法的 `role="tool"` 结果，并在 Turn 3 收口：

```text
已停止操作：write_file 被拒绝，notes/approval_test.md 未被修改，内容仍为 ORIGINAL。
```

## 可观测性

| 指标 | 数值 |
|---|---:|
| model calls | 3 |
| tool calls | 2 |
| tool calls actually executed | 1 (`read_file`) |
| denied side effects | 1 (`write_file`) |
| prompt tokens | 3430 |
| completion tokens | 182 |
| total tokens | 3612 |

`finish_reason` 链：`tool_calls → tool_calls → stop`。Session 已正常保存；最终磁盘校验为：

```text
demo_workspace/notes/approval_test.md = ORIGINAL
```

---

# Phase 12.5 真实模型链路验证

日期：2026-09-20
基线：`0b07594 Phase 12: add controlled command execution`
模型配置：`.env` 中 `OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY` 均存在；未记录值。
Windows 用户代理：`127.0.0.1:9674`；WinHTTP：直连。
请求进程使用 HTTP/HTTPS/ALL 代理 `http://127.0.0.1:9674`，未输出 API Key、Authorization
或敏感 Header。`.venv` 中无法直接导入 `httpx`，因此没有独立读取 `httpx trust_env`；
OpenAI SDK 请求本身已到达 Provider。

## 最小 Provider 请求

请求内容：`回复 OK`，不带 Tool。

结果：**失败**。Provider 返回 HTTP 429：

```text
inference exceeds tpm/rpm limit
```

归类：Provider 限流，不是 Runtime、Sandbox、Permission 或 `run_command` 错误。
按要求未重试，也未进入真实 Agent 测试。

## Phase 12.5 Agent 闭环

未执行。由于最小 Provider 请求未成功：

- 实际 Tool：无
- `run_command`：未请求
- approval decision：无
- subprocess：未启动
- model calls / tool calls：无可用 Agent 数据
- token usage：无
- Final Answer：无

---

# Phase 12.5 真实模型链路验证（第二次）

日期：2026-09-21
基线：`0b07594 Phase 12: add controlled command execution`
本次未修改 Runtime，未运行完整本地测试，未进入 Phase 13。

## 预检与最小 Provider 请求

- `OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY`：均存在；只记录存在性，未记录值。
- Windows 用户代理：`127.0.0.1:9674`，TCP 可达；本次未使用 `127.0.0.1:7897`。
- 最小请求：`回复 OK`，不带 Tool。
- Provider 结果：成功，响应内容为 `OK`。
- 最小请求 token：prompt `84`，completion `73`，total `157`。

## 真实 Agent 链路

任务原文：`运行 Phase 9 的长文件相关测试，告诉我测试是否通过。`

模型主动请求了 `run_command`。`TOOL_APPROVAL_MODE=ALLOW`，允许的
`run_command` 经过 Phase 11 Permission Runtime；风险等级均为 `EXECUTION`。
命令策略拒绝发生在 Permission callback 之前，因此被拒绝的 `git show` 没有启动
subprocess。

| Agent Turn | Tool Call | command / args | cwd | risk | approval | subprocess | exit | timed_out |
|---|---|---|---|---|---|---|---:|---|
| 2 | `run_command` | `git` / `["log", "--oneline", "-20"]` | `C:\Users\30858\mini-agent-lab` | `EXECUTION` | `ALLOW` | 是 | 0 | false |
| 3 | `run_command` | `git` / `["show", "--stat", "2a0ac8e"]` | `C:\Users\30858\mini-agent-lab` | `EXECUTION` | 未到达（Command Policy 拒绝） | 否 | N/A | false |
| 4 | `run_command` | `python` / `["-m", "pytest", "tests/test_long_file.py", "-v"]` | `C:\Users\30858\mini-agent-lab` | `EXECUTION` | `ALLOW` | 是 | 1 | false |
| 5 | `run_command` | `python` / `["-m", "unittest", "tests.test_long_file", "-v"]` | `C:\Users\30858\mini-agent-lab` | `EXECUTION` | `ALLOW` | 是 | 0 | false |

### Tool Result 摘要

- `git log`：返回当前 Phase 12 提交历史，正常完成。
- `git show`：返回 `CommandPolicyError`，原因是第一版策略只允许 `git status`、`git diff`、`git log`；未启动进程。
- `python -m pytest tests/test_long_file.py -v`：非零退出，stderr 为 `.venv` 未安装 `pytest`。
- `python -m unittest tests.test_long_file -v`：退出码 `0`，timed out 为 `false`；`Ran 10 tests in 3.219s`，`OK`。stdout 为空，测试明细位于 stderr。

Final Answer 原文：

```text
Phase 9 的长文件相关测试全部通过 ✅

python -m unittest tests.test_long_file -v

Ran 10 tests in 3.219s，OK

通过的具体测试包括：
- test_default_read_returns_first_segment_and_metadata
- test_start_line_and_max_lines_select_exact_range
- test_metadata_marks_last_segment
- test_large_ranges_return_complete_lines_with_consistent_metadata
- test_empty_and_utf8_files
- test_invalid_ranges_are_model_readable_errors
- test_past_end_is_clear
- test_single_line_over_safe_budget_is_an_explicit_tool_error
- test_result_length_guard_remains_active
- test_mock_agent_reads_until_fact

注意：`python -m pytest` 先执行失败，因为 `.venv` 中没有安装 pytest；随后改用
仓库已有的 `python -m unittest` 执行并全部通过。
```

## 调用与闭环判定

- Agent model calls：`6`
- Agent tool calls：`6`，其中 `run_command` `4` 次
- Agent token usage：prompt `14635`，completion `634`，total `15269`
- 加上最小 Provider 请求：model calls `7`，tool calls `6`，tokens `14719 / 707 / 15426`
- 未撞 `MAX_AGENT_STEPS`；模型收到 Tool Result 后给出 Final Answer。
- `shell=False` 由既有 `run_command` 实现实际执行；本次未修改该实现。

核心链路已实际发生：
`LLM → run_command → EXECUTION → ALLOW → subprocess(shell=False) → Tool Result → LLM → Final Answer`。

但 `cli.py` 在打印最终 JSON 结果时，因 Windows 默认 GBK 无法编码 Final Answer 中的
`✅`，额外抛出 `UnicodeEncodeError`。这发生在 Agent 已生成 Final Answer 之后，仍属于
本次运行的 Runtime/CLI 异常。按照本阶段“无 Runtime exception”的严格成功标准，
Phase 12.5 已证明核心受控执行链路，但 Phase 12 目前**不正式关闭**；本次不修改 Runtime，
也不重跑 Agent。

---

# Phase 12 CLI Unicode 输出修复

日期：2026-09-21

问题根因：`cli.py` 使用 `print(json.dumps(result, ensure_ascii=False))` 直接写入
Windows 默认 GBK stdout；模型 Final Answer 中的 `✅` 无法编码，导致最终 JSON 输出
抛出 `UnicodeEncodeError`。问题位于 CLI Output / Encoding，不涉及 LLM、Agent Loop、
`run_command`、subprocess、Permission 或 Command Policy。

修复：在最终 JSON 输出前仅调用 `sys.stdout.reconfigure(encoding="utf-8")`（当 stdout
支持 `reconfigure` 时），不修改 stdin、模型输出或 Agent Runtime。

新增 `tests/test_cli.py` Unicode 回归，使用模拟 `cp936` stdout 验证以下内容均以 UTF-8
输出且没有静默删除：

- `OK`
- `测试全部通过`
- `测试全部通过 ✅`
- `{"message": "测试全部通过 ✅", "ok": true}`

## Phase 12 回归结果

```text
Ran 49 tests in 5.323s
OK
```

覆盖 CLI、Command、Permission、Session、Context/Loop、Long-file、Sandbox；未运行完整
Eval，未调用真实模型。Phase 12 真实链路此前已完成，本次补齐 Final Answer → CLI 输出
边界验证，现正式关闭。

---

# Phase 13：Bounded Coding Loop

日期：2026-09-21
基线：`41a1c60 Phase 12: fix Unicode CLI output`（Phase 12 command runtime 的前一节点为
`0b07594 Phase 12: add controlled command execution`）

## 实现范围

本阶段没有加入 Planner、Coding 状态机、MCP、Memory、Multi-Agent、网络命令或 Git 写操作。
改动集中在：

- `main.py`：增加任务级内存 `CodingTaskTrace`；记录模型轮次、工具参数摘要、审批、结果摘要、
  `exit_code`、写入目标、调用计数和 token 汇总。
- `main.py`：`run_command` 的非零退出不再进入成功重复调用集合。它仍是已经执行过的命令，
  但不是成功动作；模型修复文件后可以再次运行同一条测试命令。
- `cli.py`：单任务 JSON 结果包含 Trace 汇总。
- `tests/fixtures/coding_workspace/`：独立的 `calculator.py` 和 `test_calculator.py` fixture，
  初始状态故意让 `add` / `subtract` 的运算符写反。
- `tests/test_coding_loop.py`：Mock A/B/C、初始失败、失败重测、审批拒绝、重复保护和 Trace 测试。

## Mock Coding Loop A

初始 fixture 测试：`python -m unittest test_calculator -q`，`Exit code: 1`。

确定性链路：

```text
read_file(calculator.py)
→ write_file(calculator.py, fixed content)
→ run_command(python -m unittest test_calculator -q), Exit code: 0
→ Final Answer
```

文件实际变为 `add → a + b`、`subtract → a - b`，Trace 记录 `write=1`、`run=1`、
`max_steps_reached=false`。

## Mock Coding Loop B

确定性链路：

```text
read
→ write v1（只修 add）
→ run tests, Exit code: 1
→ write v2（再修 subtract）
→ run 同一条 tests 命令, Exit code: 0
→ Final Answer
```

第二次 `run_command` 确实启动了新的 subprocess；测试失败没有被误判为 Runtime Failure，
也没有被重复调用保护拦住。

## Mock Coding Loop C

Mock 模型反复请求同一个读取工具，不给 Final Answer。测试确认：

- `model_calls = 8`
- `tool_calls = 7`
- `MAX_AGENT_STEPS = 8` 被命中
- 没有无限循环，没有孤立的 assistant tool-call 写入 canonical history

## 本地验证

```text
python -m unittest tests.test_coding_loop -q
Ran 6 tests ... OK

python -m unittest discover -s tests -p "test_*.py" -q
Ran 55 tests ... OK

git diff --check
OK
```

55 项回归覆盖 Coding Loop、Permission、Sandbox、Session、Context/Long-file、Command、CLI 和
既有 Agent Loop；未安装依赖，未扩大 Command Policy。

## 一次真实 Coding Task

真实任务原文：`修复 calculator.py，让对应测试全部通过。完成后告诉我改了什么。`

真实 fixture 位于临时 `AGENT_WORKSPACE`：
`C:\Users\30858\AppData\Local\Temp\mini-agent-coding-c6b340c0142c4376a7f947976c3866ba`

模型没有被告知 bug 所在行、正确代码或工具顺序，实际链路为：

```text
Turn 1  list_files({})
Turn 2  read_file(calculator.py)
        read_file(test_calculator.py)
Turn 3  write_file(calculator.py)
Turn 4  run_command(python -m unittest test_calculator.py), Exit code: 0
Turn 5  Final Answer
```

权限：`write_file=ALLOW`、`run_command=ALLOW`；两者均经过现有 Permission Runtime。
命令使用现有 `shell=False` 和 workspace cwd；没有 Sandbox violation，没有 Runtime exception，
没有撞 `MAX_AGENT_STEPS`。

最终测试：`python -m unittest test_calculator.py`，2 个测试通过，`Exit code: 0`。
最终文件实际为：

```python
def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
```

Final Answer 原文：

```text
测试全部通过（2 个用例 OK）。

修改内容：`calculator.py` 中两个函数的运算符写反了——`add` 用的是减号，`subtract` 用的是加号。已对调：
- `add(a, b)` 现在返回 `a + b`
- `subtract(a, b)` 现在返回 `a - b`
```

### 真实调用统计

| 指标 | 数值 |
|---|---:|
| model calls | 5 |
| tool calls | 5 |
| `write_file` calls | 1 |
| `run_command` calls | 1 |
| prompt tokens | 8267 |
| completion tokens | 344 |
| total tokens | 8611 |
| MAX_AGENT_STEPS | 未命中 |

真实任务最终通过的是项目已有的 `cli.py --task` 单任务入口；先前一次交互 stdin 管道没有形成
Final Answer，fixture 随后恢复到初始状态，未计入上述成功链路。

---

# Phase 14：Coding Task Contract / Machine-Verifiable Acceptance

日期：2026-09-21
基线：`c3de53e Phase 13: add bounded coding loop validation`

## 实现范围

新增 `acceptance.py`，没有新增 Agent Tool，也没有修改普通 `run_agent_loop` 的决策逻辑。
Contract 至少包含：`task_id`、`instruction`、`allowed_paths`、固定 `test_command` 和
`require_test_pass`。Verifier 独立完成：

```text
Contract load
→ task-start workspace snapshot
→ ordinary Agent Loop / Trace
→ changed_files = before/after file hash diff
→ unexpected_changes = changed_files - allowed_paths
→ independent final test through safe run_command
→ explainable acceptance result
```

Verifier 不调用 LLM、不读取 Session、不使用 Agent Tool Call。`run_command` 仍保持
`shell=False`、timeout、cwd sandbox 和 command allowlist。快照检测普通文件的新增、删除和
内容变化；`__pycache__` / `.pyc` 是 Python 测试运行时产物，不计入源码变化。

## Mock Contract 结果

| Case | Agent/最终状态 | Verifier 结果 |
|---|---|---|
| A | 只改 `calculator.py`，最终测试 exit 0，有 Final | PASS；`unexpected_changes=[]` |
| B | 改 `test_calculator.py` 让错误实现通过，最终测试 exit 0 | FAIL；`unexpected file changed: test_calculator.py` |
| C | 有 Final Answer，但代码仍错误 | FAIL；`final_test_failed` |
| D | 曾测试通过，之后回归为错误代码 | FAIL；Verifier 最终重跑失败 |

## 本地验证

```text
python -m unittest tests.test_acceptance tests.test_cli -q
Ran 16 tests ... OK

python -m unittest discover -s tests -p "test_*.py" -q
Ran 66 tests ... OK

git diff --check
OK
```

## 一次真实 Coding Task

Contract 文件：`tests/fixtures/coding_contract.json`

任务 instruction：`修复 calculator.py，让对应测试通过。完成后告诉我改了什么。`

真实 workspace：
`C:\Users\30858\AppData\Local\Temp\mini-agent-phase14-real-5fa0fc8182af40358114fe9afdc30df0\coding_workspace`

模型没有被告知正确代码，实际链路：

```text
Turn 1  list_files({})
Turn 2  read_file(calculator.py) + read_file(test_calculator.py)
Turn 3  write_file(calculator.py)
Turn 4  重复 write_file(calculator.py)，被重复检测拦截
Turn 5  run_command 参数类型错误，被 Command Policy 拒绝
Turn 6  run_command(python -m unittest test_calculator.py -v)，Exit code: 0
Turn 7  重复 read_file(calculator.py)，被重复检测拦截
Turn 8  仍请求工具，撞 MAX_AGENT_STEPS；没有 Final Answer
```

模型实际只修改了允许的 `calculator.py`，内容已修复；`test_calculator.py` 未改。
它执行过一个测试命令，但不是 Contract 完全一致的命令（Contract 要求 `test_calculator -q`，
模型执行的是 `test_calculator.py -v`），因此报告中的 `agent_ran_required_test=false` 是严格匹配的结果。

### Agent 统计

| 指标 | 数值 |
|---|---:|
| model calls | 8 |
| tool calls | 8 |
| executed tool calls | 5 |
| `write_file` calls | 2 |
| `run_command` calls | 2 |
| prompt tokens | 14637 |
| completion tokens | 575 |
| total tokens | 15212 |
| `MAX_AGENT_STEPS` | 命中 |

### Independent Verifier 结果

```json
{
  "task_id": "calculator_fix",
  "accepted": false,
  "changed_files": ["calculator.py"],
  "unexpected_changes": [],
  "agent_ran_required_test": false,
  "final_test_exit_code": 0,
  "final_test_passed": true,
  "agent_final_answer_present": false,
  "max_steps_reached": true,
  "runtime_exception": null,
  "reasons": ["agent_final_answer_missing", "max_agent_steps_reached"]
}
```

这次真实运行证明：Agent 做对代码、测试通过、甚至工作区没有越界修改，仍不等于
Coding Task accepted；Final Answer 和步数上限也是机器验收契约的一部分。

---

# Phase 15：Coding Completion & Budget Control

日期：2026-09-21
基线：`ab440bf Phase 14: add coding task acceptance contracts`

## 实现范围

Phase 15 保持 `MAX_AGENT_STEPS = 8`，没有用提高步数解决收口问题，也没有新增 Planner、
Reviewer LLM、MCP、Memory、RAG、Diff/Patch Tool、Parallel Execution 或 Git 写操作。

### Completion Trace

`CodingTaskTrace` 现在按 Runtime 已有信息为每个 Tool Call 分类：

| 分类 | 判定 |
|---|---|
| `PRODUCTIVE` | 非命令工具正常进入执行路径 |
| `BLOCKED_DUPLICATE` | 完全相同的已成功调用被重复保护拦截 |
| `POLICY_REJECTED` | 审批拒绝或 Command Policy / preflight 拒绝 |
| `FAILED_COMMAND` | `run_command` 已执行但 exit code 不是 0（含无可解析成功码） |
| `SUCCESSFUL_COMMAND` | `run_command` 已执行且 exit code 为 0 |

每个 Coding Task 的 summary 额外记录：`executed_tools`、`duplicate_blocked`、
`policy_rejected`、`failed_commands`、`successful_commands`；原有 model/tool/token 统计保留。

### Acceptance 状态

Verifier 结果现在同时给出：

```text
artifact_passed      = unexpected_changes 为空 且 Contract 最终测试通过
interaction_completed = 有 Final Answer 且没有 MAX_AGENT_STEPS
accepted              = artifact_passed AND interaction_completed AND 其它现有必要条件
```

因此 Phase 14 的真实结果可以更准确地表达为：

```text
artifact_passed=true
interaction_completed=false
accepted=false
```

独立 Verifier 没有改变：仍然重新计算 workspace diff，并用 Contract 固定命令重跑最终测试。

### Guidance 与 Completion Hint

Contract 模式的模型上下文增加了短 Guidance，明确目标文件、`command + args + cwd` required
test 和收口规则：代码改动完成、required test 成功、没有新错误/未满足要求时，直接 Final
Answer，简述改动和测试结果。

只有严格匹配 Contract 的 required test 且该次 `run_command` exit 0，Tool Result 才附加：

```text
[Completion status]
Command succeeded.
If this satisfies the requested coding task and no work remains, return the final answer instead of repeating reads/writes/tests.
```

普通成功命令（例如 `git status`）不会触发该 Hint。Runtime 不自动生成 Final Answer，重复调用
也仍然回喂合法 Tool Result，由模型决定是否收口。

## Mock A-E

| Case | 链路 | 结果 |
|---|---|---|
| A | `read → write → required test pass → Final` | 通过；成功测试有 Hint，随后 Final |
| B | `read → write v1 → test fail → write v2 → test pass → Final` | 第一次失败不触发 Hint，第二次成功触发 |
| C | `write → same write again` | 第二次不执行，分类 `BLOCKED_DUPLICATE`，仍可随后 Final |
| D | `bad command → POLICY_REJECTED → correct required test → PASS → Final` | 策略拒绝不终止循环，合法测试成功并收口 |
| E | 持续请求工具 | `MAX_AGENT_STEPS = 8` 仍触发，未删除保险丝 |

## 本地验证

```text
python -m unittest tests.test_coding_loop tests.test_acceptance tests.test_cli -q
Ran 24 tests ... OK

python -m unittest discover -s tests -p "test_*.py" -q
Ran 68 tests ... OK

git diff --check
OK
```

现有能力回归通过：Coding Loop、Acceptance、Command、Permission、Sandbox、Session、Context、
Long-file、CLI 及其余 tests 全部通过。`MAX_AGENT_STEPS` 仍为 8。

## Phase 15 真实模型实验

按要求准备了全新的同构 fixture，使用同一个 `tests/fixtures/coding_contract.json`、同一个
allowed path、同一个 required test 和同一个 `MAX_AGENT_STEPS=8`。但真实模型链没有成功启动，
因此不能提供伪造的 model/tool calls 或 token 对比。

第一次启动结果：

```text
OPENAI_BASE_URL=https://token.sensenova.cn/v1
ImportError: Using SOCKS proxy, but the 'socksio' package is not installed.
model_calls=0, tool_calls=0
```

修正本次运行的代理选择为 HTTP/HTTPS 后，第二次启动仍未进入模型：

```text
APIConnectionError: Connection error.
model_calls=0, tool_calls=0
```

只读连通性证据：`Test-NetConnection 127.0.0.1 -Port 7897` 返回
`TcpTestSucceeded=False`，通过该代理访问 `token.sensenova.cn:443` 失败。故 Phase 15 的
真实 Agent 链、Final Answer、是否早于 Phase 14 收口、token 对比和独立 Verifier 真实结果均为
“未执行/不可验证”；本地 Mock 与独立 Verifier 结果不受影响。

## 结论

Phase 15 已完成代码与本地验证，证明了 Completion Hint、双状态 Acceptance、Trace 分类和
8 步保险丝的行为。尚未证明真实模型在同一 fixture 上比 Phase 14 更早收口；下一步最值得做的
是恢复可用的模型/代理后，仅重跑这一项对照实验，不扩展系统架构。

---

# Phase 15.5：Phase 14 vs Phase 15 真实对照验证

日期：2026-09-21
Runtime 基线：`0bb18dc Phase 15: improve coding task completion control`

本次只做验证，没有修改 Runtime、Prompt、Completion Hint、Contract、fixture 或
`MAX_AGENT_STEPS`。使用了新的临时 workspace，但内容逐字复制自同一个
`tests/fixtures/coding_workspace/`；Contract 仍是 `tests/fixtures/coding_contract.json`。

## Provider / Proxy 检查

三个 Provider 配置字段均存在，未打印任何值：

```text
OPENAI_API_KEY=present
OPENAI_BASE_URL=present
OPENAI_MODEL=present
```

当前代理状态：

```text
HTTP_PROXY=http://127.0.0.1:9674
HTTPS_PROXY=http://127.0.0.1:9674
ALL_PROXY=absent
NO_PROXY=absent
WinHTTP=Direct access
WinINET ProxyEnable=1, ProxyServer=127.0.0.1:9674
```

OpenAI SDK 当前使用 `httpx2 2.13.0`；`Client.trust_env` 默认值和实际 Client 值均为
`True`，因此本次请求确实读取了当前环境代理。

## 最小 Provider 请求

请求内容为 `回复 OK`，不带 Tool，单次成功：

```text
provider_request=success
finish_reason=stop
response_is_ok=True
prompt_tokens=84
completion_tokens=88
total_tokens=172
```

## Phase 15 真实 Coding Task 链

任务仍为：`修复 calculator.py，让对应测试通过。完成后告诉我改了什么。`

| Turn | Tool / 状态 | Classification | Approval | required test | Completion Hint |
|---:|---|---|---|---|---|
| 1 | `list_files({})`，列出两个 fixture 文件 | `PRODUCTIVE` | `AUTO` | 否 | 否 |
| 2 | `read_file(calculator.py)` + `read_file(test_calculator.py)` | `PRODUCTIVE ×2` | `AUTO` | 否 | 否 |
| 3 | `write_file(calculator.py)`，修正 `add/subtract` | `PRODUCTIVE` | `ALLOW` | 否 | 否 |
| 4 | 完全相同的 `write_file(calculator.py)`，未执行 | `BLOCKED_DUPLICATE` | `DUPLICATE_BLOCKED` | 否 | 否 |
| 5 | 完全相同的 `read_file(calculator.py)`，未执行 | `BLOCKED_DUPLICATE` | `DUPLICATE_BLOCKED` | 否 | 否 |
| 6 | `python -m unittest test_calculator -q`，exit 0 | `SUCCESSFUL_COMMAND` | `ALLOW` | **是** | **是** |
| 7 | 模型返回 Final Answer | — | — | — | — |

Turn 6 的 Tool Result 包含完整测试成功信息：`Ran 2 tests ... OK`，并附加 Completion
Hint。模型随后没有继续 read/write/test，而是在 Turn 7 直接给出 Final Answer。

### Agent 统计

```text
model_calls=7
tool_calls=7
executed_tools=5
duplicate_blocked=2
policy_rejected=0
failed_commands=0
successful_commands=1
productive_calls=4
agent_ran_required_test=true
completion_hint_triggered=true (Turn 6)
max_steps_reached=false
agent_final_answer_present=true
prompt_tokens=12982
completion_tokens=613
total_tokens=13595
```

Final Answer 说明 `calculator.py` 中 `add` 和 `subtract` 的实现原先写反，已修正，
并报告 `python -m unittest test_calculator -q` 通过。

## 独立 Verifier

Agent 结束后照常执行 Contract Verifier，没有使用 Agent 自己的测试结果替代最终验收：

```json
{
  "artifact_passed": true,
  "interaction_completed": true,
  "accepted": true,
  "changed_files": ["calculator.py"],
  "unexpected_changes": [],
  "agent_ran_required_test": true,
  "final_test_exit_code": 0,
  "final_test_passed": true,
  "agent_final_answer_present": true,
  "max_steps_reached": false,
  "runtime_exception": null,
  "reasons": []
}
```

## 与 Phase 14 对比

| 指标 | Phase 14 | Phase 15.5 |
|---|---:|---:|
| model calls | 8 | **7** |
| tool calls | 8 | **7** |
| executed tools | 5 | 5 |
| productive | 4 | 4 |
| duplicate blocked | 2 | 2 |
| policy rejected | 1 | **0** |
| failed commands | 0 | 0 |
| successful commands | 1 | 1 |
| required test 实际执行 | false | **true** |
| Completion Hint | 无 | **Turn 6** |
| MAX_AGENT_STEPS | true | **false** |
| Final Answer | false | **true** |
| prompt tokens | 14637 | **12982** |
| completion tokens | 575 | 613 |
| total tokens | 15212 | **13595** |

本次模型调用少 1 次、工具调用少 1 次；总 token 少 1617，但本阶段成功标准不是最低
token。关键变化是：模型执行了 Contract 完全匹配的 required test，收到 Hint 后没有重复
动作，及时产生 Final Answer。

## 结论

本次真实结果支持 Phase 15 的核心假设：

```text
required test 成功 + Completion Guidance / Hint
→ 模型在完成后及时停止
→ artifact_passed=true
→ interaction_completed=true
→ accepted=true
```

与 Phase 14 相比，本次没有撞 `MAX_AGENT_STEPS`，产生了 Final Answer，独立 Verifier
最终接受任务。Phase 15.5 baseline 验证完成；随后进入 Phase 16。

---

# Phase 16：Patch-based Editing 真实验证

## 1. 目标与 baseline

本阶段在 `428ba5a Phase 15.5: record real completion comparison` 之上增加唯一精确匹配的
`apply_patch(path, old_text, new_text)`，不删除 `write_file`，也不实现完整 Git patch parser。

Phase 15.5 baseline：

| 指标 | Phase 15.5 |
|---|---:|
| model calls | 7 |
| tool calls | 7 |
| executed tools | 5 |
| `write_file` | 1 |
| `run_command` | 1 |
| duplicate blocked | 2 |
| prompt / completion / total | 12982 / 613 / 13595 |
| Verifier accepted | `true` |

## 2. 本地实现与测试

修改文件：

- `tools.py`：新增 `APPLY_PATCH_TOOL`、`apply_patch()`，注册为 `SIDE_EFFECT`。
- `main.py`：公开第五个工具；Trace 增加 `apply_patch_calls`、`patch_successes`、
  `patch_failures`；参数摘要记录 old/new 长度，不把完整 patch 内容塞进 Trace。
- `tests/test_sandbox.py`：Registry 风险期望加入 `apply_patch`。
- `tests/test_apply_patch.py`：新增 20 项 Phase 16 测试。
- `README.md`、`HANDOFF_TO_CODEX.md`：补充接口、边界、验证和真实结果。

接口语义：

```text
apply_patch(path, old_text, new_text)
```

`old_text` 必须是非空字符串并且在文件中恰好出现一次：0 次返回
`目标文本不存在`，超过 1 次返回 `目标文本不唯一，请提供更多上下文`；两种失败都在
真正写盘前结束，文件保持不变。`new_text` 可以为空。匹配不做 fuzzy、正则、AST 或
自动空白修正；仅对 LF/CRLF 做跨平台换行归一化，并按原文件风格写回，避免整文件换行漂移。

Tool Result 成功时至少包含 path、`replaced occurrence count = 1`、old/new 字符长度；
失败通过普通 `role="tool"` 返回。Permission/Sandbox 链保持为：

```text
Tool Registry → resolve_inside_workspace → Permission / Approval → apply_patch handler
```

没有增加 `if tool_name == "apply_patch"` 特判。Duplicate guard 使用完整
`path + old_text + new_text` 指纹；成功 patch 再次调用被拦截，失败和 DENY 不进入成功集合。
Acceptance 仍只依据最终 changed_files 与固定测试。

`python -m unittest discover -s tests -v`：**88 项通过**。

## 3. Mock A-D

| Case | 结果 |
|---|---|
| A：一次 patch 成功 → required test → Final | 通过；`patch_successes=1`，Acceptance accepted |
| B：old_text 多次匹配 → 提供更多上下文 → patch | 首次失败且文件不变，第二次成功；Runtime 不猜位置 |
| C：目标内容已变化 → not found → 重新读取后 patch | 首次失败是正常 Tool Result，失败调用可重试 |
| D：用户 DENY | 文件完全不变；合法拒绝结果回喂模型 |

## 4. 真实 Coding Task

任务仍为：`修复 calculator.py，让对应测试通过。完成后告诉我改了什么。`

隔离运行目录：

```text
C:\Users\30858\AppData\Local\Temp\mini-agent-phase16-real-f5a5a063059646f0a11c63f1d8ef9086
```

Provider 请求前先按交接环境检查发现 `127.0.0.1:7897` 无监听；第一次尝试停在 SDK 初始化，
0 model calls / 0 tool calls，fixture 未改变。随后发现交接日志记录的 `127.0.0.1:9674`
正在监听，在 9674 HTTP/HTTPS 代理下补齐当前虚拟环境缺失的 `socksio` 后，唯一一次真正
进入模型的 Coding Task 成功完成。该环境补依赖未写入仓库 requirements。

真实工具链：

```text
list_files → read_file(calculator.py) → read_file(test_calculator.py)
→ apply_patch(calculator.py) → run_command(python -m unittest test_calculator -q)
→ Final Answer
```

| 指标 | Phase 16 真实结果 |
|---|---:|
| model calls | 5 |
| tool calls | 5 |
| executed tools | 5 |
| `write_file` | 0 |
| `apply_patch` | 1 |
| patch success / failure | 1 / 0 |
| `run_command` | 1 |
| required test exit code | 0 |
| prompt / completion / total | 10322 / 397 / 10719 |
| max steps reached | `false` |
| Final Answer | 有 |
| `changed_files` | `["calculator.py"]` |
| `unexpected_changes` | `[]` |
| `artifact_passed` | `true` |
| `interaction_completed` | `true` |
| Verifier `accepted` | `true` |

模型自然选择了 `apply_patch`，没有被 Prompt 强制。Patch 的 old/new 均为 70 字符，
精确替换了两个错误运算符组成的唯一多行片段；之后执行 required test，收到 Completion Hint，
直接给出 Final Answer。

## 5. 与整文件 write 的观察性差异

相对 Phase 15.5：model calls `7 → 5`，tool calls `7 → 5`，duplicate blocked `2 → 0`，
prompt tokens `12982 → 10322`，completion `613 → 397`，total `13595 → 10719`；编辑工具
从 `write_file=1` 变成 `apply_patch=1`，两次都被 Verifier 接受。

这只是一次真实样本，不具有统计意义。由于 calculator fixture 很小，patch 的 old/new 两段
合计字符数（140）并不比完整文件（71）更短；本阶段证明的是局部修改能力、唯一匹配安全规则、
Permission/Sandbox/Contract 解耦和模型可以自然使用 patch。对真实大文件的 token/上下文收益
仍应在后续用更大 fixture 测量。

## 6. 结论与下一步

Phase 16 完成标准全部满足：局部修改可用；0/多匹配不会瞎改；Permission、Sandbox、Session、
Duplicate、Coding Contract、Verifier 和 Completion Control 均无回归；Mock A-D 和一次真实
`patch → test → Final` 链均通过。

下一阶段最值得补的是基于较大真实文件的 patch 参数/上下文成本测量，再决定是否需要独立的
Patch Context Compression 或模型工具偏好实验；本阶段不实现下一阶段。

---

## Phase 17 真实运行：Repository Navigation / Code Search

### 运行目标

本阶段新增多目录 fixture：`tests/fixtures/repo_fixture/`。目标文件是 `src/pricing.py`，其中
`calculate_discount` 把折扣率当成了最终乘数；测试位于 `tests/test_pricing.py`。Contract 的
`allowed_paths` 只有 `src/pricing.py`，required test 是：

```text
python -m unittest discover -s tests -p test_pricing.py -q
```

给模型的 instruction 只有“修复 calculate_discount 的错误，让对应测试通过”，没有告诉它目录和文件名。

### 有效运行结果

隔离 workspace 已正确复制，运行入口是：

```text
AGENT_WORKSPACE=<isolated repo_fixture>
TOOL_APPROVAL_MODE=ALLOW
.venv\Scripts\python.exe cli.py --contract tests\fixtures\repo_contract.json
```

先检查到 Windows 用户代理和 `HTTP_PROXY` / `HTTPS_PROXY` 均为纯值 `http://127.0.0.1:9674`，
`ALL_PROXY` 未设置，WinHTTP 为直连。加载 `.env` 后只确认 `OPENAI_API_KEY` 存在，没有打印值。
随后发送不带 Tool 的最小请求“回复 OK”，得到 `finish_reason=stop`、content=`OK`，tokens 为
`84 / 168 / 252`（prompt / completion / total）。

真实 Coding Task 也成功完成。模型没有调用 `search_text`，而是使用 `list_files` 导航，因此本次真实记录是：

| 指标 | 结果 |
|---|---:|
| model calls | 6 |
| tool calls | 8 |
| search_text calls | 0 |
| list_files calls | 4 |
| read_file calls | 2 |
| apply_patch calls | 1 |
| run_command calls | 1 |
| write_file calls | 0 |
| prompt/completion/total tokens | 14739 / 499 / 15238 |
| max steps reached | false |
| Final Answer | 有 |
| changed_files | ["src/pricing.py"] |
| unexpected_changes | [] |
| final_test_exit_code | 0 |
| artifact_passed | true |
| interaction_completed | true |
| accepted | true |

由于模型未使用 `search_text`，真实 search query 和 search result 均为“无”。实际定位候选来自：
顶层 `list_files` → `src` / `tests` → `src/utils`；随后读取 `src/pricing.py` 和
`tests/test_pricing.py`。完整 Tool Trace、Final Answer 和 Verifier JSON 证据：
`eval/phase17_real_run.json`。

### 本地闭环与回归

Mock 已证明完整链路：

```text
search_text → src/pricing.py → read_file → apply_patch → required test → Final → Verifier PASS
```

全量本地测试为 104 项通过，1 项 Windows 符号链接能力测试跳过。旧 Phase 16 的 88 项基线行为没有
回归；真实链路为 `list_files × 4 → read_file × 2 → apply_patch → run_command → Final`，且
Verifier `accepted=true`。这证明了“不知道文件路径时可完成导航”的闭环，但不证明真实模型本次会选择
`search_text`。

### Phase 18 真实运行：Repository Navigation Eval

Phase 18 的目标是观察真实模型在 SMALL / MEDIUM / LARGE-SYNTHETIC 仓库和 symbol / error-string 任务中的
`list_files`、`search_text`、`read_file` 选择，不改变 Tool Description，不强迫 `search_text`。

评测入口为 `eval/navigation_eval.py`。它为每个场景生成独立临时 fixture：7、25、75 个文本源码文件；
symbol target 和 error-string target 由 fixture ground truth 固定，Coding contract 只允许修改 symbol 文件。
指标从历史消息确定性回放：`model_calls`、总 tool calls、三类导航工具次数、candidate files、
`first_correct_file_turn`、导航前工具次数和 prompt/completion/total tokens；Coding 场景额外记录 patch、test、
Final Answer 和 Acceptance。

四个场景各执行一次，结果如下：

| scenario | status | model/tool calls | list/search/read | tokens |
|---|---|---:|---:|---:|
| small_symbol_navigation | accepted | 3 / 2 | 0 / 1 / 1 | 6,960 |
| medium_symbol_coding | accepted | 6 / 7 | 3 / 1 / 2 | 14,757 |
| large_symbol_coding | accepted | 8 / 9 | 3 / 1 / 2 | 21,028 |
| medium_error_string_navigation | accepted | 2 / 1 | 0 / 1 / 0 | 4,296 |

有效运行的工具链为：SMALL symbol `search_text → read_file → Final`；MEDIUM symbol coding
`list_files ×3 → read_file → search_text → read_file → apply_patch → Final`；LARGE-SYNTHETIC symbol coding
`list_files ×3 → search_text → read_file ×2 → apply_patch → run_command ×2 → Final`；MEDIUM error-string
`search_text → Final`。四个场景均 `accepted=true`。Medium coding 没有执行 contract 指定的 required test，
Large coding 第一次测试失败后第二次通过；独立 Verifier 两者都接受。原始结构化结果是
`eval/navigation_results.json`，完整分析是 `eval/NAVIGATION_REPORT.md`。

第一次使用仓库指令中的 `127.0.0.1:7897` 时端口没有监听，未进入 Agent loop；随后使用 Phase 17 已验证的
`127.0.0.1:9674` relay 完成四个有效场景。没有把传输配置失败混入模型行为数据，也没有修改
`search_text`、Prompt 或 Phase 17 核心代码。

离线验证：全量 `python -m unittest discover -s tests -p "test_*.py" -q` 为 `110` 项通过，`1` 项 Windows
符号链接测试跳过。下一阶段最值得做的是在同一 Provider 下重复 A/B/C，区分稳定偏好与单次路径差异，
再判断是否需要 Navigation Guidance。
