# mini-agent-lab

单用户本地 CLI Agent，使用 OpenAI 兼容 SDK，支持受控文件操作、代码定位、测试和独立验收；不使用 Agent 框架、多 Agent、RAG、MCP 或数据库。

> **先了解边界**：只支持受信项目。允许执行 Python 测试不等于操作系统沙箱；测试代码仍可能访问本机与网络。工作区里的文件一旦读取，其内容可能被发送给你配置的模型服务。不要把密钥或生产数据放进工作区。完整说明见 [SECURITY.md](SECURITY.md)。

## 当前用户指南

### 安装与配置

需要 Python >=3.11；本地回归基线是 Python 3.11。首次联网安装依赖由你自行明确执行；离线检查本身不会下载、升级或发布任何包。

Windows PowerShell，在源码根目录运行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\mini-agent.exe config
.\.venv\Scripts\mini-agent.exe doctor
.\.venv\Scripts\mini-agent.exe start
```

Linux：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install .
.venv/bin/mini-agent config
.venv/bin/mini-agent doctor
.venv/bin/mini-agent start
```

激活虚拟环境后可直接输入 `mini-agent`。Windows 源码启动器 `mini.cmd` / `agent.cmd` 仍保留；统一 Python 入口为 `python launcher.py ...`。安装版不依赖源码当前目录，`mini-agent version` 显示包版本。

`config` 询问 API 地址、模型名和 Key，密钥输入不回显；也可手动复制 `.env.example` 并填写。使用支持工具调用的 OpenAI 兼容模型。普通 `start` 是交互式聊天；`doctor` 只做离线诊断，不发真实模型请求。

依赖锁来自现有虚拟环境：`openai==3.15.0` 与实际默认传递依赖（该 SDK 使用 `httpx2/httpcore2`，不是旧版本的 `httpx/httpcore`）。未联网升级、未虚构哈希。`requirements.txt` 引用同一份锁；包元数据固定 SDK 版本，完整传递版本仍需先安装锁文件。源码构建需要 setuptools 与 wheel；本轮已为项目虚拟环境补齐 wheel 0.48.0 及其 packaging 构建依赖，SDK 与运行依赖锁未升级。检查直接调用 setuptools 构建后端，不依赖另外的 build 前端；检查命令本身不会联网补依赖。

包版本 `0.1.0` 仅为本地打包元数据，尚未发布。仓库没有指定许可证；对外分发前须由权利人决定授权范围。

### 工作区与私有状态

```powershell
mini-agent start --workspace "C:\path\to\trusted-project"
mini-agent start --resume SESSION_ID
mini-agent start --workspace "C:\path\to\trusted-project" --contract "C:\path\to\contract.json"
mini-agent start --desktop
```

源码运行默认使用 `demo_workspace/`，状态默认位于源码根；安装版默认使用用户目录下的 `.mini-agent/`，工作区为其中的 `workspace/`。`AGENT_WORKSPACE` 和 `MINI_AGENT_STATE_DIR` 可显式覆盖。状态目录应位于工作区之外，包含本地配置、会话及文件变更记录，不提交 Git。

`--desktop` 选择系统桌面目录，不是只开放某个文件。建议优先使用一个新建的专用目录，而不是桌面或主目录。恢复会话时核对原工作区身份；不能借恢复把一段对话带到另一个工作区并沿用旧测试 PASS。

### 单任务 JSON、会话与撤销

```powershell
mini-agent task --task "读取资料并说明有哪些待确认事项"
mini-agent task --contract "C:\path\to\contract.json"
mini-agent task --resume SESSION_ID --workspace "C:\path\to\trusted-project"
mini-agent sessions list
mini-agent sessions show SESSION_ID
mini-agent sessions export SESSION_ID "C:\path\to\session-export.json"
mini-agent sessions delete SESSION_ID --yes
mini-agent undo RUN_ID --workspace "C:\path\to\trusted-project" --yes
mini-agent doctor --workspace "C:\path\to\trusted-project"
```

