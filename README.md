# mini-agent-lab

从零手搓一个最小但真正可运行的 CLI Agent。

## 最终目标

```
用户输入任务 → LLM 判断下一步 → 选择工具 → 执行工具 → 结果回喂 LLM
             → 继续判断 → 连续执行多步 → 直到任务完成
```

我们要亲手把这个循环搭出来，而不是套框架。

## 技术选型（一句话）

- **Python 3.11** —— 标准库够 Phase 0 用；后面装 LLM SDK 也只需一个包
- **LLM 走 OpenAI 兼容协议** —— 用官方 `openai` SDK，但 `OPENAI_BASE_URL` 可指向任何兼容服务
  （OpenAI / DeepSeek / GLM / Kimi / OpenRouter / 本地 Ollama）
- **不用** LangChain / LangGraph / MCP / 数据库 / RAG / Memory —— 全部自己写，因为要经历它

选 OpenAI 兼容协议的真正原因：`openai` SDK 会自动读取 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`
两个环境变量，**换服务商只改 `.env`，一行代码都不用动**。

## 当前状态：Phase 16

用户只给一个**目标**，Agent 自己看目录、自己挑文件、自己读、
自己判断要不要再读一个，最后把整理好的结果**写回工作目录**并汇报：

```
用户任务 → list_files（探索）→ read_file（读取）→ write_file / apply_patch / run_command（受控执行）→ 最终回答
```

Phase 7 已完成 Context Management：默认模式为 `WRITE_ONLY`，只压缩发给模型的历史
`write_file` 内容，canonical `messages` 保持完整；同时保留 `OFF` 和 `FULL` 两种模式。

Phase 8 已完成 Session Persistence：交互式运行会把稳定的 canonical `messages` 保存到
`sessions/<session_id>.json`，可用 `main.py --resume SESSION_ID` 恢复同一段对话。
Session 不是 Memory：不做跨 Session 搜索、合并、自动摘要或知识提取。

Phase 9 已完成 Long File Reading：`read_file(path, start_line=1, max_lines=100)`
按完整行返回分页结果，并让 metadata 精确描述实际返回范围。工具层使用独立的安全字符预算，
给分页 metadata 和边界标记预留空间；`MAX_TOOL_RESULT_CHARS = 4000` 仍是所有工具结果的最终兜底。
Python 不自动循环翻页，模型根据 `has_more` 和 `next_start_line` 自主决定是否继续读取。

Phase 9 真实验证已通过：模型实际读取 `1-100 → 101-200 → 201-300 → 301-400 → 401-450`，
在第 377 行找到 `TARGET_FACT = "phase9-secret-value"`，随后正常给出 Final Answer，未撞步数上限。

Phase 10 已完成 Tool Permission / Side-effect Approval：`list_files` / `read_file` 属于
`READ_ONLY`，自动执行；`write_file` / `apply_patch` 属于 `SIDE_EFFECT`，必须先通过 Runtime 审批。
交互式 CLI 默认 `ASK`，自动入口和测试显式使用 `ALLOW` 或 `DENY`。拒绝也会生成合法的
`role="tool"` 结果，但不会写文件、不会进入成功重复调用集合，也不会被当作成功写入压缩。

Phase 11 已完成 Generalized Tool Capability / Permission Policy：工具的 Schema、Handler
和 `risk_level` 现在由同一个 `ToolDefinition` 放进 `TOOL_REGISTRY`。发给模型的
`AVAILABLE_TOOLS` 只是 Registry 派生出的 OpenAI-compatible Schema 视图；Permission
Runtime 只读取 `risk_level`，不再按 `write_file` 这样的具体工具名写分支。`READ_ONLY`
自动执行，`SIDE_EFFECT` 走现有 `ASK` / `ALLOW` / `DENY` 审批；`EXECUTION` 和
`EXTERNAL_SIDE_EFFECT` 作为统一风险 metadata 使用。

当前正式工具：

| 新增 | 说明 |
|---|---|
| `list_files` | 列工作目录一层内容，标 `[f]`/`[d]` 和字节数。`path` 可选，省略就是列根目录 |
| `write_file` | 写 UTF-8 文本，父目录不存在会自动创建，但只能创建在沙盒内 |
| `apply_patch` | 对已有 UTF-8 文本做 `old_text → new_text` 精确替换，`old_text` 必须唯一匹配 |
| `run_command` | 以 `shell=False` 执行白名单内的 Python 测试或 Git 只读命令 |
| `TOOL_REGISTRY` | 工具名 → `ToolDefinition(name, schema, handler, risk_level)`。新增正式工具只在这里注册 |

「先看目录、再决定读哪个」这件事**没有**写进系统提示词，程序里也不强制。
工具说明书里只有一句「如果你不知道有哪些文件，先用这个工具，不要猜文件名」——
让模型自己从工具描述里学出用法，而不是照着脚本走。
系统提示词只说「有哪些工具、各干什么」，最后一句是「用哪个工具、用什么顺序，你自己决定」。

Phase 6 在同一个循环上叠了三层**互相独立**的防线，解决真模型实测暴露的
「任务已经完成，但 Agent 不知道什么时候该停」：

| 层 | 谁负责 | 机制 |
|---|---|---|
| 1. 自己判断 | 模型 | 系统提示词里一句收敛原则：每次拿到工具结果后判断目标是否已满足，满足就直接给最终回答 |
| 2. 重复调用检测 | 程序（Runtime） | 同一工具名 + 规范化后完全相同的参数，且上一次**成功执行**过 → 不真执行，回喂一条重复提示 |
| 3. 最大步数 | 程序 | `MAX_AGENT_STEPS`（默认 8），数的是「问了几次模型」，撞线就停并明确告知 |

第 1 层是主力，第 2 层只在模型自己没停下来的时候提醒它一次，第 3 层是最后保险丝。
第 2 层**不会**代替模型做决定：拦下后照样按正常协议回喂一条工具结果，让模型继续下一轮；
是否结束仍然由模型自己说。

可观测性：每次问模型都会打印 `finish_reason` 和 token 用量。服务商没返回 `usage`
时显示 `unavailable`，不崩、不编造。日志里不含 API Key、Authorization 头或任何完整 HTTP 头。

重复调用检测只认「工具名相同 + 参数语义相同」：参数先 `json.loads` 再
`json.dumps(sort_keys=True)` 归一成规范串，所以键序换了、多了空格算同一个；
读不同文件、写同一文件但内容不同、列不同目录，都**不会**被误判。
失败过的调用不进检测表——模型换参数重试必须被允许。

刻意**不做**的两件事：不写死「写完文件以后禁止 read_file」（内容变了就是模型在改，得放行），
也不写死「write_file 成功以后立刻结束」（那样验证写入成功就被禁止了）。
所以「读回自己刚写的文件确认」这种正常验证是放行的；拦下来的只有
「同一个工具、完全相同的参数、上一次已经成功」这一种情况。

```bash
cd mini-agent-lab
.venv\Scripts\python.exe main.py
```

```bash
cd mini-agent-lab
.venv\Scripts\python.exe main.py
```

先用本地假服务器跑通接线（不需要 Key）：

```bash
# 终端 1
.venv\Scripts\python.exe tests\mock_server.py
# 然后把 .env 里的 BASE_URL 指向它
#   OPENAI_API_KEY=any-value
#   OPENAI_BASE_URL=http://127.0.0.1:8765/v1
#   OPENAI_MODEL=mock-model
```

预期输出（用户只给目标、不给文件名：模型自己选文件、自己写结果）：

```
Mini Agent Lab
模型：mock-model @ http://127.0.0.1:8765/v1
工作目录：C:\...\mini-agent-lab\demo_workspace（就绪）
可用工具：list_files, read_file, write_file
单个任务最多 8 步（模型问了几轮就停，防止无限循环）
工具结果上限 4000 字符（超过会截断并告知模型）
审批模式：ASK
输入一句话开始对话；输入 exit 退出。

