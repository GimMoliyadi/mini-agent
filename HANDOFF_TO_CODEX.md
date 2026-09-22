# mini-agent-lab 交接文档 → Codex

**写于**：2026-09-21　**状态**：Phase 16 已实现并完成本地与真实闭环验证（Phase 7 Context Management、Phase 8 Session Persistence、Phase 9 Long File Reading、Phase 10 Tool Permission、Phase 11 Generalized Tool Capability / Permission Policy、Phase 12 Controlled Command Execution、Phase 12.5 真实闭环、Phase 13 Bounded Coding Loop、Phase 14 Coding Task Acceptance、Phase 15 Coding Completion & Budget Control 及 Phase 16 Patch-based Editing 均已收尾）
**写给**：一个从没见过这个项目的开发 Agent（Codex）
**目的**：让你在不重新考古整个仓库的前提下，接住这个项目并往下走。

如果你只有 5 分钟，只看第 1、3、5、9、10、13 章。
如果你要动手改代码，第 3、4、5、14 章是硬材料。

---

## 1. 项目目标

这是一个**学习项目**，不是产品。

它的存在意义只有一条：让一个开发者亲手经历 Agent Runtime 的每一个环节，
而不是套一个框架然后只调参数。所以本项目**刻意不用**任何框架——
没有 LangChain、没有 LangGraph、没有 MCP、没有 RAG、没有 Memory、
没有数据库、没有多 Agent、没有 Docker。唯一第三方依赖是 `openai`。

**不要给这个项目堆功能。** 每加一层都要能说清楚"它教会了我什么"，
否则就是偏离目标。Phase 7 的价值在于理解上下文成本从哪来，Phase 8 的价值在于理解同一段 canonical 对话如何稳定保存与恢复，Phase 9 的价值在于理解长文件读取的分页边界与上下文可见范围，Phase 10 的价值在于理解模型意图与 Runtime 执行权限必须分离。

核心学习路线（这是项目的脊柱，改动前先对照）：

```
LLM → Tool Calling → Tool Execution → Tool Result → Agent Loop
    → Multi-Tool → Completion Control → Eval → Context Management → Session Persistence
    → Long File Reading → Tool Permission / Side-effect Approval
    → Generalized Tool Capability / Permission Policy
    → Controlled Command Execution
    → Bounded Coding Loop
    → Patch-based Editing
```

当前已完成 Phase 16 Runtime。本次 Phase 16 真实验证中，模型主动完成了隔离 calculator fixture 的
读取、修改、测试和最终回答；
此前 Phase 12.5 真实验证中，模型主动调用 `run_command`，
执行 Phase 9 长文件测试并通过 10 项；随后修复 `cli.py` 的 Windows GBK Unicode 输出，
并通过 ASCII、中文、emoji 及中文+emoji+JSON 回归测试，真实闭环正式关闭。本项目暂不
自动进入 unrestricted shell、Memory、RAG、MCP 或其它后续能力。

---

## 2. 当前项目状态

Phase 0 到 Phase 13 全部完成并按阶段验证。每个阶段的三段式说明：
**新增了什么 / 为什么新增 / 最重要的结论。**

### Phase 0 — 项目骨架

新增：`main.py`（只做启动打印 + `demo_workspace/` 自检）、`config.py`、`demo_workspace/` 及三份种子文件、`.gitignore`。
为什么：先立起"程序能跑 + 沙盒目录存在"这两件事，后面所有东西挂在这个骨架上。
结论：故意**没加** `sys.stdout.reconfigure()`，因为强改 stdout 编码可能让用户的 GBK 终端反而乱码；环境编码问题交给外部环境变量解决。

### Phase 1 — LLM CLI 对话

新增：`.env` 读取（自研标准库解析器，不用 python-dotenv）、`build_client()`、`ask()`、交互式聊天循环。
为什么：先证明"能问能答"，确认 OpenAI 兼容协议的选型成立。
结论：`openai` SDK 自动读 `OPENAI_API_KEY` + `OPENAI_BASE_URL`，**换服务商只改 `.env`，一行代码都不用动**——这是选这个协议的核心理由。

### Phase 2 — Tool Schema / 模型决定调用工具

新增：`read_file` 的 JSON Schema 声明 + 沙盒校验 `resolve_inside_workspace()`（**故意提前**，见下）。
为什么：让模型自己决定读哪个文件，而不是脚本硬编码。
结论：沙盒边界本该在 Phase 5 做，实际在 Phase 2 就做掉了。理由写在 README 末尾：`read_file` 是第一个吃路径的工具，
若不带边界，Phase 3 一执行就能把 `.env` 里的 API Key 读进模型上下文——那是"先按构造引入泄密能力，再打算以后补"。
**边界必须和第一个吃路径的工具同时出生。**

### Phase 3 — 真正执行 Tool + Tool Result 回喂

新增：`TOOL_HANDLERS`（工具名→函数表）、`execute_tool_call()`、`parse_tool_arguments()`、
`assistant_tool_call_message()` / `tool_result_message()` 两个协议构造函数。
为什么：模型提出调用只是"说了句话"，必须有人真的去执行并把结果喂回去。
结论：**这是协议最脆弱的地方。** `arguments` 是 JSON **字符串**，必须原样保留在历史里
（重新序列化可能改变字段顺序和转义，导致服务商 400）。失败清理必须用 `del messages[position:]`，
用 `pop()` 会留下孤立的 assistant tool_calls，下一轮请求直接协议非法。

### Phase 4 — Agent Loop

新增：`MAX_AGENT_STEPS = 8`、`run_agent_loop()`（唯一循环点）、`finalize()`；`run_tool_round` 去掉 `ask`。
为什么：一次调用不够，一个任务需要连续多步。
结论：三个关键设计决定——
(1) 步数上限检查放在**执行之前**，命中上限时模型最后那条 tool_call 请求**故意不进历史**，
所以下一轮的历史始终是协议合法的；
(2) `first_reply` 从 `main` 传进来，让第一次 `ask` 仍在 `main` 的 `try` 块内，失败回滚 `del messages[position:]` 覆盖全程；
(3) 它数的是**问了几次模型**不是**执行了几次工具**，所以最后一问必须留给收口。

### Phase 5 — Multi-Tool Agent

新增：`list_files` / `write_file` 两个工具、`MAX_TOOL_RESULT_CHARS = 4000` 结果长度保护。
为什么：只有读没有写，Agent 只能"看"不能"做"。
结论：**"加工具不动循环"是这个项目的核心工程结论。** 加一个工具 = 一份 `*_TOOL` 说明书
+ `AVAILABLE_TOOLS` 加一项 + `TOOL_HANDLERS` 加一行，`main.py` 里连一个工具名都没有。
分层也在这里定型：`tools.py` 只抛原生异常、不知道模型；`main.py` 的 `execute_tool_call` 把所有工具结果统一收口，
所以结果截断做在那一层，新旧工具自动都有保护——**工具本身不需要知道模型的上下文预算。**
另外 `write_file` **允许覆盖**已有文本文件（沙盒已锁死破坏范围；拒绝覆盖会让"重跑同一任务"直接失败，
还得再引入 `force` 参数让模型学怎么绕过，比覆盖本身更复杂）。

### Phase 5.5 — 真实 LLM 验证

新增：**什么都没加。** 只把 Phase 5 指向真实模型（`sensenova-6.8-flash-lite` @ `https://token.sensenova.cn/v1`），
跑和 mock 一模一样的任务。
为什么：mock 是自己写的，跑通不代表真模型跑通。
结论：**任务实质 3 步就完成，模型走了 8 步，撞 `MAX_AGENT_STEPS`，没有 Final Answer。**
第 4~7 步全是纯冗余：重列目录、读回自己刚写的产物、改写一遍、**再写成逐字节相同的内容**。
归因：**模型行为，不是代码缺陷。一行代码都没改。** 这一条归因很重要，后面 Phase 6 和 Phase 6.5 的失败分类都建立在它上面。

### Phase 6 — Completion / Loop Control / Observability

新增：不新增 Agent 能力、不加工具、不动循环，只在同一个循环上叠**三层互相独立的防线**，外加可观测性。
为什么：Phase 5.5 暴露了"做完了不知道自己该停"，这是 Runtime 层面的问题。
结论：三层是——
(1) 模型自己判断：系统提示词加一句收敛原则（每次拿到工具结果后判断目标是否已满足，满足就直接 Final Answer）；
(2) 程序侧重复调用检测：同一工具名 + 规范化后完全相同的参数，且上一次**成功**执行过 → 不真执行，回喂一条重复提示；
(3) `MAX_AGENT_STEPS` 保留为最后保险丝。
第 1 层是主力，第 2 层只提醒一次，**绝不**在 Python 里强制 Final Answer——是否结束仍由模型决定。
可观测性：`ask()` 原来只返回 `.message`，把 `finish_reason` 和 `usage` 丢了；改成返回 `ModelReply` dataclass。
**复测：同一任务从 8 步无收口变成 5 步正常 Final Answer，`finish_reason` 链 `tool_calls×4 → stop`。**

### Phase 6.5 — Lightweight Agent Eval

新增：整个 `eval/` 目录（`tasks.json` 8 个任务 / `run_task.py` 单任务执行器 / `run_eval.py` 总控 /
`test_metrics.py` 19 个单测 / `results.json` / `REPORT.md`）。
为什么：Phase 6 只跑过一次（n=1），"变好了"可能是运气。得有一把尺子。
结论：**结果稳定，过程不稳定。** 8/8 成功，但同一个任务跑三次 token 差 **2.20 倍**。
`main.py` 一个字都没改——Eval 只**驱动**它，不复写它的逻辑，所以测出来的就是终端里手打时的行为。

### Phase 9 — Long File Reading

新增：扩展现有 `read_file(path, start_line=1, max_lines=100)`，按完整行返回实际能放入安全预算的连续片段，
并返回实际范围、总行数、`has_more`、`next_start_line`。保留 `MAX_TOOL_RESULT_CHARS = 4000` 全局最终兜底，
单行超过安全预算时显式返回工具错误。
为什么：此前 `read_file` 的逻辑范围可能大于最终进入模型 Context 的文本范围，造成 metadata 与正文不一致，
模型会误以为已经看完一个 `has_more=false` 的范围。
结论：Python 不自动循环；模型自主发出后续 Tool Call。旧问题范围已回归验证，mock 分段读取通过，
一次真实模型验证实际读取 `1-100 → 101-200 → 201-300 → 301-400 → 401-450`，在第 377 行找到目标并正常收口。

真实验证明细见 `REAL_RUN_LOG.md` 的 Phase 9 小节。

### Phase 10 — Tool Permission / Side-effect Approval

新增：`TOOL_PERMISSIONS` 风险分类、`ASK` / `ALLOW` / `DENY` 三种审批策略、可注入的
`ApprovalCallback`，以及 Runtime 中的 `check_tool_permission()`。
为什么：模型产生 `write_file` Tool Call 只代表它提出了动作，不应同时拥有直接修改工作区的执行权限。
结论：`list_files` / `read_file` 自动执行；`write_file` 先做 Sandbox 预检，再向 callback 请求
`CREATE` / `OVERWRITE` 审批。批准才调用 handler，拒绝返回合法 `role="tool"` 结果，且不计入成功
重复调用集合。拒绝结果也保留在 canonical Session 历史里；`WRITE_ONLY` 不会把它误压缩成成功写入。
非交互测试与 Eval 显式使用 `ALLOW` / `DENY`，交互式 CLI 默认 `ASK`。

