# v2 迁移与验证记录

日期：2026-10-11。环境：Windows / Python 3.11.9。迁移分支：`refactor/langgraph`。
迁移前提交：`13e61321630fc838e3b697858f1f9011510db89b`。本轮没有发布、推送或合并。

## 源码审计与决策

实际源码已做过初步拆包：顶层 `main.py` 本来就是 shim；问题集中在约 1,414 行
的 `mini_agent/runtime.py`、模型调用层和自定义工具注册层。执行循环同时处理
模型、历史、审批、工具、验收、恢复和日志。工具模块还通过 `sys.modules` 回读
顶层 `tools` 的可变全局状态，运行和测试都依赖这些反向引用。

因此本轮替换执行引擎，同时保留可独立工作的业务边界。没有把旧 Runtime 整体
移到另一个目录，也没有维持两套运行时。新 `AgentApplication` 只装配组件、调用
编译图、读取持久化状态和输出结果，模型与工具的循环由框架执行。

| 原能力/代码 | 决策 | 当前实现 |
| --- | --- | --- |
| `agent.py`、原 `runtime.py` 的 SDK 调用与循环 | 替换 | `ChatOpenAI` + `create_agent` 生成的 LangGraph |
| 自定义 Tool schema/registry/dispatcher | 替换 | 一个 `build_tools` 列表、Pydantic 参数模型、标准 `StructuredTool` |
| 文件读写、搜索、分页、精确替换、重命名 | 保留并适配 | `mini_agent.tools.filesystem`，工作区通过参数绑定 |
| 测试命令、只读 Git、输出限制、进程清理 | 保留并适配 | `mini_agent.tools.command` / `process_runner` |
| 路径限制、敏感文件保护、原子写入、前置条件、撤销 | 保留 | `sandbox`；日志可在恢复时续写，撤销继续检查冲突 |
| Session JSON、旧 Recovery/重放控制 | 替换 | 原生 `SqliteSaver`、`thread_id`、Checkpoint 与 `Command(resume=...)` |
| 权限回调和手写待审批队列 | 替换 | 框架 `HumanInTheLoopMiddleware` + 业务 `BoundaryMiddleware` |
| 旧 Context 特判与历史压缩控制流 | 替换 | 模型消息投影与 `trim_messages`；Checkpoint 留存完整消息 |
| Coding Contract、Finish Gate、独立 Verifier | 保留并解耦 | `verifier` 纯业务逻辑 + `VerificationMiddleware` |
| `finish_task` 控制工具 | 删除 | 模型最终回答是完成候选，业务门禁决定是否接受 |
| 任务/工具/token/时间预算 | 保留并接线 | `RunBudget` + 原生 `ModelCallLimitMiddleware`；请求超时受配置约束 |
| Trace/诊断与能力检查 | 简化 | 元数据事件；标准 `inspect_project` / `inspect_capabilities` |
| 原 awareness/session/phase CLI 与实验驱动 | 退出当前产品路径 | 旧源码可从迁移前提交恢复；历史报告、任务数据保留 |

顶层必要启动/导入文件只保留短 shim。生产模块不再读取这些 shim 的可变全局值。
工具扩展通过 `extra_tools` 注入标准 LangChain 工具；能力检查同时包含扩展工具。
配置统一由 `Settings.load` 读取 `.env` 和进程环境，环境变量优先。

## 状态、审批与迁移边界

新数据库位于 `{state_dir}/.mini-agent/checkpoints/v2.sqlite3`，文件变更日志在
同一私有目录下的 `journals/`。CLI 保留 `mini.cmd config/start`，并提供 JSON
`task`、`task --resume`、审批决策、`sessions`、`sessions --legacy`、`undo` 和 `doctor`。
旧 JSON 不转换、不删除、不当作可执行 Checkpoint；旧导出/回放命令不是 v2 API。

`ASK` 在工具执行前持久化审批，重启后使用相同会话 ID 恢复。恢复测试证明：已确认
并保存的文件写入不会再次执行；恢复后文件的修改时间保持不变，只执行后续读取。
文件在暂停期间被人工修改时，恢复会拒绝覆盖。

这不意味着进程在副作用完成而回执尚未保存的任意瞬间退出都具有 exactly-once
保证。未来远程/MCP 写操作仍需业务幂等键或查询回执。SQLite 是本地同步存储；
同一工作区的多个并发任务也不提供事务隔离。

工具错误不会被当成成功；另一个文件的成功读取也不能清除原文件的失败。
带 Contract 的完成候选必须满足允许路径、未改动测试、有效且新鲜的指定测试；
独立 Verifier 再执行相同的已批准测试。零测试、全跳过、输出截断和仅退出码零
均不足以证明通过。普通自然语言任务没有通用语义 oracle，业务验收应使用 Contract。

## 实际验证与复现