你 > 看看工作目录里有什么学习资料。找到和 Agent 有关的资料，读取需要的内容，整理成一份简短学习摘要，保存为 notes/agent_summary.md。完成后告诉我你做了什么。

── Turn 1 ──
  finish_reason     : tool_calls
  prompt_tokens     : 861
  completion_tokens : 29
  total_tokens      : 890

Agent 想调用工具：list_files({})
Tool 结果 > 工作目录 . 的内容：
[f] agent_notes.md  (1363 字节)
[f] python_notes.md  (972 字节)
[f] todo.txt  (483 字节)

Agent 想调用工具：read_file({"path": "agent_notes.md"})
Tool 结果 > # Agent 学习笔记
...
Agent 想调用工具：write_file({"path": "notes/agent_summary.md", "content": "# Agent 学习摘要 ..."})
Tool 结果 > 已写入 agent_summary.md（304 字节，已写入新文件）

Agent > [mock 回复] 任务完成，我一共调用了 3 次工具：list_files → read_file → write_file。摘要已写入 notes/agent_summary.md。
```

注意三次「Agent 想调用工具」用的是**三个不同的工具**，中间每次都夹着一份「Tool 结果」——
模型是根据上一份结果决定下一步的，不是照着固定脚本走。
它列完目录后只读了 `agent_notes.md`，没去读 `python_notes.md` 和 `todo.txt`：
读哪几个、读几个、按什么顺序，都是它自己的决定。

mock 服务器还支持几个触发词，用来手动触发各种分支（不用 Key）：

| 输入里带上 | 触发什么 |
| --- | --- |
| `列文件` | 正常列出工作目录（`[f]` / `[d]` + 字节数）|
| `todo.txt` | 正常读到文件 |
| `越界` | 模型试图读 `../.env`，被沙盒拦下 |
| `不存在` | 读一个没有的文件 |
| `坏参数` | 模型给出非法 JSON 参数 |
| `big_notes.txt` | 读一个大文件，验证结果会被截断到 4000 字符并明说省略了多少 |
| `写个测试文件` | 正常写进工作目录（自动创建 `notes/`）|
| `重复调用` | 对同一文件提两次**完全相同**的调用：第一次真执行，第二次被重复检测拦下并回喂提示 |
| `写越界` | 模型试图写 `../evil.txt`，被沙盒拦下 |
| `写绝对路径` | 模型试图写 `C:/evil.txt`，被沙盒拦下 |
| `假工具` | 模型调用一个不存在的工具名，验证它变成 Tool Result 而不是程序崩溃 |
| `总是调用` | 拿到结果后模型还要工具，验证会被最大步骤数拦下来 |
| `中途崩` | 工具跑完、结果已回传之后那次请求故意 500 |
| `对比` | 连续读两个文件再总结，验证 Agent 循环真的能跑多步 |
| `agent_summary` | 多工具任务：`list_files` → `read_file` → `write_file` → 汇报 |

触发词必须是**连续出现的字面串**：输入「越界 的情况」不会触发（连续串是「越界」，
中间隔着别的字就不算），但「尝试读 ../.env 越界试试」会触发。
`总是调用` / `对比` / `中途崩` 本身不指定文件名，要搭配一个文件名触发词
（例如「总是调用 todo.txt 帮我看下」），mock 才知道先动哪个文件。
`big_notes.txt` 不在仓库里，要临时造一个够长的文件才看得见截断：

```bash
.venv\Scripts\python.exe -c "open('demo_workspace/big_notes.txt','w',encoding='utf-8').write('# 大文件\n' + ('这是一行占位内容，用来把文件撑大到超过工具结果上限。\n' * 150))"
```

Phase 10 的非交互测试/评测需要显式设置：

```powershell
$env:TOOL_APPROVAL_MODE = "ALLOW"
```

真实 CLI 保持默认 `ASK`。有路径的副作用工具会显示目标文件和 `CREATE` /
`OVERWRITE`；输入 `Y` 才执行，其他输入均视为拒绝。

mock 服务器还内置了一个协议校验器：它检查 `tool_calls` 和 `tool` 结果是否
成对出现、顺序是否正确，不满足就返回 400。真实服务商不满足这个条件也会直接
400。它能证明「请求中途失败后把半截历史清掉」这件事真的做对了——如果清不干净，
下一轮请求就会被它拦下来。

唯一第三方依赖是 `openai`，装在 `.venv` 里。涉及 `main.py` 的本地测试都用 venv 的
Python 跑（`test_loop.py` / `test_permissions.py` 要 `import main`，间接要 `openai`）：

```bash
.venv\Scripts\python.exe tests\test_sandbox.py   # 沙盒边界 + 注册表一致性
.venv\Scripts\python.exe tests\test_loop.py      # 重复调用检测 + 协议合法性
.venv\Scripts\python.exe tests\test_permissions.py # Phase 10 审批与拒绝链路
```

`tests/inputs/` 里放的是喂给 `main.py` 的 stdin 输入文件，用来复现某次实测：

```bash
cmd /c '.venv\Scripts\python.exe main.py < tests\inputs\real_retest_v2.txt'
```

## 写文件的边界和覆盖策略（刻意的设计选择）

`write_file` 复用 `read_file` 同一套沙盒校验，四类越界一律 `PermissionError`：
`../` 跳级、`..\\` 反斜杠、绝对路径（`C:/evil.txt`、`/etc/passwd`）、符号链接。
校验发生在 `mkdir(parents=True)` **之前**，所以自动创建的父目录也保证在沙盒内。
没有删除文件的能力，也没有执行程序的入口。

权限检查在 Runtime 的 `run_tool_round` 中、真正调用 `write_file` 之前发生。它先复用
`resolve_inside_workspace` 做 Sandbox 预检，再根据目标文件是否存在显示 `CREATE` 或
`OVERWRITE` 并调用审批 callback；因此用户批准也不能越过沙盒。`write_file` 本身不包含
审批或 `input()`，只负责在 Runtime 放行后写入。

**允许覆盖已有文本文件**，这是刻意的：沙盒已经把破坏范围锁死在一个专用目录里；
而拒绝覆盖会让「重跑同一个任务」直接失败，还得再引入一个 `force` 参数让模型学习怎么绕过——
那比覆盖本身更复杂。为了让覆盖不静默，返回值会明确说
`已写入 xxx.md（304 字节，已覆盖已有文件）` 或 `（304 字节，已写入新文件）`。
本项目不做版本控制，覆盖前不自动备份。

`apply_patch(path, old_text, new_text)` 是已有文件的小范围修改入口。它先通过同一条
`resolve_inside_workspace` 沙盒校验和 `SIDE_EFFECT` 审批，再以 UTF-8 文本做字面匹配：
匹配 0 次或超过 1 次都返回 `[工具失败]` 且保持文件不变，只有恰好 1 次才写入。
不做 fuzzy matching、正则、AST 猜测或自动空白修正；`new_text` 为空可用于局部删除。
读取和写入会保留文件的 LF/CRLF 换行风格。返回值只报告路径、替换次数和 old/new 字符长度，
不会把整份新文件重新塞回上下文。`write_file` 仍保留给新建文件或必要的整文件覆盖。

## Phase 11：Tool Registry 与统一 Permission Policy

### 1. 什么是 Tool Registry

`TOOL_REGISTRY` 是 Runtime 的正式工具注册表。每个名字对应一个
`ToolDefinition`，里面同时放着：

- `name`：模型 Tool Call 使用的名字
- `schema`：发给模型的 OpenAI-compatible Tool Schema
- `handler`：真正执行工作的 Python 函数
- `risk_level`：Runtime 用来决定权限路径的风险等级

`AVAILABLE_TOOLS` 不再是独立登记表，而是从 Registry 生成的 Schema 列表。
因此 Schema、Handler、Risk 不会因分别维护而漂移。

### 2. Risk level 和 approval mode 的区别

`risk_level` 是工具自身声明的能力风险：`READ_ONLY` 表示只读，`SIDE_EFFECT`
表示会改变状态；`EXECUTION` 和 `EXTERNAL_SIDE_EFFECT` 是统一的风险 metadata。
它回答「这个工具是什么性质」。

`ASK`、`ALLOW`、`DENY` 是本次运行选择的审批策略，回答「遇到需要审批的风险时
怎么处理」。因此 `READ_ONLY` 不请求审批，`SIDE_EFFECT` 才进入现有策略；用户批准
仍不能越过 Sandbox 预检。

### 3. 为什么 Runtime 不应该认识 `write_file`

如果 Permission Runtime 判断 `if tool_name == "write_file"`，每增加一个有副作用的
工具就必须修改 Runtime，容易漏掉风险规则。现在 Runtime 只查 Registry 中的
`risk_level`；测试注册的 `mock_side_effect` 即使名字完全不同，也会自动走同一条
审批路径。测试工具不会进入 `AVAILABLE_TOOLS`。

这还不是 MCP：这里仍是本地 Python 函数、项目自己的 JSON Schema 和本地 Registry。
没有远程 Tool Server、MCP 握手、传输协议或跨进程能力发现。

## Phase 12：Controlled Command Execution

`run_command(command, args=[], cwd=".")` 是第一个 `EXECUTION` Tool。它不接受完整
shell script，而是接收程序名和参数数组，并始终使用 `subprocess.run(..., shell=False)`。

第一版 Command Policy 只允许：

- `python -m pytest ...`
- `python -m unittest ...`
- `git status`、`git diff`、`git log`

`python -c`、`python -m pip`、写入型 Git 子命令、PowerShell、cmd、bash、网络命令、
未知程序和 shell 操作符都会被拒绝。`cwd` 解析后必须位于 `WORKSPACE_DIR` 内，且
Sandbox 预检发生在 Permission callback 之前。

`EXECUTION` 自动复用 Phase 11 Permission Runtime：交互式运行默认 `ASK`，测试和自动
入口使用 `ALLOW` / `DENY`。拒绝时不会启动 subprocess，但仍回传合法 `role="tool"`
结果。结果包含 `Command`、`CWD`、`Exit code`、`Timed out`、`STDOUT` 和 `STDERR`；
默认超时为 30 秒，stdout/stderr 各有独立输出上限，超时和非零退出都会作为正常 Tool
Result 返回给模型。

2026-09-21 的 Phase 12.5 真实验证已证明完整核心链路：模型主动请求 `run_command`，
`EXECUTION` 获 `ALLOW`，`python -m unittest tests.test_long_file -v` 实际运行并通过 10 项
测试，模型随后给出 Final Answer。后续已修复 `cli.py` 的 Windows GBK stdout 问题，并用
ASCII、中文、emoji 及中文+emoji+JSON 回归验证输出内容完整保留，Phase 12 真实闭环正式关闭。
详见 `REAL_RUN_LOG.md`。

## Phase 13：Bounded Coding Loop

Phase 13 用一个隔离的 `tests/fixtures/coding_workspace/` 小项目验证有限 Coding Loop：

```
read_file → write_file / apply_patch → run_command（测试）→ Tool Result → 再决定 → Final Answer
```

Runtime 没有新增 Planner、Coding 状态机或自动修复分支。测试进程退出码 `1` 仍是正常
`Tool Result`，模型可以读取 stdout/stderr 和 `exit_code` 后决定下一次修改；只有 Runtime
异常才会被当作工具失败。非零 `run_command` 结果不会进入成功重复调用集合，因此修改后
可以再次运行同一条测试命令。

本阶段增加了任务级内存 Trace（模型轮次、工具参数摘要、审批、结果摘要、退出码、写入目标、
调用计数和 token 汇总），并继续使用已有 `MAX_AGENT_STEPS` 作为上限保险丝。`write_file`
仍是 `SIDE_EFFECT`，`run_command` 仍是 `EXECUTION`，三者都必须经过现有 Permission Runtime；
fixture 仍只能位于 `WORKSPACE_DIR` 内。Mock A 验证一次修复，Mock B 验证失败后第二次修复，
Mock C 验证撞上限时停止；一次真实小任务也已读、写、测试并给出 Final Answer。

## 工具结果长度保护

`config.py` 里 `MAX_TOOL_RESULT_CHARS = 4000`。超过就截断，并明确告诉模型
「已截断、原始内容多少字符、省略多少字符」，**不静默丢内容**——
静默截断会让模型以为拿到的是全文，然后基于不完整信息下结论。

裁在 `main.py` 的 `execute_tool_call` 里，那里是所有工具结果的唯一出口，
所以现在的工具和以后任何新工具自动都有保护；工具本身不需要知道模型的上下文预算。
用字符数而不是 token 数：数 token 要引 tokenizer、要装依赖，
对「别把上下文撑爆」这个目的完全没必要。

## 项目结构

```
mini-agent-lab/
├── main.py               # 程序入口：聊天循环 + Agent 循环 + 执行工具并回喂模型
├── config.py             # 配置：读 .env，产出模型连接信息、沙盒目录、最大步数、结果上限
├── session.py            # Session JSON 的创建、保存、加载和 canonical message 校验
├── tools.py              # 工具层：工具 Schema/Handler/Policy + 沙盒校验 + Registry
├── requirements.txt      # 唯一第三方依赖：openai
├── README.md             # 本文件
├── REAL_RUN_LOG.md       # 真模型实测记录（含 Phase 5.5、Phase 6、Phase 9、Phase 10、Phase 12.5、Phase 13）
├── .env.example          # 环境变量模板，复制成 .env 再填 Key
├── .gitignore            # 保证 .env 和 __pycache__ 不进版本库
├── eval/                 # Phase 6.5：轻量 Eval（不新增 Agent 能力，只测稳定性）
│   ├── tasks.json        # 任务集：6 个基准任务 + Task 1 重复 2 次（共 8 个）
│   ├── run_task.py       # 单任务执行器：驱动 main.py + 收集指标 + 判定 success
│   ├── run_eval.py       # 总控：工作目录快照隔离 + 跑完全部任务 + 失败分类
│   ├── test_metrics.py   # 20 个单测：只测「尺子准不准」，不发任何网络请求
│   ├── results.json      # 最近一轮 Eval 的完整原始数据
│   ├── REPORT.md         # 人类可读报告：为什么稳、哪里不稳、下一步该修什么
│   └── .run_log.txt      # 过程日志（已 gitignore，每次跑会重新生成）
├── tests/
│   ├── mock_server.py    # 本地假服务器，没有 Key 也能验证接线
│   ├── test_sandbox.py   # 沙盒边界 + 注册表一致性单元测试
│   ├── test_loop.py      # 重复调用检测 + 工具调用协议合法性
│   ├── test_session.py   # Session 保存/恢复、密钥排除和上下文视图测试
│   ├── test_long_file.py # Phase 9 分段读取、完整行和 mock 分页测试
│   ├── test_permissions.py # Phase 10 审批、拒绝、协议和 Mock Agent 测试
│   ├── test_coding_loop.py # Phase 13 fixture、Mock A/B/C 和边界测试
│   ├── test_apply_patch.py # Phase 16 精确 patch、权限、重复和 Coding 闭环测试
│   ├── fixtures/coding_workspace/ # 隔离的 calculator.py + test_calculator.py fixture
│   └── inputs/           # 喂给 main.py 的 stdin 输入，用来复现某次实测
├── sessions/             # 本地 Session JSON（已加入 .gitignore，不提交实际会话）
└── demo_workspace/       # Agent 唯一允许读写的工作目录（沙盒）
    ├── agent_notes.md    # Agent 学习笔记
    ├── python_notes.md   # Python 速记
    └── todo.txt          # 任务清单
