# mini-agent 最终系统验收

验收日期：2026-10-14。结论：简单任务和工具链已有可复核的通过证据；中等全栈任务的代码结果正确，但真实模型未完成无需人工恢复的开发闭环。**暂不建议直接合并 PR #33 到 main。** 本次通过独立 QA 分支和 Draft PR 交付修复，不发布、不合并。

## 1. 版本、环境和证据口径

- 基准：[PR #33](https://github.com/GimMoliyadi/mini-agent/pull/33)，head `6a531e563855520a38993aec6425990acb8ac855`；目标分支 `fix/integrated-ci-typing-20261014`。
- QA 分支：`codex/final-system-acceptance-20261014`。受验源码提交 `018019b16242de46f9b0898772603d29a47883e8`；远程 CI 记录见文末交付记录及 [结构化证据](evaluation/codex-final-acceptance-20261014.json)。报告提交只增加验收文档与证据。
- Windows 10 build 26200，Python 3.11.9，Node 24.14.0 / npm 11.9；隔离 clone、独立 venv、一次性任务应用。没有修改用户原工作区。
- LangChain 1.4.4、LangGraph 1.2.14、langchain-openai 1.7.0、SQLite checkpoint 3.1.1；pytest 9.1.1、Ruff 0.17.0、mypy 1.20.2；FastAPI 0.143.0、Uvicorn 0.54.0、Playwright 1.64.0；Tree-sitter 0.26.0 / Python、JavaScript grammar 0.25.0 / TypeScript grammar 0.23.2；SQLAlchemy 2.1.4、Alembic 1.20.0。
- 真实模型：现有配置的 `grok-4.7`，OpenAI-compatible 接口。密钥、服务端地址和私有 checkpoint 不进入交付文件。没有配置价格表，不推算费用。
- 测试通过、独立产物验收通过、Agent `completed` 三者分别记录。只有独立验收通过且 Agent 正常完成，真实任务才计为整体通过。
- model_calls 指收到的模型响应数；失败请求可能没有 usage，不把响应数冒充全部 API 请求数。输入 token 包含 cache_read；不是去重后的上下文，也不是美元成本。
- 当前 ChatGPT 协作连接所需 `c2c` 未发现，未完成连接；本次结果来自本机运行和两名只读审查代理，不声称经过 ChatGPT 网页复审。

## 2. 架构与完整功能验收矩阵

CLI/Settings → AgentApplication → LangGraph model/tools/checkpoint → 规划、验证、恢复与预算中间件 → 项目工具。SQLite 保存会话与 checkpoint；项目记忆、文件变更、工具结果和 trace 支撑恢复与核验。工具的执行成功不能单独证明业务任务完成。

| 功能 | 实现与测试方法 | 实际结果 / 边界 |
|---|---|---|
| LangChain/LangGraph、模型和工具调用 | `test_langgraph_runtime`, `test_langchain_tools`；真实场景 A/B/C | 离线通过；真实服务能调用工具，403/连接失败可阻断任务 |
| 任务规划、执行、完成状态 | `test_task_planning`, `test_development_workflow`, `test_plan_continuation`, `test_verification` | 通过；C 暴露未先建计划就更新步骤的实际误用 |
| checkpoint、进程间恢复、部分修改 | `test_persistence`、新增 harness resume；真实 checkpoint 场景 | 新 AgentApplication 恢复，不重复 write，mtime 不变；B 手动恢复仍有配额限制 |
| 长期项目记忆 | `test_project_memory`、RAG adapter/integration | 本地记录和本地模拟服务通过；远程知识服务未验收 |
| CLI、安装入口、配置、doctor、task | packaging 6 项、Quick Start 本地 HTTP provider | 通过；本地 provider 属确定性服务，不计真实 LLM |
| MCP stdio/HTTP | `test_mcp_bridge`, `test_mcp_integration` | 实际协议集成 6 passed；不外推所有第三方 MCP |
| 文件搜索/读取/创建/修改、批量操作、补丁 | `test_navigation_tools`, `test_file_scaffolding`, `test_file_safety`, `test_tools_hardening`, `test_cross_module_coding` | 通过；A 真实补丁保留测试，B 多文件开发，C 单字段修复 |
| Python/JS/TS/TSX 导航、跨文件候选引用 | `test_code_index`, `test_fullstack_interfaces` | 三类 grammar 离线可加载；真实 JS/TSX AST 排除注释/字符串；候选引用不是完整语义依赖图 |
| Git、Issue、Draft PR 工具 | `test_git_workflow`, `test_github_delivery` | 本地 Git/模拟 GitHub 回归通过；本次真实 Draft PR 由 Codex 交付，未证明 mini-agent LLM 能自行完成远程交付 |
| FastAPI/前端结构识别 | `test_fullstack_interfaces`, `test_dev_environment`；B/C discover | 通过，实际定位 backend/frontend 和字段错误 |
| OpenAPI 导出、检查、契约变化影响 | `test_openapi_contract`, `test_openapi_impact`；真实 Web、B | 实际导出成功，priority 出现在 served schema；静态影响分析有动态路径边界 |
| TypeScript 类型、typed client、React Query hooks | `test_fullstack_codegen`, `test_react_query_codegen`；真实 Web和B | 4 项生成工具成功；B main.tsx 实际导入 `$api`、调用 query/mutation；AST oracle 防注释/字符串伪造 |
| 后端测试、前端 typecheck/build | `test_fullstack_checks`；真实 Web、B/C | 通过；B 独立 backend 5 passed，C 原 CRUD 测试保留且通过 |
| 开发服务器、退出、端口清理 | `test_dev_servers`；实际 uvicorn/Vite | 管理器清空且 8000/5173 不再监听；关闭浏览器抛错时仍 finally 清理服务器 |
| Playwright 表单、按钮、DOM、截图 | `test_browser_tools`；2 项真实浏览器测试、B/C | 实际创建/编辑/显示通过；普通控件 value 可读，password/hidden/file 排除 |
| Console/Network 诊断 | browser diagnostics、真实 B/C | 最终独立验收无 console_error、JS exception、request_failed、http_error；历史瞬态另行保留 |
| SQLite 数据迁移、旧数据保留 | B 已有 tasks(id,title)，重复 initialize 两次、API+DB | seed 保留/default2，3→1 数据持久化，非法9返回422；该应用用原生 SQLite 迁移 |
| Alembic 生成与执行 | `test_alembic_migrations` 新增真实 Operations/MigrationContext | SQLite table/index upgrade、插入/读取、downgrade 通过；不是 B 使用 Alembic 或生产迁移证据 |
| 测试生成、代码审查 | `test_test_generation`, `test_code_review`；B 新增4条测试、C review工具 | 离线通过，C review成功；B 未完成 review/最终收尾 |
| 自动失败诊断/恢复、提前结束 | `test_auto_recovery`, `test_runtime_guards` | 命令/补丁/未完成计划/工具失败均有回归；新增失败工具名提示，未在服务恢复后的真实 C 闭环重测 |
| 浏览器请求错、服务器异常退出、可选依赖缺失 | `test_browser_tools`, `test_dev_servers`, `test_code_index`, guards | 故障分类和拒绝路径回归通过；不等于所有故障都能自主修好 |

## 3. 测试统计和执行命令

以下为受验源码的最终本机结果；独立执行的子集与全套有重叠，不相加制造总数。

| 检查 | 实际结果 |
|---|---|
| `python scripts/check.py` | 334 passed、4 skipped、148 subtests passed，154.89s；Ruff、mypy（50源文件）、全部 Python 语法通过；退出码0，临时目录清理成功 |
| `python scripts/check.py --packaging-only` | 6/6：依赖约束、离线依赖闭包、sdist、wheel、语法、wheel安装与CLI入口 |
| `python scripts/verify_quickstart.py --entry <installed-entry>` | config/version/doctor/start/sessions/task 通过（本地 provider） |
| `python -m pytest -q tests/test_mcp_integration.py` | 6 passed，实际 stdio/HTTP 服务，38.94s |
| `MINI_AGENT_RUN_REAL_WEB_ACCEPTANCE=1 python -m pytest -q tests/test_real_web_acceptance.py` | 2 passed，80.02s；实际 FastAPI、npm codegen、TypeScript、React/Vite、Chromium、表单 value隐私和进程清理 |
| `python -m pytest -q tests/test_live_acceptance.py`（实际现有配置） | 1 passed；独立契约 accepted，11模型响应、10工具调用 |
| check isolation + harness 定向回归 | 7 passed；长路径只读文件清理、关闭失败清理、AST反例、真实 checkpoint恢复和结果分类 |
| `git diff --check` | 通过 |

4 个跳过：真实 LLM opt-in、两项 real_web opt-in、当前隔离 Windows 账户的符号链接创建能力。Windows junction 测试已实际通过。上述 real_web 和真实 LLM 已分别显式执行；无法创建 symlink 的用例仍标记未验证，不冒充通过。

基准 SHA 的远程 Actions [38503846584](https://github.com/GimMoliyadi/mini-agent/actions/runs/38503846584) 已检查为 5/5 job 成功（Ubuntu/Windows × Python3.11/3.13、Ubuntu真实Web）。本次 CI 加入 Windows真实Web与明确安装 grammar/SQLAlchemy/Alembic，目标为6个job；最终运行记录见文末。

## 4. 真实 LLM 自主开发结果

所有项目是一次性合成 fixture，不存在人工代写 Agent 的需求实现。验证器单独调用真实测试、API、SQLite和浏览器。人工修复仅针对 mini-agent 工具、验收环境/oracle，调整预算与恢复 checkpoint；这些干预不计自主成功。

| 场景 | 响应/工具 | 输入/输出 token | 时长 | 自动恢复/手动恢复 | 独立结果 | Agent整体 |
|---|---:|---:|---:|---:|---|---|
| A 折扣逻辑修复 | 7 / 6 | 75,122 / 667 | 45.16s | 0 / 0 | 原测试不变；定位 price*rate，修为 price*(1-rate)，指定 unittest独立重跑通过 | completed，通过 |
| B 优先级全栈功能，累计含恢复 | 82 / 82 | 1,975,891 / 7,874 | 659.86s | 0 / 3 | 5后端测试、typecheck/build、API/SQLite/页面、重复迁移、typed hooks、冷启动通过 | failed；未完成最终review/收尾 |
| C 字段不一致，干净fixture | 44 / 43 | 706,851 / 2,923 | 258.65s | 1 / 1 | task.name→task.title；原测试保留，type/build、已有显示、创建编辑、review、浏览器零错误通过 | failed；恢复请求403 |
| 实际现有 calculator+notes 验收 | 11 / 10 | usage逐响应见JSON | 50.41s | 0 / 0 | accepted=true，指定独立测试通过 | completed，通过 |
| 真实 checkpoint，新应用恢复 | 3 / 2 | 30,403 / 176 | 23.26s | 刻意中断 / 1次正常resume | 写入后中断、新实例读回，写入未重放 | completed，通过 |

主要 A/B/C 的严格闭环结果为 **1/3**；三种产物独立验证均可工作。样本单模型、单项目族，不能据此估算通用任务成功率。B 的最终冷启动补验发生在模型停止后，不加入模型运行时长。

### B 的失败链与成本

1. 首次准备阶段 npm 继承了不可用代理并超时；改为项目规范化执行环境。随后 Windows npm cache 长路径破坏初始 snapshot。这两个阶段没有模型响应。
2. 在100k默认总token预算下，9次响应、99,721输入/701输出 token、51.17s即预算停止，尚未完成优先级实现。不是默认设置的成功案例。
3. 调整验收预算后累计42次响应（700,349输入）、55次（1,045,988）、75次（1,723,260）分别因总预算、16k上下文、总预算停止。每次明确修复环境/额度后从checkpoint继续，没有重建或覆盖Agent源码。
4. 最后累计82次响应，服务连接异常结束。四项typed codegen已成功，实际React组件使用生成hooks；B没有成功的 `review_code_changes` 事件，不能判完整开发闭环通过。
5. trace中7个工具错误，包含执行/搜索参数和browser误用；模型花费大量单次探索调用。cache_read 1,418,467输入token仍计入供应商报告的输入量；人工恢复的每段有独立有限额度，不把累计用量称作一次500k预算。
6. 曾一次观察到 Vite依赖缓存变化时的 Invalid hook call；同源码重启后通过。再次移动 `.vite` 缓存并独立冷启动，全测试、API、DB和UI通过。没有证据把该瞬态认定为最终源代码缺陷，也不从历史日志抹去它。

### C 的失败链

干净fixture在模型开始前已有本地Git baseline，最初typecheck明确报告不存在的name字段。模型完成字段修复、测试、UI与代码review；却调用了未先 `set_task_plan` 的 `update_task_step`，留下一个未解决失败。通用自动恢复提示后模型重复已成功检查，最后服务返回 HTTP403 `upstream_unavailable`。一次有限checkpoint重试仍403、没有新响应。输入cache_read为514,630；未得到失败请求usage。

本次增加恢复提示中的具体未解决工具名，并用确定性模型证明建立计划后可以正确解除该失败、完成任务。由于真实服务仍不可用，该改善的真实模型闭环效果**未验证**。旧C运行还受fixture没有Git根、额度不足影响，作为环境失败保留，不混入干净C结果。

## 5. 缺陷、修复与审查

| 优先级 | 复现根因 | 修复与回归 |
|---|---|---|
| P1 | Windows npm/pnpm CMD包装与进程模型不兼容；shell参数有歧义 | 直接node+已安装JS入口，支持普通/Corepack包装；实际npm含空格/元字符参数保持完整，不产生注入文件 |
| P1 | npm/node/pnpm缓存进入snapshot；长路径/共享硬链接破坏初始验收 | 缓存限定workspace/node_modules/.cache；实际pnpm离线tgz安装、snapshot不含依赖，Corepack和store回归 |
| P1 | language-pack运行时下载grammar，隔离无profile/联网环境无法导航 | 安装官方独立grammar wheels；Python/JS/TS/TSX无网络加载及实际AST回归 |
| P1 | C恢复未指出具体失败调用，成功工具不能解除另一个未解决失败 | recovery传递未解决工具名；计划前提修复、完整状态完成回归，保持审批/预算 |
| P1 | 验收browser shutdown异常跳过servers.close | finally清理；模拟关闭异常直接回归、实际端口核验 |
| P1 | scripts/check临时副本内pnpm长路径导致测试全绿但最终cleanup失败 | Windows extended drive/UNC路径、同命名空间containment；深路径只读文件回归，不清理source/共享venv |
| P2 | inspect只取innerText，无法核验React input现值 | 返回有界redacted普通value；真实填表，password/hidden/file不返回，选择合成文件回归 |
| P2 | harness resume重建/计数风险，产物通过会掩盖Agent failed | 保留checkpoint、历史报告、aggregate usage；failed+独立通过仍passed=false，mtime/分类回归 |
| P2 | typed hooks字符串oracle可能被注释伪造；C seed必须保持原文过严 | TSX AST import/call检查、反例回归；C对照API当前权威title，B仍要求旧seed保留 |
| P2 | CI迁移测试可跳过、真实Web仅Linux | 显式安装SQL依赖，新增Windows真实Web矩阵；真实Alembic升级/降级 |
| P2 | Windows测试默认GBK读取中文归档失败；fixture venv无法继承父venv | 显式UTF8；一次性.pth复用已准备环境，fixture初始化本地Git满足review前提 |

审查采用 OCR确定性文件/规则预览（25文件：9可审查、16默认排除），加代码/安全与性能/覆盖两名只读代理，最多3轮。发现的Corepack、pnpm缓存、文件控件隐私、清理finally、oracle和Windows权限路径问题均纳入修复/回归。未将纯样式偏好当缺陷；排除的tests/docs/fixtures仍人工核对。生产预算、审批、main和用户未提交工作未改变。

## 6. 成熟度（1–5）

1=能力缺失，2=依赖干预，3=有可用证据但有限，4=本范围验证较充分，5=多项目/多模型长期稳定。平均分仅描述这次有限样本，不是发布门槛。

| 能力 | 分 | 依据 |
|---|---:|---|
| 代码理解 | 4 | A/C根因准确；B正确理解已有SQLite与React链路 |
| 代码修改 | 4 | 原测试保留，独立业务结果正确 |
| 全栈开发 | 3 | codegen/迁移/浏览器实际可用，但B依赖干预 |
| 工具使用 | 2 | B调用细碎、输入成本高，C计划前提误用 |
| 自动调试 | 3 | A完成，C修复正确但自动恢复收尾受阻 |
| 测试能力 | 4 | 全回归、真实协议/浏览器/业务验证，有明确未验证项 |
| 状态恢复 | 3 | checkpoint实证不重放；额度/context恢复需人工调整 |
| 开发闭环 | 2 | 主真实场景仅A完整通过，B/C最终状态failed |
| GitHub交付 | 3 | 工具回归与本次真实Draft有证据，LLM自主远程发布未验证 |

总体约3.1/5：具有实用工具链的受监督Coding Agent，尚缺中等任务稳定自主收尾的证据。

## 7. 最终结论 A–F

**A. 简单任务稳定性：** 两个简单代码任务与真实checkpoint均通过，支持小任务使用；样本不足以承诺普遍稳定。

**B. 中等复杂度自主开发：** 能生成并实现可工作的全栈功能；本次B需3次人工恢复且最终failed，不能判无需干预自主完成。

**C. 自主诊断与修复：** 具备定位和正确修改能力，A完整闭环、C根因修复可复核。跨工具错误恢复和最终计划状态仍有短板；本次恢复提示改进仅确定性回归验证。

**D. 关键差距：** 工具目录/历史输入成本、任务粒度与上下文管理、恢复前提与状态收尾、外部模型失败、独立业务oracle覆盖、跨项目与多模型重复测试、真实远程交付与生产迁移证据。

**E. 是否合并PR #33：** 暂不建议直接合并main或发布。先评审整合本QA修复，再在可用模型服务下重新完成B/C严格闭环；绿色CI只证明工程检查，不代表LLM自主验收或用户批准。

**F. 下一阶段三个投入方向：**

1. 任务相关工具选择、批量只读探索、历史压缩与token成本基线，使默认预算能支撑中等任务。
2. 恢复定位到具体失败前提、计划状态修正与最终验收契约；加入B/C真实重复任务与服务失败/恢复。
3. 多项目、多模型、Linux/Windows重复验收，补真实远程Draft交付、迁移回滚和长时资源清理证据。

## 8. 复现与证据

离线：安装 `requirements.lock` 与 `.[dev,mcp,code_index]`、SQLAlchemy/Alembic，运行第3节命令。真实Web另安装 `.[browser]`、FastAPI/Uvicorn与Chromium；仅设置明确opt-in时运行npm/浏览器。

真实模型使用既有配置，准备新空目录（包含私有状态，不提交）：

```powershell
python examples/final_system_acceptance.py --config-dir <existing-config-directory> --work-dir <new-private-evidence-directory> --scenario B --max-total-tokens 750000 --max-model-calls 80 --context-tokens 32000
python examples/final_system_acceptance.py --config-dir <existing-config-directory> --work-dir <another-new-directory> --scenario C --max-total-tokens 1000000 --max-model-calls 80 --context-tokens 32000
```

这些是有费用的真实模型调用，预算为验收profile，产品默认100k总token/20次模型/16k上下文未调高。`--resume <session-id>`仅对同目录现有checkpoint，在明确纠正阻碍后使用。返回码1同时保留报告，不表示代码自动回滚。

结构化证据保留最终结果、失败阶段、token口径和执行命令。完整合成项目源码、redacted模型决策/工具结果、trace、截图及日志在本机验收证据包；不包含依赖目录、私有配置、Git对象或checkpoint DB。B冷启动独立补验、C最终结果及每次失败原始报告分别保留。

## 9. Draft交付记录

待本QA提交与远程CI核验后补充；所有任务结论以上述实际本机结果为准，不把尚未执行的远程检查计为通过。