### Phase 11 — Generalized Tool Capability / Permission Policy

稳定基线：`f08f2cd Phase 10: add tool permission and write approval`。

新增 `ToolDefinition(name, schema, handler, risk_level)` 和统一的 `TOOL_REGISTRY`。
正式的 `AVAILABLE_TOOLS` 由 Registry 派生，只把 Schema 发给模型；Runtime 执行时从
同一注册项取 Handler，Permission Runtime 从同一注册项取 `risk_level`。正式工具的
metadata 为：`list_files=READ_ONLY`、`read_file=READ_ONLY`、`write_file=SIDE_EFFECT`。
`run_command=EXECUTION` 现在复用同一套风险 metadata；没有开放 unrestricted shell，也
没有新增 MCP。

审批决策不再依赖任何具体工具名：`READ_ONLY` 自动放行，其他当前需要审批的风险
走原有 `ASK` / `ALLOW` / `DENY` callback。带路径的副作用工具仍先经过
`resolve_inside_workspace`，所以审批不能突破 Sandbox。测试临时注册的
`mock_side_effect` 已证明一个不叫 `write_file` 的工具也会进入同一审批路径，且不会
进入正式 `AVAILABLE_TOOLS`。

本阶段没有修改 `call_fingerprint` 的输入，仍只有工具名和规范化参数；拒绝结果不进
成功重复调用集合。Session 仍只保存 canonical messages，Context 的 `WRITE_ONLY`
行为、Long File 分页和 Tool Call 协议配对均保持不变。

### Phase 12 — Controlled Command Execution

`run_command(command: str, args: list[str] = [], cwd: str = ".")` 已加入正式
`TOOL_REGISTRY`，风险等级为 `EXECUTION`。它使用 `subprocess.run` 且明确传入
`shell=False`，不接受完整 shell script。

Command Policy 只允许 `python -m pytest ...`、`python -m unittest ...`、`git status`、
`git diff` 和 `git log`。`python -c`、`python -m pip`、写入型 Git 子命令、PowerShell、
cmd、bash、curl、wget、ssh、未知命令和 shell 操作符都会被拒绝。

`cwd` 通过 `resolve_inside_workspace` 预检，且发生在 Permission callback 之前；这项
预检通过 `ToolDefinition.workspace_arguments` 描述，不在 Runtime 里写工具名特判。
`ToolDefinition.preflight` 负责在审批前执行通用命令策略校验。

`COMMAND_TIMEOUT_SECONDS` 默认 30 秒，`MAX_COMMAND_OUTPUT_CHARS` 默认 1500。结果包含
Command、CWD、Exit code、Timed out、STDOUT 和 STDERR；超时会终止 subprocess 并返回
合法 Tool Result，非零 exit code 也只作为结果交给模型。stdout/stderr 各自截断并明确
标记，敏感环境值和 `.env` 相关输出不会回传。

拒绝路径经过现有 `ASK` / `ALLOW` / `DENY` callback；DENY 不启动 subprocess，仍保留
合法 `role="tool"` 历史记录，也不会进入成功重复调用集合。

### Phase 13 — Bounded Coding Loop

新增：`CodingTaskTrace`、`counts_as_successful_duplicate()`、隔离 fixture
`tests/fixtures/coding_workspace/`、`tests/test_coding_loop.py`，以及 CLI 结果中的任务 Trace。

为什么：Phase 12 已经有文件读写、受控测试命令和权限边界，但还没有证明它们能自然串成一次
小型 Coding Task。这里不加 Planner 或 Coding 专用状态机，只让普通 Agent Loop 处理测试结果。

关键语义：`run_command` 的非零退出码表示测试失败，不是 Runtime 崩溃；它会作为正常
`role="tool"` 结果继续进入 messages。非零命令不进入成功重复调用集合，因此模型写入修复后
可以再次运行同一条测试命令。`write_file` / `run_command` 仍分别经过 `SIDE_EFFECT` /
`EXECUTION` Permission，Sandbox 和 `MAX_AGENT_STEPS` 均未绕过。

确定性验证：Mock A 的链路为 `read → write → test(0) → Final`；Mock B 为
`read → write v1 → test(1) → write v2 → test(0) → Final`；Mock C 反复请求工具并在
`MAX_AGENT_STEPS` 停止。一次真实任务使用同一 fixture，模型实际走了
`list → read calculator → read tests → write → run unittest(0) → Final`。

---

## 3. 当前架构

### 文件职责

```
main.py           入口 + Agent Loop + 权限检查 + 工具执行层 + 回喂模型。全部 Runtime 逻辑在这里
config.py         配置：读 .env，产出 LLMConfig、WORKSPACE_DIR、MAX_AGENT_STEPS、
                  MAX_TOOL_RESULT_CHARS、MAX_READ_RESULT_CHARS、命令超时/输出上限、审批模式、LOCAL_NO_PROXY。
                  WORKSPACE_DIR 在这里唯一定义一次
tools.py          工具 Schema + ToolDefinition / TOOL_REGISTRY / AVAILABLE_TOOLS
                  + 沙盒校验、Command Policy、受控 subprocess handler
tests/            mock_server.py（本地假服务器，不需要 Key）
                  test_sandbox.py（沙盒边界 + 注册表一致性，27 项）
                  test_loop.py（重复调用检测 + 协议合法性，4 组）
                  test_permissions.py（Phase 10/11 审批/拒绝、Sandbox、Session、Mock Agent）
                  test_commands.py（Phase 12 Command Policy、subprocess、超时和输出边界）
                  test_long_file.py（Phase 9 分段读取、完整行和 mock 分页）
                  test_coding_loop.py（Phase 13 fixture、Mock A/B/C、失败重测和上限保护）
                  inputs/*.txt（喂给 main.py 的 stdin，用来复现某次实测）
eval/             轻量 Eval（Phase 6.5）
  tasks.json        8 个任务 + 每个任务的成功规则
  run_task.py       单任务执行器：驱动 main.py + 收集指标 + 判定 success
  run_eval.py       总控：工作目录快照隔离 + 跑全部任务 + 失败分类
  test_metrics.py   20 个单测：只测「尺子准不准」，不发任何网络请求
  results.json      最近一轮 Eval 的完整原始数据
  REPORT.md         人类可读报告
  .run_log.txt      过程日志（已 gitignore）
  .workspace_snapshot/  跑 Eval 前备份的 demo_workspace（已 gitignore，重跑前必须删）
demo_workspace/   Agent 唯一允许读写的工作目录（沙盒），当前 5 个文件
README.md         项目说明 + 每个阶段的解释
REAL_RUN_LOG.md   真模型实测记录（Phase 5.5 首轮 + Phase 6 复测 + Phase 12.5 + Phase 13）
HANDOFF_TO_CODEX.md  本文件
```

### 真实数据流

```
User
  │
  ▼
messages  ──►  ask()  ──►  LLM
                │
                └──► Tool Call  /  Final Answer
                            │
                ┌───────────┘
                ▼
        Tool Registry（TOOL_REGISTRY，AVAILABLE_TOOLS 是其 Schema 视图）
                ▼
        Permission Check（READ_ONLY 直通；其他风险先做边界/策略检查再审批）
                ▼
          Tool Handler（tools.py 里的函数）
                ▼
          Tool Result
                ▼
        messages ◄── 回喂
                │
                ▼
          Agent Loop（run_agent_loop）
                │
                └──► 再 ask() → LLM
```

### 必须记住的四条架构事实

1. **`TOOL_REGISTRY` 是工具的单一注册来源。** 每项 `ToolDefinition` 同时包含 name、
   OpenAI-compatible Schema、Python handler 和 `risk_level`；`AVAILABLE_TOOLS` 只是从
   它派生的模型可见 Schema 列表。

2. **Schema / Handler / Risk 必须来自同一项。** 这样新增工具时不会出现 Schema 注册了
   但忘记 Handler，或 Handler 存在却没有风险 metadata 的漂移。

3. **`run_agent_loop` 只知道 Tool Call 协议，不知道任何具体工具。**
   它没有 `if name == "read_file"` 这种分支，里面连一个工具名都没写。
   **Phase 7 如果要在循环里加特殊逻辑，你就是在破坏这个结论。**

4. **权限检查属于 Runtime，不属于具体 handler。** `write_file` 只负责执行写入，
   `run_command` 只负责受控执行；`run_tool_round` 在调用 handler 前先做 Sandbox/Policy
   预检和审批。模型提出 Tool Call 不等于 Runtime 已授权。

5. **Tool Result 一旦进 messages，之后每一次 `ask` 都把完整 messages 原样重发。**
   LLM 本身没有记忆，上下文全靠每次重述。**这一条是 Phase 7 的出发点，也是当前最大的技术问题。**

---

## 4. 当前四个工具

### `list_files`

| | |
|---|---|
| 参数 | `path`（string，**可选**）——省略就是列工作目录根 |
| 用途 | 列一层内容，标 `[f]`/`[d]` 前缀，文件带 `(N 字节)`，目录为空会明说 |
| 沙盒限制 | 同样走 `resolve_inside_workspace()`，越界抛 `PermissionError` |
| 失败行为 | 不是目录 → `NotADirectoryError`；不存在 → `FileNotFoundError`；都不吞异常 |
| 副作用 | **无**（只读） |

说明书里特意写了"如果你不知道有哪些文件，先用这个工具，不要猜文件名"——
这句话放在**工具描述**里而不是系统提示词里，因为前者是模型从读工具说明自然形成的用法，
后者是脚本式的指令。

### `read_file`

| | |
|---|---|
| 参数 | `path`（string，必填） |
| 用途 | 读 UTF-8 文本文件，全量返回 |
| 沙盒限制 | 同上，先 `resolve()` 再 `is_relative_to()` 校验 |
| 失败行为 | `FileNotFoundError` / `PermissionError` / `UnicodeDecodeError`，全部原生抛出 |
| 副作用 | **无**（只读） |

注意：`tools.py` 里的 handler **不吞异常、不返回错误字符串**。异常交给 `main.py` 的
`execute_tool_call` 统一转成 `"[工具失败] ..."` 文本。这个分层是刻意的。

### `write_file`

| | |
|---|---|
| 参数 | `path`（string，必填）、`content`（string，必填） |
| 用途 | 写 UTF-8 文本；父目录不存在会 `mkdir(parents=True, exist_ok=True)` |
| 沙盒限制 | **先校验路径，再 mkdir**——所以自动创建的父目录也保证在沙盒内 |
| 失败行为 | 同上，原生抛出 |
| 返回值 | `已写入 xxx.md（N 字节，已写入新文件 / 已覆盖已有文件）`，字节数用 `len(content.encode("utf-8"))` |
| 副作用 | **有**（创建目录、写入/覆盖文件） |

实现用 `open(path, "w", encoding="utf-8", newline="")` 而不是 `Path.write_text()`。
原因：Windows 上 `write_text()` 默认文本模式会把 `\n` 翻成 `\r\n`，
导致"工具报 304 字节、磁盘落 311"。真模型实测已确认修复成立（1617 = 1617）。

