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