```

为什么 `demo_workspace/` 是独立的：Agent 要能读文件，就已经有泄露能力了。
把它关在一个专用沙盒里，你才能放心地让它试错。

## 现在有了能力，但「智能」还在外面

Phase 6 之后，Agent 已经能**自己把一件事做完并且自己收口**：看目录、挑文件、读、
整理、写回、判断目标已满足、汇报。新增的三层防线都没动 Agent 循环——
`main.py` 里连一个工具名都没写，工具集也没有增加。

| 仍然缺失 | 现状 |
|---|---|
| 模型的选择不可靠 | 挑哪个文件、读几个、要不要再读，全靠模型判断。它可能漏读关键文件，也可能读了不相干的 |
| 只拦「完全相同」的重复 | 重复检测认的是工具名 + 规范化参数全等。模型换成不同参数原地打转（读 A、读 B、再读 A 的同类变体）不会被发现，仍只靠步数上限兜住 |
| 没有 Memory | Session 只恢复同一段 canonical 对话，不做跨 Session 搜索、合并或自动记忆 |
| 没有工具返回值校验 | 工具结果整段塞回模型，除了长度上限，没有检查它是不是模型能用上的 |

这四处刻意不补。Phase 6 只解决「做完了不知道自己该停」这一件事本身；
往里塞记忆、做更聪明的打转检测，出了问题会分不清是哪一层。

## Phase 6.5：Eval / 稳定性验证

**这个阶段没有加任何 Agent 能力。** 没有新工具、没有记忆、没有 RAG、没有压缩、
没有 GUI。`main.py` 一个字都没改——Eval 只**驱动**它，不复写它的逻辑，
所以测出来的东西就是你在终端里手打时会遇到的东西。

要回答的问题只有一个：**Phase 6 已经跑通过一次了，那它到底稳不稳定？**

### 怎么跑的

```bash
.venv\Scripts\python.exe eval\test_metrics.py   # 20 个单测，不发网络请求，先证明「尺子」是准的
.venv\Scripts\python.exe eval\run_eval.py eval\tasks.json   # 真跑一轮 Eval（8 个任务）
```

三个设计决定：

1. **一个任务一个进程。** `config.WORKSPACE_DIR` 在 import 那一刻就定死了，同进程改不了。
   所以「每个任务都从同一份干净工作目录开始」只能在进程之外做：
   `run_eval.py` 先把 `demo_workspace/` 备份成快照，每个任务开跑前清回快照，
   再起一个子进程跑。不隔离的话，第 3 次跑同一个写文件任务会因为「文件已存在」
   而产生完全不同的行为——那测的不是模型，是文件残留。

2. **success 只用机器能验证的规则，不用另一个 LLM 打分。**
   要求最终回答 → 历史里有没有一条不带 `tool_calls` 的 assistant 消息；
   要求写文件 → 文件在不在、是不是空的、写到指定路径没有；
   要求指定路径 → 是不是那个路径；不该撞步数上限 → 撞了没有；
   不该报错 → 有没有异常。
   至于「摘要写得好不好」「比较说得对不对」这类主观指标，
   **一律不给分，只记 `needs_manual_review = true` 留给人看。**

3. **指标是照着历史回放出来的，不是现场数。**
   `run_agent_loop` 把「模型提了调用」和「调用结果」严格成对写进历史，
   而且重复调用的判定只依赖两个输入——工具指纹、已执行过的集合——
   所以走一遍历史就能还原每一次调用当时是「真执行」「被拦」还是「失败」。
   回放顺序必须和 `main` 一致：先看指纹在不在集合里，再更新集合；
   失败调用不进集合（否则模型换参数重试会被当成重复）。

### 这一轮的结论（详见 `eval/REPORT.md`）

8 个任务**全部成功**，0 撞步数上限，0 编造文件，31 次工具调用里 0 次重复被拦。

但同一个任务跑三次，token 消耗是 9,172 / 5,884 / 12,962 —— **最大比最小 2.20 倍**。

| 结论 | 依据 |
|---|---|
| 结果稳定 | 8/8 成功，最终回答都在，编造文件 0 次 |
| 过程不稳定 | 同一任务 token 方差 2.20 倍，模型调用数在 4～6 浮动 |
| 钱花在哪 | 46,011 token 里 **40,816（88.71%）是 prompt** |
| 为什么越来越贵 | 8 次运行的每轮 `prompt_tokens` **严格单调递增，无一例外** |
| 自动判定够不够用 | **7/8 任务必须人工看**；Task 3「技术上正确但实质不完整」被规则漏掉 |

上下文增长的机制（本轮只记录，不解决，是以后 Context Management 阶段的证据）：

- 第 1 轮是固定底数 831～859 token（波动 1.7%），只有 SYSTEM_PROMPT + 工具 Schema + 任务文本。
- 第 2 轮只涨 80～104，那是加上模型那句工具调用和一个小结果。
- 跳得大的轮次都是读完整文件的轮次：读 `agent_notes.md` 约 +437，读 `python_notes.md` 约 +911。
- 原因是 Agent 的工作方式就是「工具结果回喂模型」——历史写进去之后，
  **之后每一次请求都要把整条历史原样重发一遍**。读一个大文件不只那一次贵，
  它让后面每一轮都永久变贵。

两个顺带观察：

- **重复调用拦截功能 0 次触发。** Phase 5.5 修的那个「相同 Tool Call 反复执行」
  在这 8 次运行里根本没出现过——它是那次运行的偶发问题，不是系统性问题。
  功能本身正确（19 个单测覆盖），只是没被真正需要过。
- **步数上限余量只有 2**：最多用到第 6 轮，上限是 8。稍微复杂一点的任务就会逼近上限。

## Phase 14：Coding Task Contract / Machine-Verifiable Acceptance

Phase 13 的 Trace 记录 Agent 怎么做，但 Trace 和 Final Answer 都不是任务成功本身。
Phase 14 增加独立的 `acceptance.py`，用结构化 Contract 和确定性 Verifier 判断最终状态。
它不调用 LLM、不读取 Session、不进入普通 Agent Loop，也没有新增 Agent Tool。

最小 Contract 形状如下；`instruction` 是给模型看的任务文本，其余字段是 Runtime / Eval
独立读取的事实：

```json
{
  "task_id": "calculator_fix",
  "instruction": "修复 calculator.py，让对应测试通过",
  "allowed_paths": ["calculator.py"],
  "test_command": {
    "command": "python",
    "args": ["-m", "unittest", "test_calculator", "-q"],
    "cwd": "."
  },
  "require_test_pass": true
}
```

Prompt 只能告诉模型规则，不能证明模型遵守了规则。Verifier 在任务开始做文件快照，
结束时比较 SHA-256，得到 `changed_files` 和 `unexpected_changes`；普通新增、删除、
修改都能检测，Python 运行时生成的 `__pycache__` / `.pyc` 不计入源码变化。随后它用
Contract 中固定的命令，通过现有 `run_command` 的安全策略独立重跑最终测试，不相信 Agent
Trace 里的旧 `exit_code`。

只有以下条件全部满足才是 `accepted=true`：有 Final Answer、没有 `MAX_AGENT_STEPS`、
没有 Runtime exception、没有越界修改、Verifier 最终测试退出码为 0。结果同时返回
`agent_ran_required_test`、`final_test_exit_code`、`final_test_passed`、`reasons` 等证据。

Mock A（只改 `calculator.py`，最终测试通过）PASS；Mock B（改测试让测试通过）因
`unexpected file changed: test_calculator.py` FAIL；Mock C（有 Final Answer 但最终测试失败）
因 `final_test_failed` FAIL。这样明确区分了「模型说完成」「测试曾经通过」和「最终任务被接受」。

Contract 单任务入口：

```powershell
$env:AGENT_WORKSPACE = "C:\\path\\to\\coding_workspace"
$env:TOOL_APPROVAL_MODE = "ALLOW"
.venv\\Scripts\\python.exe cli.py --contract tests\\fixtures\\coding_contract.json
```

普通 `--task` 入口仍保留原有行为；只有传入 Contract 时，JSON 结果才附带独立 `acceptance`
对象。Phase 14 本地共 66 项测试通过；一次真实模型运行的详细结果见 `REAL_RUN_LOG.md`，
模型修复了允许文件且最终测试通过，但撞步数上限、没有 Final Answer，所以被 Verifier 正确拒绝。

## Phase 15：Coding Completion & Budget Control

Phase 15 保持 `MAX_AGENT_STEPS = 8`，解决的是“产物已经正确但 Agent 继续调用工具”的可观测性与
收口提示，不把步数上限改大，也不让 Runtime 代替模型生成 Final Answer。

Coding Task Trace 现在给每个 Tool Call 标注：`PRODUCTIVE`、`BLOCKED_DUPLICATE`、
`POLICY_REJECTED`、`FAILED_COMMAND` 或 `SUCCESSFUL_COMMAND`，并汇总
`executed_tools`、`duplicate_blocked`、`policy_rejected`、`failed_commands` 和
`successful_commands`。Contract 验收结果另外拆成：

- `artifact_passed`：没有越界文件变化，且独立最终测试满足 Contract；
- `interaction_completed`：有 Final Answer，且没有撞 `MAX_AGENT_STEPS`；
- `accepted`：两者都成立，并通过其余现有必要条件。

Contract 模式会给模型一段很短的目标文件、严格 required test 和收口规则。只有严格匹配
Contract 的 `command + args + cwd` 且 exit code 为 0 时，Tool Result 才附加 Completion Hint；
普通成功命令不会被误报为任务完成。Verifier 仍然独立拍快照并重跑固定测试。

Mock A（成功测试后 Final）、Mock B（失败测试后修复再测）、Mock C（重复调用）、Mock D（策略
拒绝后恢复）均按预期工作；Mock E（顽固模型）仍由 8 步保险丝停止。Phase 15.5 baseline
本地全量为 68 项测试；Phase 16 增加 20 项 patch 测试后，全量为 88 项并通过。Phase 16
真实模型使用 `apply_patch` 完成 calculator fixture，详见 `REAL_RUN_LOG.md`。

## Phase 16：Patch-based Editing

Phase 16 增加最小的局部编辑能力，不改变现有 `write_file` 职责，也没有实现 Git unified diff、
AST rewrite、fuzzy matching、Planner、MCP 或 Patch Context Compression。

```text
apply_patch(path, old_text, new_text)
```

唯一匹配规则：`old_text` 出现 0 次时返回“目标文本不存在”；出现超过 1 次时返回“目标文本不唯一”；
只有恰好 1 次才替换。两种失败都是正常 `role="tool"` 结果，文件保持不变，模型可以重新
`read_file → apply_patch`。成功结果包含 path、`replaced occurrence count = 1`、old/new 字符长度。

`apply_patch` 注册为 `RiskLevel.SIDE_EFFECT`，沿用 `TOOL_REGISTRY`、`resolve_inside_workspace`、
`ASK/ALLOW/DENY`、重复调用保护、Session canonical history 和 Acceptance 的 changed_files 逻辑。
Verifier 不依赖具体编辑工具。Trace 额外记录 `apply_patch_calls`、`patch_successes` 和
`patch_failures`；patch 参数暂不做专门压缩，先记录真实大小。

本地验证覆盖：唯一/0/多匹配、局部删除、中文、多行、CRLF、沙盒越界、ALLOW/DENY、重复调用、
失败重试、Session、Context、Mock A-D、`patch → test → Final → Acceptance`，以及既有长文件、
Command、Completion Control 和 Verifier 回归；全量 `87` 项测试通过。

Phase 16 的一次真实样本中，模型选择 `apply_patch` 而非 `write_file`，最终
`artifact_passed=true`、`interaction_completed=true`、`accepted=true`。这是单次观察，不能推出
统计结论；本 fixture 很小，patch 的 old/new 两段合计字符数反而可能大于完整文件，优势主要在真实
大文件中避免重新生成未修改内容和降低误覆盖范围。

## Phase 17：Repository Navigation / Code Search

Phase 17 增加一个只读的固定字符串搜索工具：

```text
search_text(query, path=".", max_results=20)
```

`list_files` 只回答“这一层有什么”，`search_text` 递归回答“这个字符串出现在哪些文件的哪几行”，
然后模型再用 `read_file` 精读候选文件。第一版只做 literal search，不引入正则、Embedding、RAG、
AST、LSP、MCP、Planner 或自动改写 query。0 matches 是正常 Tool Result，模型可以自行换关键词、
缩小 path 或结束任务。

搜索路径沿用 `resolve_inside_workspace()`，拒绝 `..`、越界绝对路径和搜索根的外部符号链接。默认
忽略 `.git`、`.venv`、`__pycache__`、`sessions`、`eval/runs` 及常见 cache 目录；不能按 UTF-8
读取或含 NUL 的文件直接跳过。结果主动限制为最多 100 个匹配、每个匹配一行前后一行上下文，并返回：

```text
matches_shown
matches_total
truncated
```

`search_text` 注册为 `RiskLevel.READ_ONLY`，复用现有 Registry / Permission / Session / Context 链，
不增加工具名特判。新增多目录 `repo_fixture`：用户只说“修复 calculate_discount 的错误”，模型即可
`search_text → read_file → apply_patch → run_command → Final`，Acceptance 只允许修改
`src/pricing.py`。

本地验证：`python -m unittest discover -s tests -q` 共 104 项通过，1 项 Windows 符号链接能力测试因
运行环境权限跳过；其余旧的 Patch、Command、Permission、Session、Context、Completion Control 和
Acceptance 测试均通过。真实 Provider 最小请求成功，真实 Coding Task 也完成并被 Verifier 接受；模型
本次选择了 `list_files` 而不是 `search_text`，详见 `REAL_RUN_LOG.md` 和 `eval/phase17_real_run.json`。

## 阶段完成情况

每一步都需要你确认才继续，不会自动往下走。

- **Phase 1 · 接上 LLM** ✅ —— 读 `.env`，让模型回一句话。证明「能问能答」。
- **Phase 2 · 第一个工具** ✅ —— `read_file` 已声明，模型能决定去读哪个文件。
- **Phase 3 · 工具执行** ✅ —— 解析工具调用并真正执行，把结果回喂给模型。
- **Phase 4 · Agent 循环** ✅ —— 装进循环，一个任务能连续跑多步，带最大步数保护。
- **Phase 5 · 多工具 Agent** ✅ —— `list_files` / `write_file` + 工具注册表 + 工具结果长度保护。
- **Phase 5.5 · 真模型首次实测** ✅ —— 不换代码，只把 Phase 5 指向真实模型跑同一个任务。
  结论：8 步耗尽、无 Final Answer，任务是「做完了不知道收手」。见 `REAL_RUN_LOG.md`。
- **Phase 6 · 完成判断 / 循环控制 / 可观测性** ✅ —— 不新增 Agent 能力，
  只在同一个循环上叠三层防线：提示词收敛原则、重复调用检测、`MAX_AGENT_STEPS`；
  并把 `finish_reason` 和 token 用量打印出来。同一任务复测：5 步收口，正常 Final Answer。
- **Phase 6.5 · Eval / 稳定性验证** ✅ —— 仍然不新增 Agent 能力，`main.py` 一字未改。
  建了 `eval/tasks.json`（8 个任务）+ 总控 + 19 个单测，跑了一轮真模型。
  结论：**结果稳定（8/8 成功），过程不稳定（同一任务 token 差 2.20 倍）**。
- **Phase 7 · Context Management** ✅ —— 增加 `OFF / WRITE_ONLY / FULL` 模式，默认
  `WRITE_ONLY`；canonical history 与发给模型的上下文视图分离，并完成离线基准与受控验证。
- **Phase 8 · Session Persistence** ✅ —— 增加 `sessions/<session_id>.json` 保存/恢复，
  `python main.py --resume SESSION_ID` 恢复同一 Session；保存前校验 tool-call 配对，
  不保存 API Key，不进入 Memory。
- **Phase 9 · Long File Reading** ✅ —— 扩展现有 `read_file` 的 `start_line` / `max_lines`，
  只返回安全预算内的完整行，并由实际返回范围生成 `has_more` / `next_start_line`；
  Python 不自动翻页，保留全局结果长度兜底。mock、多段本地测试和一次真实模型验证均通过。
- **Phase 10 · Tool Permission / Side-effect Approval** ✅ —— 增加 `READ_ONLY` / `SIDE_EFFECT`
  风险分类和 Runtime 审批层；`ASK` / `ALLOW` / `DENY` 通过可注入 callback 选择策略。
  审批前仍先做 Sandbox 校验，批准才执行 `write_file`，拒绝仍回传合法 Tool Result。
- **Phase 11 · Generalized Tool Capability / Permission Policy** ✅ —— 用统一的
  `ToolDefinition` / `TOOL_REGISTRY` 收拢 Schema、Handler 和 `risk_level`；`AVAILABLE_TOOLS`
  从 Registry 派生，Permission Runtime 不再依赖具体工具名。增加仅测试使用的
  `mock_side_effect` 验证通用审批。
- **Phase 12 · Controlled Command Execution** ✅ —— 增加 `run_command`，以 `shell=False`
  执行受限的 Python 测试和 Git 只读命令；增加 workspace cwd 预检、命令审批、超时、
  stdout/stderr 截断、非零退出结果和本地/Mock 测试。
- **Phase 13 · Bounded Coding Loop** ✅ —— 保留普通 `LLM → Tool Call → Tool Result → LLM`
  循环，用隔离 calculator fixture 验证 `read → write → test → Final`、测试失败后的二次修复、
  Permission、Sandbox、Session/Context/Long-file/Command 回归和 `MAX_AGENT_STEPS` 兜底；
  增加最小任务 Trace，没有引入 Planner 或 Coding 状态机。
- **Phase 14 · Coding Task Contract / Machine-Verifiable Acceptance** ✅ —— 增加结构化
  Contract、文件快照差异和独立最终测试 Verifier；Mock A/B/C/D、增删文件、命令安全策略和
  全量 66 项本地测试通过。真实模型样本被正确判为未接受，暴露出当前 Agent 收口仍受步数上限影响。
- **Phase 15 · Coding Completion & Budget Control** ✅ —— 增加 Tool Call 分类、产物/交互双状态、
  Contract 收口 Guidance、严格 required-test Completion Hint 和 Mock A-E 回归；保持独立 Verifier
  与 `MAX_AGENT_STEPS = 8`。本地 68 项测试通过；Phase 15.5 真实模型对照已 accepted。
- **Phase 16 · Patch-based Editing** ✅ —— 增加唯一精确匹配的 `apply_patch`，复用 Permission / Sandbox /
  Duplicate / Session / Contract 链路；Mock A-D、一次真实 calculator Coding Task 和全量 88 项测试通过。
- **Phase 17 · Repository Navigation / Code Search** ✅ —— 增加只读 literal `search_text`，复用 Registry /
  Sandbox / Permission / Session / Context；多目录 fixture 的 Search → Locate → Read → Patch → Test →
  Final → Acceptance Mock 闭环通过，全量 104 项测试通过（1 项 Windows 符号链接测试跳过）。真实模型请求
  因 Provider 连接错误未进入 Agent，未伪造真实调用或 token 结论。

Phase 16 已收尾；下一阶段只做分析，不在本阶段自动开始。

> **一处有意的偏离**：原计划把「真正拦截沙盒之外」放在 Phase 5，
> 实际在 Phase 2 就和 `read_file` 一起做掉了。
> 理由：`read_file` 是第一个吃路径的工具，若不带边界，Phase 3 一执行
> 就能把 `.env` 里的 API Key 读进模型上下文——那是先按构造引入泄密能力，
> 再打算以后再补。边界必须和第一个吃路径的工具同时出生。

## 约定

### 单任务入口与最新评测

```powershell
.venv\Scripts\python.exe cli.py --task "读取 agent_notes.md 并总结"
```

运行日志输出到 stderr，结果 JSON 输出到 stdout。`completed` 返回退出码 0，
`incomplete` / `failed` 返回 1，`cancelled` 返回 130。完成表示模型给出了最终回答，
并不代替产物验收。已完成的文件写入不会因取消或请求失败自动撤销。
交互式聊天仍使用 `main.py`。

可通过进程环境变量 `AGENT_WORKSPACE` 指定工作目录，默认仍为 `demo_workspace`。
该变量在模块加载时读取，应在启动 Python 前设置。不要将密钥目录用作工作目录。

可通过 `TOOL_APPROVAL_MODE=ASK|ALLOW|DENY` 选择副作用审批策略；未设置时交互式 CLI 默认为
`ASK`。自动化评测应显式设置 `ALLOW`，拒绝路径测试显式设置 `DENY`，避免等待键盘输入。

评测现已改为独立临时工作区，每题保存结果与输出内容；运行及验收说明见
[eval/RUNNING.md](eval/RUNNING.md)。历史 8/8 只代表旧规则，不代表新规则全部通过。

- Agent 只能读写配置的工作目录（默认 `demo_workspace/`）内的文件，路径边界由代码强制执行。
- `.env` 永不进版本库。
- 每个阶段先跑通，再进下一阶段。

## Phase 18 · Repository Navigation Eval

Phase 18 新增独立评测 harness，不新增 Agent Tool，也不修改 `search_text` 的 Prompt 或描述。
`eval/navigation_fixtures.py` 确定性生成 SMALL（7 文件）、MEDIUM（25 文件）和
LARGE-SYNTHETIC（75 文件）fixture；`eval/navigation_metrics.py` 从真实历史中的 tool call/result
计算 `first_correct_file_turn`、目录/搜索/读取次数、candidate files 和 token 成本，不使用 LLM Judge。

真实入口是 `python -m eval.navigation_eval`，结构化结果为 `eval/navigation_results.json`，报告为
`eval/NAVIGATION_REPORT.md`。四个有效场景各运行一次并被接受；首次使用未监听的 `127.0.0.1:7897`
未进入 Agent loop，随后使用 Phase 17 已验证的 `127.0.0.1:9674` relay 完成有效运行。离线 fixture、
ground truth、search/read、Coding Contract 和全量回归通过；完整记录见 `REAL_RUN_LOG.md`。

## Phase 18.5 · Repository Navigation Stability Check

Phase 18.5 在 Phase 18 的 `0aaae56` 基线之上，仅重复 SMALL / MEDIUM / LARGE-SYNTHETIC 三个 symbol 场景，
每个场景新增 2 次真实运行，并与基线合并为 `n=3`。Agent runtime、Tool schema、Prompt、上下文/验收逻辑均未修改；
评测代码、原始结果和报告分别见 `eval/navigation_stability.py`、`eval/navigation_stability_results.json` 和
`eval/NAVIGATION_STABILITY_REPORT.md`。

六次有效新增运行均无 Provider failure。SMALL 三次均为 `search_text → read_file → Final`；MEDIUM 三次均先
`list_files ×3` 再 `search_text`，其中 2/3 被接受，1 次因达到 max steps 且缺少 Final Answer 未被接受（但产物与最终
测试均通过）；LARGE 三次均使用 `search_text`，3/3 被接受。一次早期稳定性 harness 在 LARGE 新运行上超时，因未
成功序列化而单独记录在 `eval/navigation_stability_prior_failures.json`，不混入 n=3 模型统计，也不重写为模型失败。

综合结论：SMALL 的搜索路径稳定；MEDIUM 的仓库导航稳定但 Coding 收尾存在路径/步数波动；LARGE 的搜索使用稳定，
具体 tool chain 仍是多路径。当前没有足够证据新增 Navigation Guidance，下一步继续做受控重复观察即可。

## Phase 19 · Required-Test Visibility Experiment

Phase 19 只修改评测层，Runtime、Tool Schema、Completion Hint、Acceptance、MAX_AGENT_STEPS、Permission、
Sandbox 和 Context 均保持不变。Control 直接复用 Phase 18.5 的 MEDIUM 三次结果；Treatment 使用相同的
MEDIUM fixture 和任务，只在模型上下文中增加事实性的 `Required test command: python -m unittest discover -s tests -p test_discount.py -q`，
没有增加“必须 Final”或“优先执行”等指导。

Treatment 计划运行 3 次，实际有 2 次有效运行和 1 次 `APIConnectionError` provider failure；有效两次均执行
exact required test、触发 Completion Hint、给出 Final Answer 并被接受。Control 为 exact test `0/3`、Hint `0/3`、
Final `2/3`、accepted `2/3`、MAX_AGENT_STEPS `1/3`；Treatment 有效分母为 `2`，对应指标均为 `2/2`。
该 n=3 实验只说明可见性与更稳定的收口行为一致，不单独证明因果关系。

评测入口为 `python -m eval.required_test_visibility`；结果为 `eval/required_test_visibility_results.json`，报告为
`eval/REQUIRED_TEST_VISIBILITY_REPORT.md`。完整 raw result、实际 run_command、zero-test 和 provider failure 记录均保留在结果文件中。
Phase 19 专项测试与 Navigation/稳定性测试通过；全量回归为 `117` 项通过、`1` 项 Windows 符号链接测试跳过。

## Phase 19.5R · Provider Recovery & Resume 启动方式

Recovery harness 必须从 repo root 以 module invocation 启动，并显式把 `eval` 放入
`PYTHONPATH`，以兼容现有 eval 模块的顶层导入；不修改 Runtime 或 Python import 逻辑：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "eval")
.venv\Scripts\python.exe -m eval.required_test_visibility_recovery
```