> **不要重新设计现有 Tool。** 现有工具的边界、参数名、覆盖策略、失败语义都已实测固化。
> `run_command` 的边界见上表；它不是 unrestricted shell。

### `apply_patch`

| | |
|---|---|
| 参数 | `path`、`old_text`、`new_text`（均为 string；`old_text` 非空） |
| 用途 | 对已有 UTF-8 文本做一次精确的 `old_text → new_text` 替换 |
| 唯一匹配 | 0 次：`目标文本不存在`；超过 1 次：`目标文本不唯一`；只有 1 次才写入 |
| 沙盒限制 | 复用 `resolve_inside_workspace`；审批前校验，不能访问 workspace 外 |
| 换行 | 匹配逻辑按统一换行处理，写回时保留原文件的 LF/CRLF 风格 |
| 返回值 | path、`replaced occurrence count = 1`、old/new 字符长度 |
| 风险 | `SIDE_EFFECT`，复用 `ASK` / `ALLOW` / `DENY` |

不做 fuzzy matching、正则、AST 猜测、自动空白修正或完整 Git patch parser。失败是正常
`role="tool"` 结果，文件保持不变；模型应重新 `read_file` 后构造更具体的 patch。
`write_file` 保留给新建文件或必要的整文件覆盖。

### `run_command`

| | |
|---|---|
| 参数 | `command`（string，必填）、`args`（string 数组，默认 `[]`）、`cwd`（string，默认 `.`） |
| 用途 | 受控执行本地 Python 测试或 Git 只读命令，使用 `subprocess` + `shell=False` |
| 允许 | `python -m pytest ...`、`python -m unittest ...`、`git status`、`git diff`、`git log` |
| 禁止 | `python -c`、pip、写入型 Git、PowerShell、cmd、bash、curl、wget、ssh、未知命令、shell 操作符 |
| 沙盒限制 | `cwd` 和 path-like 命令参数必须位于 `WORKSPACE_DIR`；检查早于 Permission callback |
| 返回值 | `Command`、`CWD`、`Exit code`、`Timed out`、`STDOUT`、`STDERR` |
| 运行保护 | 默认 30 秒超时；stdout/stderr 各自最多 1500 字符，超出明确标记截断 |
| 风险 | `EXECUTION`，复用 `ASK` / `ALLOW` / `DENY`；DENY 不启动进程 |

---

## 5. 安全边界

**这部分是硬约束。** 违反任何一条都要停下来问，不要自己判断"这个应该没关系"。

1. **`WORKSPACE_DIR` 是唯一的沙盒根。** 只在 `config.py` 里定义一次。
   Agent 没有任何机制读写这个目录之外的任何东西。

2. **路径必须先 `resolve()` 再 `is_relative_to()` 校验。**
   只做字符串前缀检查会漏掉三种情况：
   - `../` 跳级
   - 符号链接指向沙盒外
   - 模型直接给绝对路径（`C:/evil.txt`、`/etc/passwd`）
   越界一律抛 `PermissionError`，**绝不静默降级成一个沙盒内的路径**。

3. **`write_file` 必须在 `mkdir(parents=True)` 之前校验路径。**
   反过来的话，模型可以用"写一个深路径"把沙盒外的父目录造出来。

4. **`.env` 永不进 Git。** `.gitignore` 已配置。

5. **API Key 绝对不许打印。** 日志里不许出现 API Key、Authorization 头、任何完整 HTTP 头。
   本文件的任何地方都没有 Key，也不要把它写进任何文档、测试输出或记忆文件。

其他已固化的事实：没有删除文件的能力，没有执行程序的入口，
所有工具结果统一在 `main.py` 的 `execute_tool_call` 出口截断到 4000 字符
（超限要明说"已截断、原始多少字符、省略多少"，不静默丢——静默截断会让模型以为拿到全文）。

---

## 6. Phase 5.5 真模型暴露的问题

第一次把项目指向真实模型（同一任务，之前 mock 是 4 次问模型 / 3 轮工具 / 正常收口），结果：

- **8 次问模型、7 次工具执行、撞上 `MAX_AGENT_STEPS`**
- **没有 Final Answer**
- 任务实质在第 3 步就完成了（`list_files` → `read_file agent_notes.md` → `write_file` 1616 字节）
- 第 4 步：又 `list_files` 了一遍（只多了个 `[d] notes`，无新信息）
- 第 5 步：`read_file` **读回自己刚写的那个摘要**
- 第 6 步：`write_file` 改写一遍（1533 字节）
- 第 7 步：`write_file` 同一路径，**内容与第 6 步逐字节相同**（只是 JSON 键序把 `"path"` 和 `"content"` 换了个先后）
- 第 8 步：模型仍要求调用工具 → 命中步数上限，停止

同时**没发生**的事（这些都要单独确认过）：
零编造文件名（第一个动作就是 `list_files`）、零沙盒突破、零协议 400、零静默覆盖、结果截断未触发。

### 这个结果证明了什么

**证明了 Runtime 能跑。** 沙盒拦截、覆盖策略、长度保护、协议清理、步数上限、
上限处的干净退出，全部按设计工作。

**证明不了的是模型不知道收口。** 模型的失败模式是"写完之后不知道该收手"：
`write_file` 已经回报了成功和字节数，它却继续重列目录、读回自己的产物、再写两遍。

归因结论：**模型行为，不是代码缺陷。一行代码都没改。**

这一条归因是后面所有工作的地基。Phase 6 的三层防线是为它而建的；
Phase 6.5 的失败分类里 `max_steps_hit` 被归为 **Model Behavior** 而不是 Runtime，也是因为它。

---

## 7. Phase 6 的解决方案

三层防线，**互相独立**——任何一层失效，另外两层仍能兜住。

| 层 | 谁负责 | 机制 |
|---|---|---|
| 1. 自己判断 | 模型 | 系统提示词加一句收敛原则：每次拿到工具结果后先判断用户明确要求的目标是否已经满足，满足就直接给最终回答；不要为"再确认一下"反复调用工具；需要验证有副作用的操作可以验证，但验证成功后要收口 |
| 2. 重复调用拦截 | Runtime | 同一 **Tool Name** + 规范化后**完全相同的参数**，且上一次**成功**执行过 → 不真执行，回喂一条重复提示 |
| 3. 最大步数 | Runtime | `MAX_AGENT_STEPS = 8`，最后保险丝，撞线就停并明确告知 |

### 第 2 层的实现细节（这些是"为什么"，代码里看不出）

- 指纹 = `json.loads(arguments)` 后 `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",",":"))`。
  所以**键序换了、多了空格算同一个**——Phase 5.5 那两次 write_file 就是键序不同、内容逐字节相同，
  字符串直比会漏。
- 参数不是合法 JSON 时，`JSONDecodeError` 退回原始字符串**并且不拦截**——拿不准宁可放行。
- **失败过的调用不进检测表**（以 `result.startswith("[工具失败]")` 为门）。
  否则模型第一次读错文件名后换参数重试会被当重复拦死，永远过不去。
- **拦下后仍按正常协议回喂一条 tool result**（内容是 `DUPLICATE_NOTICE`），让模型继续下一轮。
  **绝不**在 Python 里强制 Final Answer——是否结束仍然由模型决定。
- 指纹表是**每个任务一份**（`executed` 在 `main` 里随任务创建、任务结束即弃）。
  跨任务不清的话，上一轮的正常调用会被这一轮误判成重复。
- 回放顺序必须和 `main` 一致：先看指纹在不在集合里，**再**更新集合。

### 可观测性

`ask()` 原来只返回 `response.choices[0].message`，把 `finish_reason` 和 `usage` 都丢了，
导致 Phase 5.5 那轮根本报告不出每步的 `finish_reason`。
改成返回 `ModelReply(message, finish_reason, prompt_tokens, completion_tokens, total_tokens)`，
数值字段类型是 `int | None`，全用 `getattr(..., None)` 取，`None` 打印成 `unavailable`。
**不为这件事做大规模重构。**

### Phase 6 复测结果

同一任务（只有输出路径改成 `notes/agent_summary_real_v2.md`）：

| | Phase 5.5 首轮 | Phase 6 复测 |
|---|---|---|
| 问模型次数 | 8 | **5** |
| 真实工具执行 | 7 | **5** |
| `write_file` 次数 | 3（第 2、3 次内容逐字节相同） | **1** |
| 读回自己的产物 | 1 次 | **0 次** |
| 重列目录 | 1 次（无新信息） | **0 次** |
| 撞 `MAX_AGENT_STEPS` | 是（第 8 步） | **否（5 / 8）** |
| Final Answer | **无** | **有** |
| `finish_reason` | 无法报告 | `tool_calls×4 → stop` |

token 合计 prompt 7288 / completion 884 / total 8172，服务商每次请求都返回 `usage`，无一项缺失。

**诚实的 caveat：这是单次运行（n=1）的对比，没有对照组。** 只能说"这次明显更好、机制上对症"，
不能说"必然每次都这样"。防再犯靠的是三层防线同时存在。Phase 6.5 就是为这个 n=1 问题而建的。

### ⚠️ 一条警告

**不要把正常的「write 后 read 验证」误判成错误。**

这是刻意的设计选择，写在 README 里：
不写死"写完文件以后禁止 read_file"（内容变了就是模型在改，得放行），
也不写死"write_file 成功以后立刻结束"（那样验证写入成功就被禁止了）。
所以拦下来的只有"同一个工具、完全相同的参数、上一次已经成功"这一种情况。

---

## 8. Phase 6.5 Eval 结果

数据来源：`eval/REPORT.md`（人类可读）+ `eval/results.json`（完整原始数据）。
**下面每个数字都可以在这两个文件里核对到。**

### 一句话结论

**结果相对稳定，但过程和成本不稳定。**

### 核心数据

| 指标 | 数值 |
|---|---|
| 总任务数 | 8（6 个基准任务 + task_1 重复 2 次） |
| 成功 / 失败 | **8 / 0（100%）** |
| 有 Final Answer | **8 / 8** |
| 撞 `MAX_AGENT_STEPS` | **0 次**（最多用到第 6 轮，上限 8） |
| 编造文件 | **0 次** |
| 工具调用 | 共 **31 次**，其中失败 1 次（task_4 故意读不存在的文件）、被拦 0 次、被丢弃 0 次 |
| Provider/网络失败 | 0 次 |
| 总模型调用 | 29 次，**平均 3.62 次/任务** |
| 总 token | **46,011**，平均 **5,751/任务** |
| prompt / completion | **40,816（88.71%）** / 5,195（11.29%） |
| 需要人工评审 | **7 / 8**（只有 task_5 写指定字符串能 100% 机器验证） |

### 过程不稳定的证据（本轮最关键的数据）

**task_1 的任务文本在三个任务里完全一致**，三次运行：

| | task_1 | task_1_b | task_1_c |
|---|---|---|---|
| 模型调用 | 5 | 4 | 6 |
| 工具调用数 | 6 | 4 | 7 |
| total_tokens | 9,172 | **5,884（最省）** | **12,962（最贵）** |
| prompt_tokens | 8,260 | 5,030 | 11,483 |
| 读了哪些文件 | agent_notes.md + notes/ 下两份旧摘要 | **只读 agent_notes.md** | agent_notes.md + notes/ 下两份旧摘要 |
| 是否重复写文件 | 否 | 否 | **是（连写两次同一文件）** |