单任务入口的运行说明在 stderr，stdout 是一个 JSON 结果。成功为 0，未完成或失败为 1，参数错误为 2，取消为 130。普通任务的完成不等于产物验收；Coding Contract 使用固定测试、受保护来源、文件差异与显式 finish gate，独立 Acceptance 才决定是否接受。

会话管理支持列出、查看、命名、导出和显式删除；具体参数见 `sessions --help`。管理历史会话不要求原工作区仍然存在。删除只移除会话 JSON，不删除工作文件或 journals 撤销备份。导出会做脱敏，但分享前仍需人工检查。会话包含 canonical 历史、文件内容与工具参数，不是跨会话 Memory。

新会话绑定规范化工作区，使用进程锁和 revision 防止同一会话并发覆盖。旧 v1/v2 会话如果没有工作区信息，必须确认原目录后用 `--resume SESSION_ID --workspace 原目录 --adopt-workspace` 显式绑定；这个选项不能改绑已有工作区。取消、异常或限额后的编码任务可显式恢复，旧测试证据会重新核验。每次执行有独立 run_id 和预算；显式恢复会开启新批次，历史预算保存在会话的 `runs`，最新值也可由 `sessions show` 查看。

文件写入、patch、重命名的前态保存在私有变更记录中，使用任务结果里的 `run_id` 显式 undo。取消或请求失败**不会自动回滚**已完成的修改。undo 不覆盖用户同期编辑，冲突或部分恢复会明确返回；命令执行造成的任意副作用不属于文件撤销范围。撤销记录可能包含完整旧文件，同样不能公开。

### 觉察对话（终端持续对话，独立入口）

```powershell
.\mini.cmd awareness
.\mini.cmd awareness --json
```

也可用 `mini-agent awareness` 或 `python launcher.py awareness`。终端每次输入一行，回车提交，回复后继续输入；可以补充或纠正上一轮，输入 `exit` / `退出` 结束，Ctrl+C 取消，EOF 正常结束。上下文仅保留在当前进程内存，重新启动不会恢复。自动化用 UTF-8 标准输入管道传入多行，EOF 提交，仍为单次整理。不要把敏感正文直接写在命令行或 shell 历史里。入口仅接受 `--json` / `--help`，不接受 `--task`、`--resume`、`--contract`、`--workspace` 等执行型参数。`--help` 不读取输入或初始化模型。

默认用简短自然对话回应，不再显示三栏、重复引用和来源标签。经历、感受与解释仍在内部区分，完整逐字引用与来源保留在校验和 `--json` 结果中。先回应实际表达的处境和感受，不预设用户应当平静、积极或放下；未表达的感受允许空缺，推测必须使用试探措辞。旧轮次感受不直接断言为当前仍存在；同意、拒绝、跳过和结束时简短承接选择，不反复重述处境。经历不是核实事实，解释不确定不等于错误，不要求求证；保留具体伤害、辱骂和威胁，不把现实问题一概解释成用户的想法。

觉察引导先询问意愿。上一轮邀请后的直接短确认，如“愿意”“我愿意”“可以”“你问吧”，才可开启当前话题的引导；“可以？”等疑问、转述、假设、含糊回答或夹带其他说明的复杂回答不自动授权，可先澄清。许可在当前话题内有效，转话题后重新确认。每轮最多一个可跳过的问题，不每轮都问；没有回答上一问时承接用户实际说的内容，不换个说法继续追问。拒绝、跳过或撤回后同一话题不再邀请，仍可普通对话。“停止”“我不想继续了”等可能指停止引导的表达交给模型区分，不直接作为退出命令。模型识别明确结束整个对话的意图后会简短结束并退出；`exit` / `退出` / `结束对话` 仍不调用模型。急迫风险继续采用独立安全支持分支。不读心、不诊断、不引入书籍观点、不默认给建议，不布置呼吸、闭眼或其他练习。

`--json` 的 stdout 每轮输出一个对象（管道仅一个对象；终端连续对话则为逐行 JSON）：`status`（`completed` / `failed` / `cancelled`）、`result`、`error`。成功时 `error=null`；失败时 `result=null`，`error` 仅含固定的 `code` / `message`。退出码为成功或正常结束 0、失败 1、参数错误 2、取消 130。终端的可恢复请求失败会显示失败并等待用户下一次输入；失败回复不加入上下文。配置、预算或未知运行错误则结束，不自动重试。结果有两种互斥结构：