启动 smoke test 使用 `--help`，不会发起真实模型请求。

## Phase 21 · Explicit Task Finish + Deterministic Finish Gate

Phase 21 把 Coding Task 的“完成”从模型的普通文本收口，改成 Runtime 可判定的一次显式请求。
模型仍然自己决定何时收口，但“是否真的完成”不再由自然语言推断，而由一份确定性规则决定。

### 1. `finish_task(summary=...)`

新增第七个工具 `finish_task`，唯一参数 `summary`（非空字符串），是接受时被展示给用户的最终说明。
它不执行任何文件操作、不跑测试、不询问用户。Tool Registry 里的 `tool_kind` 把它标为
`CONTROL_FLOW`，与 `risk_level` 正交：前者描述它参与循环的方式，后者描述它的副作用风险。

三个隔离点，任一都不足以单独保证正确：

1. `run_tool_round` 在普通工具分发之前先查 `tool_kind`，命中就交给专用分发器，`break` 结束本轮。
2. `check_tool_permission` 对 `CONTROL_FLOW` 直接放行，因此它永远不进普通 Approval。
3. `execute_tool_call` 对 `CONTROL_FLOW` 返回失败提示，作为最后一道防线。

它也不进重复调用指纹表：finish 的结果从不写入 `executed`，所以
`finish_task("done") → rejected → run required test → PASS → finish_task("done")`
这条链路必须成立，第二次完全相同的调用不会被拦成“无新信息”。