**均值 9,339 token，标准差 2,892，变异系数 31.0%，最大/最小比 2.20 倍。**

差异不是"多花点钱"，是**不同的解题策略**：
1. **文件选择变了**——task_1_b 判断 `python_notes.md` 和 Agent 无关就跳过，只读一份就写。
2. **task_1_c 写完又改了一遍**——它发现原笔记写的 `demo_workspace/` 在当前工作目录里不存在，
   想在摘要里加一句注释。**两次 write 参数不同 → 不算重复、没被拦截，这是正确行为**，
   但代价是一整轮往返 + 3,254 prompt token。
3. 模型调用数在 4～6 浮动，变异系数 14%。

三次都成功了，三份摘要都真实基于 `agent_notes.md`，零编造。**结果对，路径不同。**

### 上下文增长（只记录，不解决）

全部 8 次运行，**每轮 `prompt_tokens` 严格单调递增，无一例外。**

| 任务 | 每轮 prompt_tokens | 增量来源 |
|---|---|---|
| task_1 | 859 → 953 → 1390 → 2294 → 2764 | +94 → +437（读 agent_notes.md）→ +904（读旧摘要）→ +470 |
| task_6 | 831 → 914 → 1825 → 2729 | +83 → **+911**（读 python_notes.md）→ +904（读两份旧摘要） |
| task_4 | 833 → 926 → 1009 → 1090 | +93 → +83 → +81 |
| task_5 | 843 → 923 | +80 |

三个规律：
1. **第 1 轮是固定底数**：8 次运行的第一轮是 **831～859**（波动仅 1.7%）。
   这部分是 SYSTEM_PROMPT + 工具 JSON Schema + 任务文本，跟任务难度无关。
2. **第 2 轮跳得小**：80～104，只是加上"模型那句工具调用" + "一个小的工具结果"。
3. **跳得大的一轮都是读了完整文件的轮次**：读 `agent_notes.md` 约 +437，读 `python_notes.md` 约 +911。
   **token 的增量几乎等于被读文件的内容体积。**

机制：Agent 的方式就是"工具结果回喂模型"，历史写进去之后，
**之后每一次请求都要把整条历史原样重发一遍**。读一个大文件不只那一次贵，
它让**后面每一轮永久变贵**。

### 规则看不到什么（这条必须交代）

100% 成功是个**弱结论**。7/8 任务标了 `needs_manual_review`，因为
"摘要写得好不好""比较说得对不对"这类主观指标一律不给分。

人工看过的最典型一例：**task_3「列目录」被规则判为通过，但它实质是不完整的**——
模型列了根目录文件、还说了 `notes/` 是个目录，但**从来没有列过 `notes/` 里面的内容**。
规则（`require_tool_chain: ["list_files"]` + 没编造文件）全都满足了，任务却没做完。
这是"为什么需要语义 Eval"最直接的演示。

另外两个次要观察：
- **重复调用拦截 31 次调用 0 次触发。** Phase 5.5 那个失败模式在这 8 次里根本没出现，
  是那次运行的偶发问题，不是系统性问题。（功能本身正确，19 个单测覆盖，只是没被需要过。）
- **步数上限余量只有 2**：最多用到第 6 轮，上限 8。task_1_c 已证明稍微复杂一点就逼近上限。

### Eval 自身的三个问题（都诚实披露在 REPORT.md 里）

1. `finish_workspace` 死分支：总控在调 `finish_workspace` 之前多调了一次 `restore_snapshot`，
   把"保留 Eval 写出文件作为证据"分支变成死代码。**已修。**
   代价：这个 bug 是**跑完之后**才发现的，Agent 写出的两个文件已被清掉无法展示——
   但 `output_file_exists` 判定发生在清理之前，`results.json` 里的判定是当时真实测出来的，**不影响测量结论**。
2. `max_steps_hit` 原先被误分类为 Runtime，与 Phase 5.5 的归因矛盾。**在跑之前已修**，归为 Model Behavior。
3. `require_no_runtime_error` 这个规则名有误导性。**未改**，不影响判定。

---

## 9. 当前最大的技术问题

**首要问题不是 Tool，不是真模型的收口，不是 Memory。是 Context Growth（上下文增长）。**

理由，用 Eval 的真实数据：

- **88.71% 的钱花在 prompt** 上，completion 只占 11.29%。
- prompt 之所以占这么大比重，是因为**每次 `ask` 都重发完整历史**。
- 历史之所以持续变长，是因为**工具结果一旦加进 messages，之后的每一次请求都要把它重新发一遍**。
- 所以：读一个 1,000 字节的文件，不只那一轮贵——
  它让后面**每一轮**都永久变贵。Eval 里每轮 `prompt_tokens` 严格单调递增、无一例外，就是这个的直接证据。
- 而且这是**结构性**的，调 Prompt 解决不了：它不取决于模型聪不聪明，
  取决于"把结果回喂给模型"这个机制本身。Phase 7 越复杂，咬得越狠。
  task_1 三次运行 2.20 倍的 token 差，本质就是"这个模型决定多读了两份文件"。

对比另外三个候选问题，都不在同一个量级：

| 候选 | 为什么不是首要 |
|---|---|
| Tool 不够 | 四个工具已覆盖读/写/列/受控执行，"加工具不动循环"已验证。问题不在数量 |
| 模型不会收口 | Phase 6 的三层防线 + Eval 8 次运行 0 撞上限、0 重复拦截。已缓解，且是单点问题 |
| 没有 Memory | 任务结束历史就丢，这是真的缺失，但**当前一个任务内的成本就已经 88% 花在重发历史**，跨任务记忆是下一个层次的问题 |

> **不要解决这个问题，只把事实交接清楚。**
> 本阶段的职责是记录、量化、把证据交给下一阶段——不是顺手做个优化。
> 如果你发现自己在写压缩代码，说明你越界了。

---

## 10. Phase 7 Context Management（已完成）

本章保留 Phase 7 实施前的设计记录；Phase 7 已完成，Phase 8 也已完成。

### 实际完成情况

- Phase 7 增加 `OFF / WRITE_ONLY / FULL` 三种上下文模式，默认 `WRITE_ONLY`。
- canonical `messages` 保持完整，只有发给模型的视图由 `build_model_context()` 压缩。
- Phase 8 增加 `session.py` 和 `main.py --resume SESSION_ID`。
- Session JSON 保存 `session_id`、时间、模型、版本和 canonical `messages`，不保存 API Key。
- 保存/加载时校验 `assistant.tool_calls` 与 `tool` result 的配对；不做 Memory、RAG 或跨 Session 合并。
- Session 本地测试与既有 sandbox、loop、Eval metrics、reliability、CLI 测试均通过。

以下内容只描述当时的目标和分析问题，供理解设计取舍，不是待执行任务。

### 目标

降低 Tool Result 对历史造成的**持续性**上下文成本，
同时**尽可能少地丢失信息**。

注意目标里有两个约束是同时成立的：**既省 token，又少丢信息。**
只做前者就是靠丢信息换 token，那不是优化。

### 不要从复杂框架开始

不要一上来就设计一个完整的上下文管理系统。先调研最小可行的选项，
把下面这些写成**需要 Codex 先分析的问题**：

1. **Tool Result 的生命周期应该是什么？**
   当前所有工具结果一视同仁地永久留在历史里。哪些结果有长期价值，哪些只是"这一次看过了"？
   能不能先给工具结果分个类，而不是做统一处理？

2. **哪些结果必须长期保留？**
   想想 `write_file` 的返回值（"已写入 xxx.md（1617 字节，已写入新文件）"）
   和 `read_file` 的返回值（一整个文件内容）的区别——
   模型后续轮次真正需要哪个？写操作的**确认**是不是比读操作的**全文**更有长期价值？

3. **哪些可以压缩？**
   当前有 `MAX_TOOL_RESULT_CHARS = 4000` 这个**单次**上限。
   但截断是发生在"进入历史的那一刻"，之后它就永久以截断形态存在。
   压缩和截断是不是一件事？压缩能不能保留结构信息（"已读过这个文件、有 N 行、关键内容是什么"）而截断不能？

4. **哪些可以只留摘要或引用？**
   模型第 4 轮真的还需要第 2 轮读到的全文，还是只需要知道"我读过这个文件、结论是 X"？
   如果它后面想再看一遍，能不能**再调一次 read_file**——工具还在，重读的成本是可控的？

5. **能不能区分"当前轮"和"后续轮"的视图？**
   一个可能的方向：当前这一轮用完整结果（模型正在做决定，需要全文），
   进入下一轮之后替换成压缩版。
   这个方向的技术难点在哪？会不会破坏 `assistant tool_calls` / `tool result` 的配对协议？

6. **怎么量化"信息损失"？**
   现有 8 个任务里 7 个必须人工看。如果 Phase 7 改了上下文策略，
   **怎么知道是省了 token 还是偷偷让模型看不到关键信息了？**
   这个问题不解决，Phase 7 就没有验收标准。

### 明确不要做的事

**不要直接给出最终实现答案。** 上面每一条都应该先形成分析和方案对比，
**等用户确认**再动手。这是本项目的硬规矩（见第 11 章）。

---

## 11. 当前阶段禁止事项（Phase 13 已完成）

除非有明确理由（而且要写清楚理由、等用户确认），**不要**做以下任何一件事：

- ❌ 重写 Agent Loop（`run_agent_loop` 是这个项目最值钱的一段代码）
- ❌ 再次拆分 Tool Registry（`TOOL_REGISTRY` 已是 Schema / Handler / Risk 的单一来源）
- ❌ 换框架
- ❌ 引入 LangGraph
- ❌ 引入 Memory
- ❌ 引入 RAG
- ❌ 引入 MCP
- ❌ 把 `run_command` 扩展成 unrestricted shell；不得加入 shell=True、PowerShell、cmd、bash、网络命令或安装命令
- ❌ 把 Phase 13 的 bounded Coding Loop 扩展成无限自主编程；不得绕过 `MAX_AGENT_STEPS`
- ❌ 增加超出当前学习目标的新 Tool
- ❌ 做 GUI
- ❌ 做 Multi-Agent
- ❌ 修改沙盒安全边界（见第 5 章，硬约束）
- ❌ 为了优化 Context 破坏 Eval 基线（现有 8 个任务 + 46,011 token 的基线是 Phase 7 唯一可比的参照物；
  改掉它就没法判断改好了还是改坏了）

**理由**：这个项目的学习路线是线性的。每一步都必须能独立说明"教会了我什么"。
同时动三样东西，出了问题分不清是哪一层，也学不到任何东西。

---

## 12. 接手前必须阅读的文件（按优先级）

按这个顺序读，后面的文件在前面的语境下才有意义：