- `mode="organize"`：新增必填 `reflection`（简短自然回应）、`response_type`（`respond` / `invite` / `clarify` / `guide` / `end`）和 `intent`（恰好为 `kind` / `quote`）；保留 `experiences` / `feelings` / `interpretations` 与 `question`。这是觉察 JSON 接口的一次扩展，旧消费者需适配新增字段。分类每项仍恰好为 `quote` / `text` / `source`；来源枚举不变。
- `intent.kind` 为 `none` / `consent` / `decline` / `skip` / `revoke` / `topic_change` / `end`。`none` 的引用为 `null`；其他意图必须引用当前用户消息，不能引用旧轮次或助手。`consent` 的引用还须覆盖当前完整输入（可去掉首尾空白）并通过短确认检查，不能截取他人话语中的“愿意”。引用校验只证明文字来源，不能证明同意、换题或结束的语义判断成立；这些判断需人工验收。
- `question` 是唯一提问字段：`respond` / `end` 必须为 `null`，其他类型必须有一个问题；`reflection` 不允许问号。`invite` 只能在未同意的新话题询问意愿，`guide` 只能在当前话题已同意时使用。固定模型规则要求未同意时的引导意愿邀请标为 `invite`，不能标为 `clarify`，且面向用户不解释这些内部状态。程序校验状态和类型组合，回应类型与问题实际含义的一致性仍需语义复核。
- `mode="safety_support"`：`risk`（同样的三字段，来源仅 `user_report`）与 `support="urgent_help"`；不含三栏或问题。这个枚举对应人类可读输出中的固定现实安全求助说明。

本入口不发送工具 schema、不执行模型工具调用、不读旧会话，不创建工作区、状态、会话、锁、journal 或验收快照。仍使用既有模型配置与内存预算；格式、字段、来源或引用失败最多重新生成一次，初始和修复共用预算，坏回复不回灌模型。缺失 usage 的默认 STOP 可能禁止修复，不能当作成功。网络、拒绝、截断、空回复、协议错误与工具调用不会触发格式重试。可识别的安全支持分支校验失败直接失败，不强行修复为三栏。

终端上下文只包含当前运行成功的用户输入与校验后的助手结果。引导状态仅在内存中，包括未同意、等待确认、当前话题已同意，以及当前话题已拒绝（用于防止反复邀请）。明确撤回在请求前就会使许可失效；离开旧话题的表达不等于拒绝新话题的引导。失败回复不能授予许可；若格式违规回复已用当前原文识别了拒绝、跳过、撤回、换题或结束，修复轮先收紧许可并必须保留该意图，修复失败仍不恢复许可。其他请求失败也撤销旧许可，避免沿用可能失效的话题授权。引用可来自任一条用户输入，但不能来自助手推测，也不能拼接跨轮片段。每轮采用独立预算；累计上下文仍受字符上限约束，超限时结束并明确报错，不静默丢弃较早的上下文。退出或取消后不写入历史文件。

输入最多 12000 字符、回复最多 24000 字符，JSON 嵌套最多 8 层；超限直接失败，不静默截断。每栏最多 24 项，引用最多 2000 字符、整理最多 1000 字符、问题最多 500 字符。拒绝无效 Unicode、终端控制和危险格式字符（换行、回车、Tab 除外），保留正常文字和组合 emoji 所需的 U+200C / U+200D 连接符；人类输出将字段中的换行等转成可见转义。发现既有脱敏规则会改变输入时，要求先移除疑似凭据；结果也不得靠事后脱敏篡改原文引用。

隐私边界：**不保存本地会话不等于纯本地或完全无痕**，正文仍发送给配置的模型服务商，服务商留存不受本程序控制。运行期间局部禁用 SDK/HTTP 日志（包括 `OPENAI_LOG=debug`），错误不打印原始响应或异常正文；无法保证所有秘密都被识别，终端录屏、管道上游、系统及服务商日志也不受本入口控制。