### 2. Control Flow 分发与本轮截断

一个 assistant response 里可以并列多个 tool call。Runtime 只处理到**第一个** `CONTROL_FLOW` 调用为止，
`tool_calls_through_control_flow` 返回这段前缀，并把它作为 assistant message 写回历史。

这解决的不只是执行顺序：被截掉的调用如果写进 canonical history，就会留下没有对应 tool result 的
`tool_call_id`，下一次请求会被提供商直接 400。所以截断同时作用于「执行」和「历史记录」两处，
`session._validate_messages` 的配对校验会兜住任何漏网情况。

Gate 拒绝也一样：`finish_task` 后面的 read / run 本轮不执行。它们不是被丢弃的错误，
而是等模型在下一轮重新提出。

### 3. Task State

`acceptance.TaskState` 是一份极小的持久化生命周期，刻意不用时间戳：

```
status                            RUNNING | FINISHED | LIMIT_REACHED | ERROR
event_seq                         任务内单调逻辑时钟，每个工具尝试消耗一个
last_mutation_event_seq           真正改变 workspace 的那次事件序号
last_successful_exact_required_test_seq   成功 exact required test 的事件序号
finish_message                    Gate 接受时写入，作为对用户的最终回答
last_finish_rejection             最近一次被拒的尝试（含 reasons）
unresolved_runtime_error          Runtime 级失败
finish_attempts                   全部尝试，保留拒绝历史
initial_snapshot                  任务开始时的 workspace 摘要
```