| # | 文件 | 为什么读它 |
|---|---|---|
| 1 | `HANDOFF_TO_CODEX.md` | 本文件，先看全局 |
| 2 | `README.md` | 项目自述 + 每个阶段的设计选择（注意：有几处已知不一致，见本文件末尾备注） |
| 3 | `eval/REPORT.md` | Phase 6.5 的人类可读报告，理解"结果稳定、过程不稳定"这个结论 |
| 4 | `eval/results.json` | 完整原始数据。REPORT 里每个数字都能在这里核对 |
| 5 | `REAL_RUN_LOG.md` | 真模型实测记录：Phase 5.5 首轮（失败模式）+ Phase 6 复测（修复效果） |
| 6 | `main.py` | 442 行，全部 Runtime 逻辑。Agent Loop + 工具执行层 + 重复检测都在这里 |
| 7 | `tools.py` | 工具 Schema/Registry + 沙盒校验 + Command Policy + handler |
| 8 | `config.py` | 144 行。所有常量 + `.env` 解析 |
| 9 | `eval/tasks.json` | 8 个任务 + 成功规则。Phase 7 的历史验收基线 |
| 10 | `session.py` | Phase 8 Session JSON 的创建、保存、加载和 canonical message 校验 |

读的时候建议特别看这三处，因为它们的注释里写着"为什么"：
- `main.py` 的 `run_agent_loop` —— 上限检查为什么放在执行**之前**
- `main.py` 的 `call_fingerprint` —— 为什么要规范化 JSON 而不是字符串直比
- `tools.py` 的 `resolve_inside_workspace` —— 为什么必须 `resolve()` 之后再比较

---

## 13. Codex 接手后的第一件事（历史流程，已完成）

以下是 Phase 7 接手时使用的历史流程；Phase 8 已完成，当前没有待执行的接手步骤。

1. **读第 12 章列的那些文件。**

2. **检查 `git status`。**
   历史上这里曾经没有基线 commit；现在 Phase 7 已有基线提交，Phase 8 收尾提交后以
   最新 `git status` 为准。不要把本段历史说明当成当前工作区状态。

3. **画出当前的 messages 生命周期。**
   从 `main()` 里 `messages = [{"role":"system", ...}]` 开始，到任务结束为止，
   每一条消息是谁在什么时候加进去的、什么时候被删掉的、什么条件下永久留存。
   要标清楚 `del messages[position:]` 这个回滚点的位置。

4. **定位上下文增长到底发生在哪一步。**
   具体到函数和行号。参考方向：工具结果是通过 `tool_result_message()` 加进历史的，
   然后被 `ask()` 在后续每次调用里原样重发。**"增长发生点"和"成本发生点"是两回事**，
   要分开说清楚——增长点只有一处，成本点是每一轮 `ask`。

5. **基于真实的 Eval 数据，提出 2～3 个最小可行的 Context Management 选项。**
   每个选项都要按下面四个维度对比：

   | 维度 | 要回答什么 |
   |---|---|
   | 实现复杂度 | 改几个文件、加多少行、要不要动协议 |
   | 信息损失风险 | 模型可能因此看不到什么？会不会重演 task_3 那种"实质不完整" |
   | token 节省潜力 | 用 46,011 / 88.71% 这个基线估算，量级是多少 |
   | 是否破坏现有协议 | `assistant tool_calls` 和 `tool result` 的配对、`tool_call_id` 匹配、`arguments` 原样保留 |

   **不要在这一步实现。** 只出方案对比，交给用户选。

6. **跑一遍现有测试确认基线是绿的**（这些不发网络请求）：

   ```bash
   C:\30858\mini-agent-lab\.venv\Scripts\python.exe tests\test_sandbox.py
   C:\30858\mini-agent-lab\.venv\Scripts\python.exe tests\test_loop.py
   C:\30858\mini-agent-lab\.venv\Scripts\python.exe eval\test_metrics.py
   ```

   注意：系统 `python`（3.11.9）**没有装 openai**，必须用 `.venv` 里的解释器。
   `tests/` 和 `eval/` 都不是包，脚本开头靠 `sys.path.insert(0, 项目根)` 才能 import 项目模块。

   如果要重跑 Eval 本身：先删 `eval\.workspace_snapshot\`（它现在存在，
   `prepare_snapshot` 遇到已存在且非空的快照会直接 `SystemExit(2)` 拒绝），
   而且那会花真金白银的 API 调用。

---

## 14. 已知细节 / 坑

这些都是在真跑中踩出来的，不是理论推断。改代码前过一遍。

1. **Tool Schema 的参数名必须和函数形参逐字一致。**
   `handler(**arguments)` 是按**名字**转发关键字参数的。
   实际踩过：声明写 `relative_path`、形参写 `path` → `unexpected keyword argument`。
   `tests/test_sandbox.py` 里用 `inspect.signature` 把 schema 的 `properties` 键和 handler 形参自动对一遍，
   就是为了封死这个坑。改工具时不要绕过这个测试。

2. **assistant 的 `tool_calls` 消息和 `tool` 结果消息必须成对、且顺序正确。**
   `tool_call_id` 必须匹配原始 call 的 `id`。
   服务商会直接 400。`tests/mock_server.py` 内置了一个协议校验器专门查这个。

3. **请求失败时不能只 `pop()` 最后一条消息。**
   要用 `del messages[position:]` 清掉整个半截回合。
   只 pop 一条会留下孤立的 assistant tool_calls，下一轮请求协议非法 → 服务商 400。
   `tests/mock_server.py` 的 `中途崩` 触发词就是为了验这个。

4. **Windows 上 localhost 的代理绕行问题。**
   `urllib.request.getproxies()` 读 `ProxyServer` 但**不读** `ProxyOverride`，
   所以 Windows 系统代理设置里的"局域网除外"那条会被静默丢掉，localhost 流量被送去走代理，
   表现是**代理 502** 而不是"connection refused"。
   **`NO_PROXY` 是唯一真正生效的开关。** 所以 `config.load_config()` 里有
   `os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")`，而且这一步**必须在 `.env` 解析之后**。
   （本机代理是 mihomo @ `127.0.0.1:9674`。另外：`openai` 的 `Connection error` 不保证是配置错——
   先探测连通性再怀疑配置。有一次是纯网络抖动，重跑即恢复。）

5. **mock 和真实模型的行为差异很大。**
   同一个任务，mock 是 4 次问模型 / 3 轮工具 / 正常收口；
   真实模型是 8 次 / 7 次 / 撞上限 / 无 Final Answer。
   **mock 只用来验协议和接线，不用来判断 Agent 好不好。** 任何关于"模型行为"的结论都必须来自真模型。
   另外 mock 的触发词是**顺序敏感**的（更具体的词要排在更短的词前面，"写越界"含"越界"，反了会误判），
   而且必须是**连续出现的字面串**（"越界 的情况"不触发）。

6. **相同 Tool Call 的重复检测不能误伤参数不同的修正行为。**
   两个具体的例子，必须放行：
   - 模型第一次读错文件名，换参数重试——**失败过的调用不进检测表**（门是 `result.startswith("[工具失败]")`）。
   - task_1_c 写完文件后用**略微不同的内容**重写一遍——参数不同就不算重复，**正确放行**。
   - 读 A、读 B、再读 A 的同类变体：重复检测只认"工具名 + 规范化参数全等"，这种发现不了，仍只靠步数上限兜住。这是已知的局限，README 已声明。

7. **`finish_reason` / `usage` 不能丢。**
   Phase 5.5 那轮就是因为 `ask()` 只返回 `.message`，导致整轮报告不出 `finish_reason`，
   只能靠"`reply.tool_calls` 非空"反推。改成 `ModelReply` dataclass 之后再没丢过。
   用 `getattr(..., None)` 取，因为有些兼容网关不返回 `usage`；`None` 显示成 `unavailable`，不崩、不编造。
   **不要在没确认服务商真的返回的情况下，编一个 token 数字出来。**

8. **Eval 不应该修改 Agent 的行为。**
   `eval/run_task.py` 只**驱动** `main.build_client` / `main.ask` / `main.log_reply` / `main.run_agent_loop`，
   一个字都不改，所以测出来的 = 终端手打时的行为。
   它要看到第 2..N 轮回复，做法是用一个**只记录不修改**的 wrapper 替换模块全局 `main.ask`，用完立刻还原。
   它要拿到纯 JSON stdout，做法是跑 Agent 期间把 `sys.stdout = sys.stderr`（`main.py` 没有缓存流引用，安全）。
   **改 Eval 的时候不要顺手"优化"一下 main.py——那就不是测基线了。**

9. **Eval 的 success 规则只能判断客观项，不能证明内容质量。**
   规则集只有：有没有最终回答、撞没撞步数上限、有没有运行时报错、
   文件在不在/空不空/是不是指定路径、工具链里有没有必需步骤、
   最终回答里有没有提到不存在的文件、文本含不含指定词。
   **"摘要写得好不好""比较说得对不对"一律不给分，只记 `needs_manual_review`。**
   这就是为什么 8/8 成功是个弱结论——task_3 被规则判过、但它实质没做完。
   改 success 规则时要警惕：加一条主观规则就等于把这个 Eval 变成另一个 LLM 打分，那不是这个项目想要的。

10. **补两条环境层面的（不常踩但踩过）：**
    - Windows 上 `Path.write_text()` 默认文本模式会把 `\n` 翻成 `\r\n`。
      `write_file` 用 `open(..., newline="")` 就是为了这个。
      （反过来 `read_text()` 默认 `newline=None` 会把 `\r\n` 翻回 `\n`，所以读不受影响。）
    - `len(x) or 0` **防不住 `None`**——`len(None)` 先抛错，`or 0` 轮不到短路。正确写法 `len(x) if x else 0`。
    - 本机 stdout 编码是 `gbk`(cp936)，程序在自己终端里中文正常，**被管道捕获时会乱码**。
      用 `$env:PYTHONUTF8='1'`（或 `PYTHONIOENCODING=utf-8`）修。
    - PowerShell 5.1 没有 `&&` / `||`。喂多行输入要用真实文件 `cmd /c "... < file"`。

---

## 附：本文件核对时发现的事实不一致（只指出，未自行修复）

按你交代的规矩，这些**只指出来，不动**。全部是文档层面的，不影响代码、不影响测量结论。