安全分支的模式字段已可识别时，即使 JSON 缺少结尾、后续字段损坏或模式字段使用 Unicode 转义，也直接报告安全分支校验失败，不进入普通格式修复。引用文字里的模式示例不作为模式字段。

结构、来源枚举和逐字引用校验**不证明语义、话题识别、风险识别或事实正确**。许可校验限制结构与直接短确认，但不能证明模型的回应类型准确描述了实际文本。离线 mock / SDK 回环测试验证工程边界；真实模型合成案例只支持有限的人工语义复核，不代表心理健康用途的全面可靠性。

合成对话与人工复核清单可用 `.\.venv\Scripts\python.exe eval/awareness_dialogue.py` 预览，不读模型配置或访问网络。加 `--live` 才会使用当前配置逐轮调用模型；每个场景重新清空内存状态，输出仅写终端，不自动保存正文。场景的 `completed` 只表示校验后的轮次完成，不能当作语义验收通过。2026-10-04 的工程检查、实际模型观察与针对性复测见 [觉察对话验收记录](eval/AWARENESS_DIALOGUE_REVIEW.md)。

### 九类护栏

下表描述本轮已接线并在 Windows / Python 3.11 上验证的实现边界。2026-10-02 的完整离线入口 6/6 通过：主套件收集 399 项，398 通过、1 项因本机符号链接权限跳过；另有两个脚本检查、20 项指标检查、5 项 eval 可靠性测试及语法检查通过。独立打包入口也为 6/6 通过。数字只代表确定性工程回归，不代表真实模型任务成功率。

| 范围 | 当前边界 |
| --- | --- |
| 文件路径与隐私 | 统一工作区限制、敏感路径、Windows ADS/设备/UNC/歧义路径和链接处理；文件读入模型仍属于外发 |
| 文件变更与恢复 | 内容有界、原子写入、审批差异、同期变更前提复核、任务变更记录与冲突拒绝 undo |
| 命令与进程 | 测试/Git 参数策略、无 shell、最小化子进程环境、输出上限、超时/取消与进程树清理；不是 OS 沙箱 |
| Coding 验收 | 固定测试来源受保护、非允许路径检测、实际测试数量及跳过/失败识别、修改后测试新鲜度、独立最终验证 |
| Runtime 预算 | 总时间、工具总数、单批数量、上下文与输出/累计 token 的独立限额，不仅依赖模型步数 |
| usage 与错误 | 缺失 usage 不填零，已知与估算分开；默认 STOP，错误/取消不伪装完成，不隐式无限重试 |
| 会话状态 | 原子保存、协议配对、工作区身份与恢复校验、历史证据失效、管理/脱敏导出 |
| CLI 与诊断 | 统一入口、严格参数、stdout JSON/stderr 日志、明确退出码、离线 doctor、显式 undo |
| 安装与发布准备 | Python 包/版本/入口、完整扁平模块、已安装依赖锁、隔离离线回归、跨平台 CI、真实发布评测规范 |

ASK 是交互默认。批准副作用前核对文件差异或命令；无交互通道不能自动变成 ALLOW。自动化仅在你已信任的临时项目里显式选择 ALLOW；拒绝路径用 DENY。敏感路径例外只支持精确路径，不可用通配符，例外不等于秘密自动脱敏。

### 环境配置与预算

所有环境项应在启动进程之前设置；不要把真实 Key 写进命令记录或测试输出。