迁移前的隔离回归入口实际运行，原六项检查均通过。旧测试中依赖自研 ModelReply、
dispatcher、Session JSON、finish_task 与 phase harness 的部分随旧引擎移除；它们
没有被硬塞入新引擎。文件安全、命令、撤销、配置、资源预算和复制隔离回归保留。
核心流程改由以下测试覆盖，旧版与新版的测试数量不可直接折算为能力覆盖比例。

| 要求 | 已执行证据 |
| --- | --- |
| A：目录、搜索、读取、修改、核对 | `test_langgraph_runtime` 多步图流程，以及真实 HTTP 传输测试 |
| B：失败测试、修复、重测、独立验收 | 真实文件与 unittest 子进程；过早完成和修改后旧证据均被拒绝 |
| C：异常/审批中断后跨应用恢复 | SQLite 重开、原生审批恢复、工作区身份校验、写入不重放 |
| D：标准工具扩展 | `@tool multiply` 注入，不修改 Runtime；模型调用并在能力列表可见 |
| 原文件/命令保护 | 原子写入、硬链接、Windows 路径、敏感文件、扫描/输出预算、撤销冲突、进程树清理 |
| 上下文与生命周期 | 长工具结果投影不改 Checkpoint、多轮状态保留、失败不回显上一任务回答、模型调用限额 |
| HTTP 与 CLI | 本地 OpenAI-compatible 服务；真实 `ChatOpenAI` 请求、错误恢复、Windows `mini.cmd task` JSON |

开发依赖采用 `requirements.lock` 的完整依赖闭包，运行：

本地最终检查：pytest **135 passed、1 skipped、119 subtests passed**；该跳过项为
另行执行的真实模型验收。Ruff、mypy（24 个源文件）和语法检查全部通过，隔离
回归 **4/4**；隔离打包 **6/6**。安装式 Quick Start 与离线修复 Demo 均通过。
结构化公开记录见 [V2_VALIDATION.json](evaluation/V2_VALIDATION.json)。

```powershell
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe scripts/check.py --packaging-only
.\.venv\Scripts\python.exe scripts/verify_quickstart.py --entry .venv/Scripts/mini-agent.exe
.\.venv\Scripts\python.exe examples/offline_coding_demo.py
```

`scripts/check.py` 在隔离副本执行完整 pytest、Ruff、mypy 和语法检查，不读取真实
配置或调用外部模型。样例项目中的故意错误代码是 Agent 输入，pytest 不收集
`tests/fixtures` 为仓库自身测试。临时测试产物不纳入代码 lint。

## 真实模型验收

真实配置只从本地 Git 忽略的 `.env` 读取。工作区、测试和 Checkpoint 均在一次性
临时目录，明确允许修改范围为 `calculator.py`、`notes.txt`，不修改测试。
验收结果：`completed`，独立验收 `accepted: true`，指定测试 PASS（1 项实际执行），
无范围外修改。5 次模型调用、9 次工具调用、14,208 total tokens，应用计时 17.13 秒。
模型请求的工具顺序：list → search → search → read → read → patch → patch → read → command；
独立 Verifier 随后另行复跑指定测试。

首次连接通过本机代理失败，尚未产生可用模型响应或工具执行。已修正当前
LangChain HTTP 客户端 socket 配置对环境代理的影响；本机代理的 TLS 连接仍失败，
成功验收通过对配置服务的主机设置 `NO_PROXY` 直连完成，没有关闭 TLS 校验。
代理选项由进程环境控制，不在代码中硬编码服务地址或私密配置。

需要重新运行时，配置有效的服务，并根据实际网络设置代理；以下占位主机需替换：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:ALL_PROXY = "socks5://127.0.0.1:7897"
# 仅在该服务需要绕过代理时设置；保留 TLS 验证。
$env:NO_PROXY = "127.0.0.1,localhost,YOUR_PROVIDER_HOST"
.\.venv\Scripts\python.exe -m pytest -q tests/test_live_acceptance.py --run-live
```

默认离线套件跳过这一项，避免反复消耗模型额度。本次真实测试已单独实际运行
通过，公开验证摘要不含 Key、服务地址、完整会话或个人路径。

## 未实现与验证范围

当前完成同步 CLI/API 和上述 A–D 功能。MCP Client、Planning、独立 rag-chain 接入、
长期 Memory、Streaming、FastAPI/Web UI、GitHub 工作流和 Multi-Agent 按 README
路线图推进，本轮没有合并或修改 rag-chain。

Windows/Python 3.11 本地验证通过；CI 已配置 Windows/Linux × Python 3.11/3.13，
但未触发远程 Actions，不能声称这四个平台组合已实际通过。单次真实模型验收
证明当前服务链路可用，不是任务质量或长时稳定性的统计基准。

架构图与模块边界见 [architecture.md](architecture.md)，实际阅读的 LangChain、
LangGraph、HelloAgents、Datawhale 源码基准与采纳决策见
[framework-references.md](framework-references.md)。