| # | 位置 | 不一致内容 | 严重度 |
|---|---|---|---|
| 1 | `README.md:24` | 标题仍是「当前状态：Phase 6」，但 README 自己第 255 行有 Phase 6.5 章节、第 337 行 checklist 也有 Phase 6.5 ✅ | 低（标题过期） |
| 2 | `README.md:33-39` | 「Phase 5 加的三样东西」表格里列了 `TOOL_HANDLERS`。但 `TOOL_HANDLERS` 实际是 **Phase 3** 加的（Phase 3 是关键的工具执行层）；Phase 5 真正新增的是 `list_files` / `write_file` + 结果长度保护 | 中（阶段归属错误，会让接手者误判工具执行层是什么时候引入的） |
| 3 | `README.md:72-80` | 同一个 ```bash 代码块（`cd mini-agent-lab` + `.venv\Scripts\python.exe main.py`）**连续重复了两次** | 低（明显的粘贴残留） |
| 4 | `README.md:230-233` | 项目结构树里 `demo_workspace/` 只列了 3 个文件，实际现在是 **5 个**：多出 `notes/agent_summary.md`（1533 B，Phase 5.5 产物）和 `notes/agent_summary_real_v2.md`（1617 B，Phase 6 复测产物） | 低（真模型实测的产物留在沙盒里，没同步进文档） |
| 5 | `README.md:93-125` | 「预期输出（mock）」块里 Turn 1 的 token 是 `861 / 29 / 890`——**这三个数字和 REAL_RUN_LOG 里 Phase 6 真模型复测的 Turn 1 完全一致**。作为"mock 输出"展示时数值来源不对（mock 不会返回真实 token 用量） | 低（展示口径混淆，不是数据错误） |
| 6 | `README.md:167-173` | 「两组测试都用 venv 的 Python 跑」——Phase 6.5 之后实际是**三组**（`tests/test_sandbox.py`、`tests/test_loop.py`、`eval/test_metrics.py`） | 低（表述过期） |
| 7 | `eval/REPORT.md:46` | task_6 的工具链写成 `list → read×2 → list → read×2`（数出来 6 次），但 `results.json` 里 task_6 的 `tool_calls_requested` 是 **7**。总览里的"共 31 次"是对的（6+2+1+3+1+**7**+4+7=31），只有这一格的链路记述少了 1 次调用 | 低（表格记述不精确，汇总数正确） |

**没有发现**冲突的项：REAL_RUN_LOG 的 Phase 6 复测 token 合计（861+944+1363+1808+2312 = 7288 prompt；29+73+58+508+216 = 884 completion；890+1017+1421+2316+2528 = 8172 total，7288+884 = 8172 自洽）；
`prompt 40,816 + completion 5,195 = total 46,011`；Phase 5.5 的归因结论与 Phase 6.5 失败分类里 `max_steps_hit` 归 Model Behavior 一致；
README 与 REPORT 关于 8/8、2.20 倍、88.71%、31 次工具调用、0 重复拦截、0 撞上限、7/8 人工评审的口径全部一致。

---

## 15. Phase 14：Coding Task Contract / Machine-Verifiable Acceptance

本阶段代码已实现，HEAD 仍在等待提交；不要把真实运行的 FAIL 误读成 Verifier 失败：
真实模型确实修复了允许文件并通过了独立最终测试，但没有给 Final Answer 且撞上
`MAX_AGENT_STEPS`，所以按 Contract 必须拒绝。

### 新增与接入

- `acceptance.py`：`CodingTaskContract` / `TestCommand`、JSON 加载、workspace SHA-256 快照、
  changed file 差异和确定性 `verify_contract`。
- `cli.py`：保留普通 `--task`，新增 `--contract CONTRACT.json`；带 Contract 时先拍快照，
  Agent 结束后独立重跑固定测试并在 JSON 中返回 `acceptance`。
- `tools.py`：`run_command` 增加显式 workspace 参数，Verifier 复用同一套 `shell=False`、
  timeout、cwd 沙盒和 command allowlist，不经过 LLM Tool Call。
- `tests/test_acceptance.py`：Contract、快照增删改、命令安全、Mock A/B/C/D、缓存排除和
  无 LLM 验证；`tests/fixtures/coding_contract.json` 是可复用 Contract。

### 当前验收规则

`accepted=true` 必须同时满足：

1. Agent 有 Final Answer；
2. 没有 `MAX_AGENT_STEPS`；
3. 没有 Runtime exception；
4. `unexpected_changes` 为空；
5. Verifier 独立执行 Contract 测试且 `final_test_exit_code == 0`。

`agent_ran_required_test` 只记录 Agent 是否确实执行了与 Contract 完全一致的测试命令；
它不是最终正确性的替代品。Verifier 始终自己执行最终测试。

### 本地结果

```text
python -m unittest tests.test_acceptance tests.test_cli -q
Ran 16 tests ... OK

python -m unittest discover -s tests -p "test_*.py" -q
Ran 66 tests ... OK

git diff --check
OK
```

### 下一阶段只做分析

最值得补的是 Agent 的收口/预算策略：真实样本已经证明「最终测试通过」仍可能因为
重复动作耗尽步数而不被接受。下一阶段再决定是否调整模型提示、重复检测或预算；本阶段不实现。

**交接完毕。** 项目已完成 Phase 14：Context Management、Session Persistence、Long File Reading、
Tool Permission / Side-effect Approval、Generalized Tool Capability / Permission Policy、
Controlled Command Execution、Bounded Coding Loop 与 Coding Task Acceptance 均已收尾；
Session 文件默认不进入版本库，后续阶段不自动开始。

---

## 16. Phase 15：Coding Completion & Budget Control

Phase 15 的唯一目标是让 Coding Task 在证据充分后更可靠地收口，同时保留
`MAX_AGENT_STEPS = 8` 作为最终保险丝。没有引入 Planner、Reviewer LLM、MCP、Memory、RAG、
Diff/Patch Tool、Parallel Execution 或 Git 写操作。

### 实现

- `main.py`：`CodingTaskTrace` 为每个 Tool Call 记录 `classification`：
  `PRODUCTIVE`、`BLOCKED_DUPLICATE`、`POLICY_REJECTED`、`FAILED_COMMAND`、
  `SUCCESSFUL_COMMAND`；汇总 `executed_tools`、重复拦截、策略拒绝、失败/成功命令和 token。
- `acceptance.py`：Verifier 结果增加 `artifact_passed` 与 `interaction_completed`；
  `accepted = artifact_passed AND interaction_completed AND 其它现有必要条件`。
- `cli.py`：Contract 模式给模型短 Guidance，包含 `allowed_paths`、严格 required test 和
  测试成功后的收口规则；普通 `--task` 不改变。
- required test 只按 `command + args + cwd` 严格匹配。匹配且 exit 0 才添加 Completion Hint；
  普通 `exit 0` 命令不触发。
- Coding Task 下重复调用提示明确说明“动作刚刚已经成功执行，没有新信息”，但仍只回喂 Tool
  Result，由模型决定是否 Final Answer。

### Mock 与回归

| Case | 结果 |
|---|---|
| A：read → write → required test pass → Final | 正常收口；Hint 出现 |
| B：test fail → rewrite → required test pass → Final | 第一次不触发 Hint，第二次触发 |
| C：重复动作 | 第二次不执行，分类 `BLOCKED_DUPLICATE`，可随后 Final |
| D：Policy Reject → 正确 required test → pass → Final | 正常恢复，分类 `POLICY_REJECTED` |
| E：顽固模型持续调用 | 仍在第 8 个 model call 触发保险丝 |

本地验证：`python -m unittest discover -s tests -p "test_*.py" -q` 共 68 项通过；
`git diff --check` 通过。Phase 14 的真实基线仍是 8 model calls / 8 tool calls、
最终测试通过但无 Final Answer、`artifact_passed=true`、`interaction_completed=false`、
`accepted=false`。

Phase 15 真实模型复跑未进入模型：第一次因缺少 `socksio` 无法初始化 SOCKS 代理，修正为
HTTP/HTTPS 代理后，连通性检查确认 `127.0.0.1:7897` 不可连接，客户端返回
`APIConnectionError`；两次 trace 均为 0 model calls / 0 tool calls。因此没有合法的 Phase 15
真实 Agent 链、token 对比或“更早收口”结论，避免将环境失败冒充模型结果。

下一阶段最值得补的是在可用模型/代理环境下重复同一 fixture 的真实对照实验，并比较
Completion Hint 对 model/tool calls、Final Answer、MAX_AGENT_STEPS 和 token 的影响；本阶段不实现。

---

## 17. Phase 16：Patch-based Editing

### 本阶段唯一目标

为已有文本文件增加最小的局部修改能力：

```text
apply_patch(path, old_text, new_text)
```

不做 Git apply、unified diff parser、AST rewrite、fuzzy patch、Patch Context Compression、
自动 Code Review、MCP、RAG、Memory、Planner、Parallel Tool 或 Git 自动 commit。

### 实现与数据流

- `tools.py`：新增 `APPLY_PATCH_TOOL`、`apply_patch()` 和 Registry 注册项。
- `main.py`：系统提示词公开新工具；`CodingTaskTrace` 新增
  `apply_patch_calls`、`patch_successes`、`patch_failures`；Trace 参数只记录长度摘要。
- Permission：`apply_patch` 声明 `RiskLevel.SIDE_EFFECT`，直接复用 Registry → Sandbox →
  Approval → Handler 链，不增加工具名特判。
- Duplicate：成功 patch 的完整 `path + old_text + new_text` 指纹进入现有集合；再次相同调用
  返回重复 Tool Result，不重复写盘；失败和 DENY 不锁定，允许模型恢复。
- Contract：Acceptance 只看最终 changed_files 和固定测试，不依赖使用 `write_file` 还是
  `apply_patch`。

### Mock A-D 与本地验证

| Case | 结论 |
|---|---|
| A：一次唯一 patch → test → Final | 成功；Completion Hint 出现；Acceptance accepted |
| B：old_text 不唯一 → 重新 read/增加上下文 → patch | 第一次失败且文件不变，Runtime 不猜位置，第二次成功 |
| C：目标已变化 → old_text not found → 重新 read → 新 patch | 失败是正常 Tool Result，失败调用不进入 duplicate guard，后续可恢复 |
| D：DENY | 文件完全不变，合法 `role="tool"` 结果，模型可正常收口 |

`tests/test_apply_patch.py` 新增 20 项覆盖：Registry/Risk、唯一/0/多匹配、局部删除、中文、
多行、CRLF、Sandbox、ALLOW/DENY、Duplicate、失败重试、Session、Context、Mock A-D 和
`patch → test → Final → Acceptance`；全量 `python -m unittest discover -s tests -v` 共 88 项通过。

### Phase 15.5 baseline 与真实 Phase 16

Phase 15.5 baseline：`write_file` 1 次、`run_command` 1 次、重复拦截 2 次，
7 model calls / 7 tool calls，tokens 为 prompt `12982` / completion `613` / total `13595`，
Verifier `accepted=true`。

Phase 16 隔离 calculator fixture 的真实链：

```text
list_files → read_file × 2 → apply_patch → run_command(required test) → Final Answer
```

Trace：5 model calls、5 tool calls、`write_file=0`、`apply_patch=1`、patch success `1`、
patch failure `0`、`run_command=1`、required test exit `0`；tokens 为 prompt `10322` /
completion `397` / total `10719`。独立 Verifier：`changed_files=["calculator.py"]`、
`unexpected_changes=[]`、`artifact_passed=true`、`interaction_completed=true`、
`accepted=true`。

这是单次观察，不做统计性结论。小 fixture 的 old/new 两段合计字符数可能大于完整文件；
patch 的实际价值是避免真实大文件重复生成未修改内容，并把修改边界明确限制在唯一片段。
下一阶段最值得补的是基于更大真实文件的 patch 参数/上下文成本测量，再决定是否需要独立的
Patch Context Compression 或模型工具偏好实验；本阶段不实现。

---

## 18. Phase 17：Repository Navigation / Code Search

### 本阶段唯一目标

增加一个只读文本搜索工具，让 Agent 在不知道目标文件名和路径时可以先定位代码：

```text
search_text(query, path=".", max_results=20)
```

只做固定字符串匹配。`list_files` 是一层目录浏览，`search_text` 是递归的候选定位，`read_file` 才
负责精读；0 matches 正常回传给模型，不自动换 query，也不自动决定最相关文件。

### 实现与边界

- `tools.py`：新增 `SEARCH_TEXT_TOOL`、`search_text()` 和 Registry 注册项。
- `search_text` 沿用 `resolve_inside_workspace()`；搜索根必须在 `WORKSPACE_DIR` 内，外部 `..`、绝对
  路径和符号链接逃逸均拒绝。
- 默认跳过 `.git`、`.venv`、`__pycache__`、`sessions`、`eval/runs`、`.pytest_cache`、`.mypy_cache`、
  `.ruff_cache` 和 `node_modules`。无法按 UTF-8 读取或含 NUL 的文件跳过，不把二进制交给模型。
- 默认最多返回 20 个匹配行，上限 100；每项给相对路径、命中行号、命中行及前后一行上下文。
  结果包含 `matches_shown`、`matches_total`、`truncated`，并主动遵守 `MAX_TOOL_RESULT_CHARS`。
- `RiskLevel.READ_ONLY` 直接复用 Registry → Permission → Handler；没有 `if tool_name ==
  "search_text"` 特判。Trace 另计 `list_files_calls`、`search_text_calls`、`read_file_calls`。

### Fixture 与 Mock 闭环

新增 `tests/fixtures/repo_fixture/`：`src/pricing.py` 含 `calculate_discount` bug，另外有 app、formatting、
utils 和测试文件。Contract 只允许改 `src/pricing.py`，给模型的 instruction 只描述 bug，不给路径。

Mock 链已验证：

```text
search_text(calculate_discount)
→ read_file(src/pricing.py)
→ apply_patch
→ run_command(required test)
→ Final Answer
→ Acceptance accepted=true
```

本地新增 16 项搜索/导航测试；全量 `python -m unittest discover -s tests -q` 为 104 项通过，1 项
符号链接测试因当前 Windows 运行环境不允许创建链接而跳过。旧的 Patch、Command、Permission、
Session、Context、Completion Control 和 Acceptance 回归均通过。

### 真实运行

有效隔离运行使用 `tests/fixtures/repo_fixture` 和 `tests/fixtures/repo_contract.json`，用户任务未提供
目标路径。先用当前可用代理 `http://127.0.0.1:9674` 发送无 Tool 的最小请求并收到 `OK`；随后真实
Coding Task 成功完成。模型本次没有调用 `search_text`，而是通过 `list_files` 浏览工作区、`src`、
`tests` 和 `src/utils`，直接定位并读取 `src/pricing.py` 与 `tests/test_pricing.py`。