| 环境项 | 用途 / 默认 |
| --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | 模型连接；Key 只存本地私有配置 |
| `AGENT_WORKSPACE` | 受信文件工作区 |
| `MINI_AGENT_STATE_DIR` | 工作区外的私有配置、会话与变更记录目录 |
| `TOOL_APPROVAL_MODE` | `ASK` / `ALLOW` / `DENY`；交互默认 ASK |
| `CONTEXT_MODE` | `OFF` / `WRITE_ONLY` / `FULL`；默认 WRITE_ONLY，只改变出站视图 |
| `MINI_AGENT_TASK_TIMEOUT_SECONDS` | 单任务时间限额，默认 300 秒 |
| `MINI_AGENT_MAX_TOOL_CALLS` | 单任务工具调用上限，默认 64 |
| `MINI_AGENT_MAX_BATCH_TOOL_CALLS` | 单次响应的工具批次上限，默认 8 |
| `MINI_AGENT_MAX_CONTEXT_CHARS` | 单次请求上下文字符上限，默认 120000 |
| `MINI_AGENT_MAX_OUTPUT_TOKENS` | 单次模型输出 token 上限，默认 4096 |
| `MINI_AGENT_MAX_TOTAL_TOKENS` | 任务累计 token 预算，默认 100000 |
| `MINI_AGENT_UNKNOWN_USAGE_POLICY` | `STOP` / `ESTIMATE`；缺失 usage 默认 STOP |
| `MINI_AGENT_ALLOW_SENSITIVE_PATHS` | 显式允许的精确相对路径，分号分隔或 JSON 数组；默认无例外 |
| `MINI_AGENT_HTTP_PROXY` | 历史 eval 的显式 Provider 代理项；离线检查清空真实代理 |

这些 token 限额不是硬收费保证；服务商可能不返回完整 usage，字符/字节估算不是精确 tokenizer，也不能追回已发出的请求费用。模型调用步数与历史恢复额度仍由 Runtime 控制，不通过任意放大步数掩盖失败。SDK 隐式重试关闭，失败请求保留未知用量，而不是报告零成本。

编码验收快照采用流式 SHA-256，限制为 20000 个目录条目、128 MiB 普通文件内容，并检查任务时限；过大时要求缩小工作区。快照不读取符号链接目标，不接受共享硬链接作为内容验证证据。普通无工具聊天不扫描整个工作区。读取分页以脱敏后的实际字符数重新计算完整行范围；命令摘要保留输出首尾，避免截掉真实测试统计。

### 完整离线回归与构建检查

```powershell
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe scripts/check.py --packaging-only
```

Linux 换为 `.venv/bin/python`。默认完整入口运行 unittest tests、`test_loop`/`test_sandbox` 两个脚本、eval metrics、五项 eval 可靠性测试与全部 Python 语法检查，任何一项失败均聚合非零，后续检查不因为前一项失败而静默略过。

检查只运行于独立临时项目副本：不复制真实 `.env`、sessions、Git、Serena、虚拟环境、私有变更记录或外部链接，不触及原 demo。TMP/TEMP、工作区、状态和用户配置目录均隔离，真实 Key/代理不继承，模型只指向 dummy 回环地址，并限制 Git 上溯。Windows 启动器测试临时复用已有 venv 目录链接，结束时先**只移除链接**，绝不递归删除目标。

`--packaging-only` 仅检查包模块、锁版本与离线依赖闭包，构建 sdist/wheel、离线安装，并从不含源码的目录运行安装入口 `--help`。没有 wheel 等构建工具或依赖时明确非零，不下载补齐。CI 覆盖 Windows/Linux 的 Python 3.11 工程检查；准备依赖阶段需要网络，实际回归阶段离线，不调用付费模型，也没有 publish 步骤。

### 已知验证限制

- 通用编码任务未发起新的真实模型评测，没有新的自然任务质量、通过率或成本结论。代表性任务与人工判据见 [release_tasks.json](eval/release_tasks.json) 和 [RELEASE_EVAL.md](eval/RELEASE_EVAL.md)，真实开跑需显式确认。觉察对话的当前模型合成复核另见 [验收记录](eval/AWARENESS_DIALOGUE_REVIEW.md)，不能推广为通用任务质量结论。
- 本机完整行为回归与构建安装已经实际通过，包括使用真实 SDK 连接回环假服务的 CLI 工具调用、拒绝、截断和服务异常场景；这些离线检查没有访问外部模型服务。
- Windows 的一个符号链接测试受权限限制原生跳过，没有人为跳过失败测试。Linux 与远程 CI 尚未实跑，配置存在不代表对应平台已验证。
- 小 fixture、mock、保护测试来源以及零测试检测均不证明业务测试完整；总结事实与自然任务质量仍需人工验收。
- Windows/Linux CI 只配置 Python 3.11；其他 Python 版本、极端目录竞态与不可信项目执行不在当前已验证承诺内。
- 脱敏规则不是所有秘密的识别器；API 服务商的数据留存、账单与取消行为不由本地护栏保证。