关键在比较 `last_successful_exact_required_test_seq > last_mutation_event_seq`：
它比“测试时间戳晚于写入时间戳”稳得多，测试重跑、时钟回拨都不影响判断。

### 4. Finish Gate

`acceptance.evaluate_finish_request(contract, task_state, workspace)` 是纯函数式的判定：
只读 Contract、TaskState 和 workspace 摘要，不调模型、不改文件、不跑测试。

按顺序检查：

1. Contract 存在（缺失返回 `coding_contract_missing`）。
2. `unexpected_changes` 为空 —— 改动了 `allowed_paths` 之外的文件则逐条 `unexpected_change:<path>`。
3. `require_test_pass` 时，必须存在成功的 exact required test（`successful_exact_required_test_missing`）。
4. 若发生过 mutation，成功测试必须发生在最后那次 mutation 之后（`successful_exact_required_test_stale`）。
5. 没有未解决的 Runtime error。

拒绝是**信息性**的：返回体带 `required_test` 的精确命令，让模型知道该补哪一步。
Gate 从不判断“用户想要的大概完成了没有”。

### 5. Freshness 只认两种事件

`event_seq` 对所有工具尝试递增，但两个游标只在明确条件下移动：

- `last_mutation_event_seq`：仅当工具定义标了 `workspace_mutation`（`write_file`、`apply_patch`）、
  已获批准、无失败前缀，且前后 workspace 摘要确实不同。失败、被拒、duplicate、no-op 都不移动。