真实统计：`model_calls=6`、`tool_calls=8`、`list_files_calls=4`、`search_text_calls=0`、
`read_file_calls=2`、`write_file_calls=0`、`apply_patch_calls=1`、`run_command_calls=1`；tokens 为
`prompt=14739`、`completion=499`、`total=15238`；`max_steps_reached=false`。唯一修改是
`src/pricing.py`，required test exit code 为 0，Verifier `artifact_passed=true`、
`interaction_completed=true`、`accepted=true`。完整 Final Answer、Trace、候选路径和验收证据保存于
`eval/phase17_real_run.json`。

这是一次行为观察：真实模型可以在不知道目标路径时靠 `list_files` 完成导航，但本次没有证明它会主动
选择 `search_text`。下一阶段若继续，最值得做的是在可用 Provider 下重复观察工具偏好；本阶段不实现。

---

## 18. Phase 18：Repository Navigation Eval（当前收尾）

本阶段新增三个评测模块：

- `eval/navigation_fixtures.py`：确定性生成 7 / 25 / 75 文件的安全 fixture，symbol 与 error-string ground truth 固定。
- `eval/navigation_metrics.py`：从 canonical message history 计算 list/search/read、candidate files、首个正确文件 turn 和 token 成本。
- `eval/navigation_eval.py`：每个场景独立临时 workspace、最多一次真实 Agent Task；Coding 场景接入现有独立 Acceptance。

离线测试验证了 fixture 数量、唯一 symbol definition、唯一错误字符串、search 命中、未修复测试失败、修复后测试通过、
navigation metric 和 Coding contract。全量回归为 `110` 项通过，`1` 项 Windows 符号链接能力测试跳过。

四个有效真实场景均已各运行一次并被接受。首次使用未监听的 `127.0.0.1:7897` 未进入 Agent loop，随后使用
Phase 17 已验证的 `127.0.0.1:9674` relay 完成有效运行；原始记录：`eval/navigation_results.json`；报告：
`eval/NAVIGATION_REPORT.md`。没有把传输配置失败混入模型行为数据。

Phase 18 的下一步只应是 Provider 可用时重新运行这四个一次性场景并填充对照数据；不要提前新增 Navigation Guidance，
也不要修改 `search_text` 描述或系统提示词。

---

## 19. Phase 18.5：Repository Navigation Stability Check（已完成）

Phase 18.5 以 `0aaae56` 为基线，对 Phase 18 的 SMALL / MEDIUM / LARGE-SYNTHETIC symbol 场景各新增 2 次
真实运行，与原基线合并为每场景 `n=3`。没有修改 Agent runtime、Tool schema、Prompt、上下文窗口或验收逻辑。

有效新增运行共 6 次，Provider failure 为 `0`。SMALL 为 3/3 次 `search_text → read_file → Final`；MEDIUM 为
3/3 次 `list_files ×3 → search_text`，Coding acceptance 为 `2/3`；LARGE 三次均使用 `search_text`，Coding
acceptance 为 `3/3`。MEDIUM 的一次未接受运行是 max steps 且没有 Final Answer，但 artifact 与最终测试均通过；
required test 按 contract 在三次中均未运行。MEDIUM/LARGE 的 `first_correct_file_turn` 都稳定在 `3`，SMALL 稳定在 `1`。

早期稳定性 harness 曾在 LARGE 的一次尝试上发生 900 秒超时且未序列化结果；该 harness attempt 单独保存在
`eval/navigation_stability_prior_failures.json`，没有计入 n=3，也没有当作 Provider failure 或模型行为数据。

报告和机器结果：`eval/NAVIGATION_STABILITY_REPORT.md`、`eval/navigation_stability_results.json`。当前结论是
导航观察已足够支持“SMALL 稳定、MEDIUM 收尾有波动、LARGE 搜索稳定但多路径”；暂不实现 Navigation Guidance。
本阶段全量回归为 `113` 项通过、`1` 项跳过。

---

## 20. Phase 19：Required-Test Visibility Experiment（已完成）

Phase 19 以 `8c0620d` 为 baseline。Control 直接复用 Phase 18.5 MEDIUM 三次记录，不重新调用模型；Treatment
使用相同 MEDIUM fixture、任务、Provider、Tool Schema、Runtime、Acceptance 和 `MAX_AGENT_STEPS`，唯一变化是
在模型上下文中增加事实性的 exact required test：`python -m unittest discover -s tests -p test_discount.py -q`。
没有增加 Final/优先执行指导，也没有修改 Runtime。

Treatment 共执行 3 次：2 次有效，1 次 `APIConnectionError` provider failure。有效 Treatment 两次均为
`exact required test → Completion Hint → Final`，均 `accepted=true`；有效分母为 2。Control 为 exact test `0/3`、
Hint `0/3`、Final `2/3`、accepted `2/3`、MAX `1/3`。有效 Treatment 对应指标均为 `2/2`，并出现 `zero-test=0`。

评测代码、结果和报告：`eval/required_test_visibility.py`、`eval/required_test_visibility_results.json`、
`eval/REQUIRED_TEST_VISIBILITY_REPORT.md`。该 n=3 结果只能支持“required-test 可见性与更稳定收口一致”，不作因果证明。
Phase 19 专项测试与全量回归通过：全量 `117` 项通过、`1` 项 Windows 符号链接能力测试跳过。

### Phase 19.5R Recovery harness 启动方式

从 repo root 启动 Recovery 时使用 module invocation，并显式加入 `eval` 到 `PYTHONPATH`：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "eval")
C:\Users\30858\mini-agent-lab\.venv\Scripts\python.exe -m eval.required_test_visibility_recovery
```

不要直接运行 `python eval\required_test_visibility_recovery.py`；该路径不会自动把 repo root
加入 import path。`--help` smoke test 只验证启动，不调用模型。

---

## 21. Phase 21：Explicit Task Finish + Deterministic Finish Gate（已完成）

Phase 21 以 `9c114e6`（Phase 20）为 baseline。Codex 在本阶段实现了完整的 Runtime 骨架后额度耗尽，
工作树留下 13 个已修改文件和 1 个新测试文件、**没有提交**。Claude Code 接力完成了收尾。

### 本阶段唯一目标

显式 `finish_task` 工具 + 确定性 Finish Gate + 最小 Task State。
明确不做：STUCK detector、Planner、Critic、Reviewer LLM、Multi-Agent、Event Bus、DAG、MCP、RAG、
Memory、自动测试、fuzzy command matching、语义化 Finish Judge。

### 实现与数据流

```
LLM → tool_calls
     → run_tool_round
         → 普通工具：permission → execute → 更新 TaskState 游标
         → CONTROL_FLOW：专用分发器（跳过 permission / duplicate / handler）
             → evaluate_finish_request（纯判定）→ FINISHED | REJECTED
     → FINISHED → Agent Loop 退出