## 研发历史（旧行为与旧结果）

以下保留原 Phase 1–25 的研发过程与当时数字。旧文中的“沙盒”、依赖范围、无备份、步骤上限及历史评测结论都是对应历史版本的描述，不是当前安全或发布承诺；当前使用以本指南、SECURITY.md 与实际离线检查为准。

从零手搓一个最小但真正可运行的 CLI Agent。

## 最终目标

```text
用户输入任务 → LLM 判断下一步 → 选择工具 → 执行工具 → 结果回喂 LLM
             → 继续判断 → 连续执行多步 → 直到任务完成
```

我们要亲手把这个循环搭出来，而不是套框架。

## 技术选型（一句话）

- **Python 3.11** —— 标准库够 Phase 0 用；后面装 LLM SDK 也只需一个包
- **LLM 走 OpenAI 兼容协议** —— 用官方 `openai` SDK，但 `OPENAI_BASE_URL` 可指向任何兼容服务
  （OpenAI / DeepSeek / GLM / Kimi / OpenRouter / 本地 Ollama）
- **不用** LangChain / LangGraph / MCP / 数据库 / RAG / Memory —— 全部自己写，因为要经历它

选 OpenAI 兼容协议的真正原因：程序从 `.env` 读取 `OPENAI_API_KEY`、
`OPENAI_BASE_URL` 和 `OPENAI_MODEL`，再交给 `openai` SDK；更换兼容服务时只需修改配置。

## 首次使用（Windows）