- `last_successful_exact_required_test_seq`：仅当 `run_command` 的 command + args + cwd 与 Contract 完全一致，
  且退出码为 0。

失败的历史测试不留下永久污染：`test FAIL → patch → exact test PASS → finish_task` 最终可以通过，
因为游标记的是「当前最新有效状态」，不是「历史上有没有失败过」。

### 6. 普通 Final 的行为

- **Contract 生效时**：普通 Final 不算完成。Runtime 追加一条 `CODING_FINISH_PROTOCOL_NOTICE`
  （“Coding task is still RUNNING / call finish_task”）再问一次模型，状态保持 `RUNNING`。
- **普通聊天**：完全不变。没有 Contract 时普通 Final 直接结束循环，`finish_task` 即使在 Schema 里可见
  也只会被 Gate 拒绝并回喂原因，不影响任何状态。

### 7. MAX_AGENT_STEPS 与 Finish 的优先级

`MAX_AGENT_STEPS` 仍数「问了几次模型」，但每个被允许的模型响应都会先完整处理到第一个 CONTROL_FLOW 调用为止。
因此最后一步上的合法 `finish_task` 优先进入 `FINISHED`，不会被步数上限抢先覆盖；
若那一步被 Gate 拒绝，已处理完的工具结果仍留在 canonical history，随后状态进入 `LIMIT_REACHED`，
拒绝记录照常保留。