```

`tool_kind` 与 `risk_level` 正交。三道隔离：`run_tool_round` 在普通分发前查 `tool_kind` 并 `break`；
`check_tool_permission` 对 CONTROL_FLOW 放行；`execute_tool_call` 对 CONTROL_FLOW 返回失败作为兜底。

一个 response 只处理到第一个 CONTROL_FLOW 调用为止，`tool_calls_through_control_flow` 截出的前缀
同时用于「执行」和「写回 canonical assistant message」。截断作用于两处是必需的：残留的 tool_call
会留下没有配对 result 的 `tool_call_id`，下一次请求被提供商 400。Gate 拒绝时同样截断。

### Mock A–J

`tests/test_finish_protocol.py` 覆盖全部十条场景，全部通过：

| 编号 | 场景 | 结论 |
| --- | --- | --- |
| A | patch → exact test PASS → finish_task | `FINISHED`，Gate 接受，acceptance 全绿 |
| B | test FAIL → patch → finish REJECT → test PASS → finish | 第二次接受；拒绝原因是 stale，不是 missing |
| C | 改动测试文件后 finish | 拒绝，`unexpected_change:test_calculator.py`，最终 `LIMIT_REACHED` |
| D | finish 后同批还有 read / run | 不执行，canonical 无 dangling call |
| E | finish 被拒后同批还有 tool call | 不执行，状态保持 `RUNNING` |
| F | 完全相同的 finish_task 第二次提交 | 不被 duplicate guard 拦截，REJECTED → FINISHED |
| G | Contract 生效 + 普通 Final | 状态保持 `RUNNING`，注入 nudge 后再问一次 |
| H | 最后一步 finish 且 Gate PASS | `FINISHED`，`max_steps_reached=False` |
| I | 最后一步 finish 但 Gate REJECT | 记录拒绝，协议合法，`LIMIT_REACHED` |
| J | 历史失败后更新的 fresh PASS | 历史失败不永久污染，最终允许 finish |

### 本地验证

`python -m unittest discover -s tests -p "test*.py"`：**149 项通过，1 项 Windows 符号链接能力测试跳过**。
`compileall` 通过，`git diff --check` 无空白错误。
Session、Permission、Sandbox、Context、Long-file、Patch、Search、Command、Acceptance 全部无回归。

### 关键设计取舍

1. **freshness 用逻辑时钟不用时间戳**：`event_seq` 对每个工具尝试递增，但只有 `write_file` /
   `apply_patch` 在前后 workspace 摘要确实不同时移动 `last_mutation_event_seq`；
   `run_command` 仅在 command + args + cwd 完全匹配且退出码 0 时移动测试游标。
   失败、被拒、duplicate、no-op 都不移动。
2. **MAX vs Finish**：每个被允许的模型响应先完整处理到第一个 CONTROL_FLOW 调用，
   所以最后一步的合法 finish 优先于步数上限。代价是 Contract 生效且模型反复给普通 Final 时，
   nudge 会让实际请求数最多接近两倍——这是 Phase 21 的取舍，不是缺陷。
3. **Hint 只作引导**：required test 通过后提示改为「调用 `finish_task(summary=...)`」，
   同步更新了 coding guidance、duplicate notice 和系统提示词。

### 交接修正（Claude Code 补的部分）

Codex 的实现中 `verify_contract` 无条件用 `task_state` 判定 `interaction_completed`，
导致**不驱动 finish 协议的旧调用方全部变成 `accepted=false`**，
reason 是它们从未被告知要调用工具的 `finish_task_not_accepted`。
受影响的现有调用方：`eval/navigation_eval.py`、`eval/post_mutation_verification_guidance.py`、
`eval/required_test_visibility.py`（三者都不传 `task_state`）。
后果是 Phase 18–20 的评测报告会系统性误报失败，且已保存的结果 JSON 不可比。

修正为：传入 `task_state` 时按 finish 协议判定；未传时保留「普通 Final + 未触上限」的旧语义。
这样 `verify_contract` 只在实际执行了 finish 协议的运行上要求 `finish_task`，不做无依据的归咎。
补了两个回归测试锁住该行为。

同时把 `_finish_control_flow_result` 里重复的 summary 校验改为直接调用 Tool Registry 中的
`tools.finish_task`，消除了两处相同的校验逻辑，也让该 handler 不再是死代码。

改动范围：`acceptance.py`、`main.py`、`tests/test_acceptance.py`。

### 真实模型验证

单次运行，MEDIUM fixture（`tests/fixtures/repo_fixture` + `tests/fixtures/repo_contract.json`）。
Provider：`sensenova-6.8-flash-lite`，`TOOL_APPROVAL_MODE=ALLOW`。

```
list_files → read_file ×2 → apply_patch → run_command（exact test, exit 0）→ finish_task → FINISHED
```

`model_calls=4`，`finish_task_calls=1`，`finish_successes=1`，`finish_rejections=0`，
`max_steps_reached=False`，`total_tokens=10882`。
`event_seq`：apply_patch=4（mutation）、run_command=5（测试通过）、finish_task=6。
`artifact_passed=true`，`interaction_completed=true`，`agent_self_verified=true`，`accepted=true`，
`unexpected_changes=[]`，独立 Verifier 重新执行测试 `final_test_exit_code=0`。
原始记录：`eval/phase21_real_run.json`。

### 已知限制

1. **`DEFAULT_APPROVAL_MODE=ASK` 在非交互环境下会让 Coding Task 必然失败**：无 TTY 时 `input()` 返回
   EOF，`ask_for_approval` 按安全默认拒绝所有副作用工具。真实 CLI 运行需要显式
   `TOOL_APPROVAL_MODE=ALLOW`。这是 Phase 11 就存在的配置语义，不是 Phase 21 引入的，
   但 Phase 21 把它变成了硬阻塞（模型无法写入 → 无法通过测试 → 无法 finish）。
2. **测试运行器自身的缓存目录不被排除**：`snapshot_workspace` 只排除 `__pycache__` 和 `.pyc`/`.pyo`。
   若 Contract 的 required test 用 `python -m pytest`（命令策略允许），生成的 `.pytest_cache/`
   会被当作 `unexpected_change` 阻塞 finish。当前 fixture 用 `python -m unittest`，不触发。
   这是 Phase 14 verifier 与 Finish Gate 共享的既有缺口，未在本阶段扩大范围处理。
3. **交互模式（`main.py`）下 finish 协议只在 `--resume` 一个携带 Coding 状态的 Session 时生效**；
   新建交互式 Session 没有 Contract。这与 Phase 14 的入口结构一致（Coding Task 走 `cli.py`），
   不是本阶段引入的。
4. **`.pytest_cache` 之外的 test-cache 目录**（`.mypy_cache` 等）同样不在排除列表内。

## 22. Phase 21.1：Runtime Hardening（已完成）

Phase 21.1 以 `ec2fdfd`（Phase 21）为 baseline。这是基础设施硬化，**不是新的 Agent 能力阶段**：
Phase 21 的 `finish_task` schema、`ToolKind`、Finish Gate freshness 规则、exact required test 匹配、
`MAX_AGENT_STEPS`、Completion Hint、`accepted` 定义、Session schema、Agent Loop 主结构全部未改动。
本阶段只解决 Phase 21 真实运行暴露的两个环境问题。

### 1. ASK + 无交互通道：快速失败

原状（Phase 21 真实运行第 2 节记录过）：无 TTY 时 `input()` 返回 EOF，`ask_for_approval` 按安全默认
拒绝，副作用工具全部被拒，模型反复重试到 `MAX_AGENT_STEPS`，`finish_task_calls=0`，
`LIMIT_REACHED`，`total_tokens=23660` 全花在环境失败上。

修法（`main.py` + `cli.py`）：

- 新增 `ApprovalUnavailableError(RuntimeError)` 和统一文案
  `NON_INTERACTIVE_APPROVAL_ERROR`。它**不**复用 `APPROVAL_DENIED_PREFIX`：
  「没有审批通道」是环境失败，「用户明确拒绝」是用户决定，混在一起会让日志和 Session 记录同时失真。
- `check_tool_permission` 的异常列表保持 `(OSError, TypeError, ValueError)`，
  所以 `RuntimeError` 子类会原样向上传播，不会被改写成一个普通工具拒绝。
- `cli.run_task` 在第一次 `main.ask` 之前做 preflight：`contract is not None` 且
  `approval_needs_interactive_input(get_approval_mode())` → 返回
  `{"status": "failed", "answer": None, "error": ..., "trace": ...}`，`model_calls=0`。
  形状与既有的失败分支一致（`acceptance` / `task_state` 不生成，因为什么都没跑）。
- `ask_for_approval` 在打印提示**之前**检测通道；检测不到就抛异常，不再把 EOFError 当正常控制流。
- `main.main()` 把该异常加入既有通信失败分支，Contract 生效时记入 `task_state` 的 `ERROR`，
  普通聊天回滚本轮悬空消息。没有为它复制第二份恢复逻辑。

交互通道判定用两端条件：`stdin.isatty() and stdout.isatty()`。
只看 stdin 在这台 Windows 上不够——`isatty()` 对 `NUL` 等字符设备也返回 True，
实测 `stdin=DEVNULL` 的子进程报 `True`，`stdin=PIPE` 报 `False`。
两端都要求终端后，无 TTY 的 harness 才会被正确判定为「不可交互」。
代价：`python main.py > transcript.txt` 这类只重定向输出的跑法里，副作用工具会报
「审批不可用」而不是弹出提示——提示本来也看不见，报清晰错误比让人盲打更对。

安全语义未变：没有用户明确批准，SIDE_EFFECT / EXECUTION 不执行。
ASK + 无 TTY **不会**被自动降级成 ALLOW，也不会跳过 Approval。
ALLOW / DENY 不检测终端，行为不变。READ_ONLY 和 CONTROL_FLOW 在回调之前就已放行，不受影响。

### 2. Snapshot 忽略 pytest cache

原状：`snapshot_workspace` 只排除 `__pycache__` 和 `.pyc`/`.pyo`。
Contract 的 required test 一旦用 `python -m pytest`，生成的 `.pytest_cache/` 会成为
`unexpected_change:<path>`，Finish Gate 永久拒绝。

修法只动 `acceptance.py` 一处：

```python
_GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
_GENERATED_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache"}
```

`_is_generated_artifact` 是唯一过滤点，`snapshot_workspace` 调用它，
Finish Gate（`evaluate_finish_request`）和独立 Verifier（`verify_contract`）都从
`snapshot_workspace` 取摘要——**没有第二套规则**，两边判定天然一致。
按目录名锚定匹配，所以根目录同名的 `README.md` 仍然被跟踪。

为什么只加 `.pytest_cache`：清单必须保持刻意地短。
忽略所有点目录 / 所有隐藏文件 / 所有未知新文件，都会让真实的越界改动逃出 Verifier，
而那正是这个机制存在的理由。「只忽略明确列举的缓存」是唯一不会放过真问题的形状。
`.coverage`、`.mypy_cache`、`.ruff_cache` 本轮未确认，**不加**；
有测试锁住它们仍然被报为 `unexpected_change`。

忽略只影响 `changed_files` / `unexpected_changes` 的判定，
**不改 Sandbox 路径校验**，Agent 能访问的范围没有变化。
`tools.py` 里 `search_text` 对 `__pycache__` 的跳过是搜索遍历，不是变更检测，未触碰。

### 本地验证

`python -m unittest discover -s tests -p "test*.py"`：**166 项，1 项 Windows 符号链接能力测试跳过**。
`compileall` 与 `py_compile` 通过，`git diff --check` 无空白错误。
新增 17 个测试：审批行为 10 个（真实终端下 y/N 两条、无 TTY 快速失败且不调用 `input()`、
不作为普通拒绝、ALLOW / DENY 不受影响、READ_ONLY 不受影响、通道两端判定两条、模式判定两条）、
pytest cache 5 个、CLI preflight 2 个。

本轮**没有调用真实模型**。ASK + 无 TTY 用真实子进程验证过：
`cli.py --contract` 在 `stdin=DEVNULL`、API 指向失效本地端点的情况下，
返回 `status=failed`、`model_calls=0`、无 stderr、无 traceback；
同一条件下 ALLOW / DENY 都会越过 preflight 走到模型调用（被失效端点立即拒绝），
证明 preflight 没有误伤。
`.pytest_cache` 用模拟 cache 文件验证（`README.md`、`CACHEDIR.TAG`、`v/cache/nodeids`），
因为本环境未安装 pytest，也没有为本阶段安装新依赖。

### 顺带发现的既有问题（未修）

1. **`tests/test_session.py::test_main_missing_resume_has_no_traceback` 在这台机器上失败**，
   与 Phase 21.1 无关：把 HEAD 原样抽出到临时目录跑同一模块，失败完全一致。
   根因是子进程的 stderr 以 UTF-8 字节输出（`[Session\xe9\x94\x99\xe8\xaf\xaf] ...`），
   而父进程用 `text=True` 按 `cp936` 解码，reader 线程 `UnicodeDecodeError` 被吞掉，
   `result.stderr` 变成 `None`。用一个不 import 任何项目代码的
   `python -c "print('错误', file=sys.stderr)"` 复现同样的 UTF-8 字节，
   说明这是解释器/环境行为，不是本仓库代码的行为。修法应是测试里显式 `encoding="utf-8"`，
   但那是独立的小修，不在本阶段范围内。
2. 同一临时目录里 `test_finish_protocol.py` 有一个失败，但那来自
   `git archive` 抽出时是 LF 而工作树是 CRLF：该测试把文件原样写回去，
   `write_text` 的换行转换让 LF 文件变成 CRLF，被判定成「发生了改动」。
   工作树本身是 CRLF，该测试通过。这是一个对环境敏感的测试写法，不是 Phase 21 的缺陷。