需要 Python 3.11，以及支持工具调用的 OpenAI 兼容模型。克隆仓库后，在项目目录运行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\mini.cmd config
.\mini.cmd start
```

`config` 会依次询问 API 地址、模型名和 API Key；密钥输入不回显，配置只保存到本地
`.env`。以后修改配置仍运行 `.\mini.cmd config`，直接回车可保留已有值。
如需手动配置，也可以复制 `.env.example` 为 `.env` 并填写这三项。

默认只操作项目内的 `demo_workspace`；文件写入和重命名默认询问批准。运行
`.\mini.cmd start --desktop` 才会把工作区切换到桌面。`.env` 和本地会话文件已被 Git 忽略。
`mini start` 这种省略路径的写法需要自行把项目目录或转发脚本加入 PATH；克隆仓库后
直接使用上面的 `.\mini.cmd start`。

## 当前状态：Phase 25

### Runtime Capability Introspection

**Runtime Tool Capability ≠ Runtime Feature**。`inspect_capabilities()` 只读返回
`callable_tools` 与 `runtime_features`：前者从当前 `TOOL_REGISTRY` 生成，报告工具描述、
风险、审批要求、当前可用性和不可用原因；后者报告 Session、Context、Sandbox、
Finish Gate、Verifier、verification freshness、stage-aware recovery 等 Runtime 能力，
并区分 `supported`、`active`、`current_state`。Runtime 能保存 Session，不表示模型能直接调用
Session Tool；当前有工作区内 `rename_file`，没有任意位置的 move Tool。`run_command` 受现有 command policy 限制，
不是任意 shell。能力问题优先调用 `inspect_capabilities()`，不靠 sandbox 源码推测。

需要核对具体实现时，`inspect_project()` 可列出并分页读取 Agent 自身的一小组核心项目文件。
这是只读入口；`.env`、会话、测试产物和项目外文件不在可读列表中。普通任务文件继续通过
`list_files` / `read_file` 访问 `AGENT_WORKSPACE`，写入和命令执行边界不变。

### Windows 交互式启动

首次安装步骤见上方。之后在项目目录运行 `.\mini.cmd start`，或在 CMD 输入
`agent.cmd`、在 PowerShell 输入 `.\agent.cmd`，即进入现有 `main.py` 交互式 CLI。
`agent.cmd` 使用脚本自身目录定位 `.venv\Scripts\python.exe` 和 `main.py`，不改全局 PATH；
可从其他当前目录用脚本路径启动。`--resume SESSION_ID` 参数会原样传给正式入口。
单任务 JSON 入口仍为 `cli.py --task ...`。

`.\mini.cmd start --resume SESSION_ID` 恢复会话，`.\mini.cmd help` 显示用法。
需要从任意目录使用时，可通过项目内 `mini.cmd` 的完整路径启动。

要处理桌面文件，**新开会话**运行 `.\mini.cmd start --desktop`。此模式把工作区限定为桌面；
`read_file`、`write_file` 和 `rename_file` 才能看到桌面文件，写入和重命名仍按审批模式处理。
`rename_file` 拒绝覆盖已有目标文件。普通 `.\mini.cmd start` 仍使用 `demo_workspace`。

终端执行记录只显示工具名、简要参数和结果摘要；`inspect_capabilities` 显示可用工具数量，
不会把整段 JSON 打满屏幕。完整工具结果仍会交给模型并保存在会话消息中；任务 Trace 数据不变。

Phase 24.4 已加入第 8 次固定 required test 实际 FAIL 后的阶段式恢复控制。
恢复最多允许 2 次有效修改、2 次固定测试、1 次有效 finish 和 2 次无进展轮，
总模型调用数不超过 15；普通任务仍限 8 次。两次固定前缀真实验证中，
一次因无进展额度耗尽而未接受，另一次在第 15 次调用完成并通过独立验收。
实现、验证结果与复现方式见 [Phase 24.4 报告](eval/phase24_4_report.md)。

Phase 24.5 修复了真实修改后读取、搜索和固定测试被旧重复调用记录拦截的问题。
两次追加的固定前缀真实运行都在第 11 次调用通过独立验收；
样本与限制见 [Phase 24.5 报告](eval/phase24_5_report.md)。

用户只给一个**目标**，Agent 自己看目录、自己挑文件、自己读、
自己判断要不要再读一个，最后把整理好的结果**写回工作目录**并汇报：

```text
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
| --- | --- |
| `list_files` | 列工作目录一层内容，标 `[f]`/`[d]` 和字节数。`path` 可选，省略就是列根目录 |
| `write_file` | 写 UTF-8 文本，父目录不存在会自动创建，但只能创建在沙盒内 |
| `apply_patch` | 对已有 UTF-8 文本做 `old_text → new_text` 精确替换，`old_text` 必须唯一匹配 |
| `rename_file` | 在当前工作区内重命名已有文件；目标已存在时拒绝覆盖 |
| `run_command` | 以 `shell=False` 执行白名单内的 Python 测试或 Git 只读命令 |
| `TOOL_REGISTRY` | 工具名 → `ToolDefinition(name, schema, handler, risk_level)`。新增正式工具只在这里注册 |

「先看目录、再决定读哪个」这件事**没有**写进系统提示词，程序里也不强制。
工具说明书里只有一句「如果你不知道有哪些文件，先用这个工具，不要猜文件名」——
让模型自己从工具描述里学出用法，而不是照着脚本走。
系统提示词只说「有哪些工具、各干什么」，最后一句是「用哪个工具、用什么顺序，你自己决定」。

Phase 6 在同一个循环上叠了三层**互相独立**的防线，解决真模型实测暴露的
「任务已经完成，但 Agent 不知道什么时候该停」：

| 层 | 谁负责 | 机制 |
| --- | --- | --- |
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

```text
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
| `列文件` | 正常列出工作目录（`[f]` / `[d]` + 字节数） |
| `todo.txt` | 正常读到文件 |
| `越界` | 模型试图读 `../.env`，被沙盒拦下 |
| `不存在` | 读一个没有的文件 |
| `坏参数` | 模型给出非法 JSON 参数 |
| `big_notes.txt` | 读一个大文件，验证结果会被截断到 4000 字符并明说省略了多少 |
| `写个测试文件` | 正常写进工作目录（自动创建 `notes/`） |
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

```text
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

```text
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
| --- | --- |
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
| --- | --- |
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

阶段式恢复及修改后的重复调用刷新已完成 Phase 24.5；四次固定前缀真实验证仍不足以判断通过率或 token 成本的稳定性。

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

```text
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
  且退出码为 0。较新的固定测试若实际执行且以非零码失败，会清除此游标；后续 PASS 可重新建立。