代价是：Contract 生效且模型反复给普通 Final 时，nudge 会让实际请求数最多接近两倍。
这是 Phase 21 的设计取舍，不是缺陷。

### 8. Session 持久化

Session version 升到 2，同时接受 1。v1 记录被明确禁止携带 Coding 状态。
`coding_contract` 与 `task_state` 必须成对出现，缺一即报错，避免存下一个无法解释的状态。

保存的是 canonical messages，不是压缩视图；`build_model_context` 的压缩只作用于发往模型的出站副本。
旧 Session 缺少 `task_state` 时，`restore_coding_session` 返回 `(None, None)` 并以普通聊天恢复——
不 crash，也不伪造“测试已通过”。状态不是 `RUNNING` 的 Coding Session 拒绝继续执行。

### 9. Acceptance

`verify_contract` 的判定拆成两条互不替代的线：

- `artifact_passed` 仍由独立 Verifier 决定，它重新执行 Contract 里的固定测试命令，与 Agent 自述无关。
- `interaction_completed` 在传入 `task_state` 时等于 `status is FINISHED`；未传 `task_state` 的
  旧调用方（Phase 18–20 评测脚本不驱动 finish 协议）保留「普通 Final + 未触上限」的语义，
  否则会被记上一个它们从未被告知要调用工具的 `finish_task_not_accepted`。

`accepted` 仍是两者与无 Runtime 异常的组合。Finish Gate 只是 Runtime 内的快速判定，
独立 Verifier 不因此被绕过。另有 `agent_self_verified` 表示「测试成功且晚于最后一次改动」。

### 10. Completion Hint

required test 通过后的提示从“如果完成请 Final Answer”改为“请调用 `finish_task(summary=...)`”，
同步更新了 coding task guidance、duplicate notice 和系统提示词。提示只是引导，
真正完成仍必须走 `finish_task → Gate → FINISHED`。