真实文件修改会清除先前成功的工具调用记录，使相同的读取、搜索和固定测试能针对新内容重新执行；
失败修改和 no-op 不会清除记录。

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

## Phase 21.1 · Runtime Hardening

这不是新的 Agent 能力阶段。Phase 21 的协议、状态机、Gate 规则、Acceptance 判定和
Session schema 全部不变；本阶段只修 Phase 21 真实运行暴露出来的两个环境问题。

### 1. ASK 审批在无交互环境下快速失败

问题：`DEFAULT_APPROVAL_MODE=ASK` 在子进程、评测 harness、CI 里没有交互通道，
`input()` 返回 EOF，`ask_for_approval` 按安全默认拒绝。副作用工具每次都拿到拒绝，
模型反复重试直到 `MAX_AGENT_STEPS`，结果是 `finish_task_calls=0`、`LIMIT_REACHED`、
`total_tokens` 白白花掉。这是环境失败，不是推理失败。

现在：

- `ApprovalUnavailableError` 是独立异常，**不会被改写成 `[用户拒绝执行]`**。
  「环境没有审批通道」和「用户明确拒绝」是两回事，混在一起会让日志和 Session 记录同时失真。
- `cli.py` 的 Coding Task 在第一次模型调用之前做一次 preflight：Contract 生效 + 模式为 ASK +
  检测不到交互通道 → 直接返回 `{"status": "failed", "error": ...}`，`model_calls=0`，
  不进循环、不消耗额度。
- `ask_for_approval` 在打印提示之前先检测；检测不到就抛异常，不靠 EOFError 当正常控制流。
- `main.py` 的循环把该异常与 API 通信类失败一起处理，报清晰错误，不甩 traceback；
  Contract 生效时记入 `task_state` 的 `ERROR`，普通聊天回滚本轮悬空消息。

交互通道的检测用「stdin 和 stdout **都是**终端」两端条件。只看 stdin 在 Windows 上不够：
`isatty()` 对 `NUL` 这类字符设备也返回 True，stdin 指向 `NUL` 的子进程会被误判成交互终端。
代价是 `python main.py > transcript.txt` 这种只重定向输出的跑法里，副作用工具会报
「审批不可用」——提示本来也看不见，报清晰错误比让人盲打更对。

安全语义没有变：没有用户明确批准，SIDE_EFFECT / EXECUTION 不执行。
ASK + 无 TTY 不会被自动降级成 ALLOW，也不会跳过 Approval。
ALLOW / DENY 不检测终端，行为不变；READ_ONLY 和 CONTROL_FLOW 在回调之前就已放行，不受影响。

### 2. Snapshot 忽略测试运行器缓存

问题：Contract 的 required test 用 `python -m pytest` 时，`pytest` 生成 `.pytest_cache/`，
被 `snapshot_workspace` 当作 Agent 的改动 → `unexpected_change:<path>` → Finish Gate 永久拒绝。

扩展的是**同一份** ignore 集合，不是另写一套规则：

```python
_GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
_GENERATED_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache"}
```

`_is_generated_artifact` 是唯一的过滤点，`snapshot_workspace` 调用它，
Finish Gate 和独立 Verifier 都从 `snapshot_workspace` 取摘要，两边的判定天然一致。
按目录名锚定匹配，所以根目录里同名的 `README.md` 仍然被跟踪。

为什么清单保持刻意地短：任何宽泛的忽略规则（忽略所有点目录、所有隐藏文件、所有未知新文件）
都会让真实的越界改动逃出 Verifier，而那正是这个机制要抓的东西。
「只忽略明确列举的缓存」是唯一不会放过真问题的形状。
`.coverage`、`.mypy_cache`、`.ruff_cache` 等本轮未确认的问题不加，
它们仍然会被报为 `unexpected_change`，有回归测试锁住这一点。

忽略 snapshot 产物只影响 `changed_files` / `unexpected_changes` 的判定，
**不涉及 Sandbox 路径校验**：Agent 能读写和访问的范围没有任何变化。
