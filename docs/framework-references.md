# LangChain / LangGraph 迁移参考

本文记录本次迁移实际读取过的上游源码、当前可安装版本和对 mini-agent 的直接架构约束。源码基准为 2026-10-11 通过官方 GitHub、PyPI 和 LangChain 官方文档读取的内容；链接均固定到提交或版本标签，避免把旧教程 API 当成当前 API。

## 研究基准与版本

| 组件 | 当前可安装版本 | Python 约束 | 源码基准 |
| --- | --- | --- | --- |
| `langchain` | `1.4.4` | `>=3.10,<4` | [tag `langchain==1.4.4`](https://github.com/langchain-ai/langchain/tree/langchain%3D%3D1.4.4)，提交 `34489d61433cbf5044a630de2e7797e361672900` |
| `langchain-core` | `1.6.9` | `>=3.10,<4` | [tag `langchain-core==1.6.9`](https://github.com/langchain-ai/langchain/tree/langchain-core%3D%3D1.6.9)，提交 `fc7dc7757a46ebb444be1ead0efdeab5ca7ad8ce` |
| `langchain-openai` | `1.7.0` | `>=3.10,<4` | [tag `langchain-openai==1.7.0`](https://github.com/langchain-ai/langchain/tree/langchain-openai%3D%3D1.7.0)，提交 `2baf2f33c72cf027383a0b123e9be5077eb483d5` |
| `langgraph` | `1.2.14` | `>=3.10` | [主分支源码提交](https://github.com/langchain-ai/langgraph/tree/b4991f1ba36a2449ab59c38ab71828ee865bff84)，提交 `b4991f1ba36a2449ab59c38ab71828ee865bff84`；该提交的 `pyproject.toml` 版本为 `1.2.14` |
| `langgraph-checkpoint-sqlite` | `3.1.1` | `>=3.10` | [SQLite saver 源码提交](https://github.com/langchain-ai/langgraph/tree/b4991f1ba36a2449ab59c38ab71828ee865bff84)，同上游提交；该提交的 `pyproject.toml` 版本为 `3.1.1` |

`langchain 1.4.4` 声明 `langchain-core>=1.6.9,<2`、`langgraph>=1.2.11,<1.3`；`langgraph 1.2.14` 声明 `langgraph-checkpoint>=4.1,<5`、`langgraph-prebuilt>=1.1,<1.2` 和 `langgraph-sdk>=0.4.6,<0.5`。项目已经采用与 Python 3.11 相容的上界约束；升级时应重新锁定整组依赖，不要只升级 `langchain` 一个包。

## 实际读取的上游源码

### LangChain

- [agents/factory.py（稳定标签）](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D1.4.4/libs/langchain_v1/langchain/agents/factory.py)：`create_agent` 的签名、模型/工具循环、middleware 节点和 checkpointer 接线。
- [agents/middleware/types.py（稳定标签）](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D1.4.4/libs/langchain_v1/langchain/agents/middleware/types.py)：`AgentState`、`ModelRequest`、`ModelResponse`、`AgentMiddleware` 及同步/异步 hook。
- [agents/middleware/human_in_the_loop.py（稳定标签）](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D1.4.4/libs/langchain_v1/langchain/agents/middleware/human_in_the_loop.py)：审批请求/决策、`after_model` 中断和编辑后的 `wrap_tool_call`。
- [官方 HITL 指南](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)：`GraphOutput.interrupts`、多动作决策顺序及 UI 恢复流程。

`create_agent` 现在直接建立并编译一个 `CompiledStateGraph`，基础循环是：模型返回 `AIMessage.tool_calls` → 工具节点产生 `ToolMessage` → 再调用模型，直到没有 `tool_calls`。它接收 `model`、标准 LangChain tools/Callable、`system_prompt`、`middleware`、`context_schema`、`checkpointer`、`store`、`interrupt_before/after`、结构化输出和 streaming 参数。基础工具循环不需要再写一层 `while`/`StateGraph`。

`AgentMiddleware` 的职责边界很适合承接旧项目中仍有价值的横切逻辑：

- `before_agent` / `after_agent`：任务级初始化和收尾状态；
- `before_model` / `after_model`：上下文裁剪、完成前检查或根据模型结果调整状态；
- `wrap_model_call`：模型 fallback、重试、动态模型/工具选择、模型输出观测；
- `wrap_tool_call`：统一工具错误、重试、输出包装和观测。

`ModelRequest` 暴露 `messages`、`system_message`、`tools`、`state`、`runtime` 和 `model_settings`。修改请求应使用不可变的 `request.override(...)`，不要直接给请求字段赋值；源码已把直接赋值标为弃用。middleware 列表顺序是外层到内层，必须为同步入口和异步入口分别实现 hook，或者明确只暴露对应调用方式。

### LangGraph 持久化与中断

- [langgraph/types.py（`interrupt`、`Command`、`StateSnapshot`）](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/types.py)
- [checkpoint/base/__init__.py（`BaseCheckpointSaver`）](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/checkpoint/langgraph/checkpoint/base/__init__.py)
- [checkpoint/sqlite/__init__.py（`SqliteSaver`）](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py)
- [LangGraph persistence 指南](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts 指南](https://docs.langchain.com/oss/python/langgraph/interrupts)

`BaseCheckpointSaver` 的持久化主键是 `config["configurable"]["thread_id"]`。相同 `thread_id` 代表同一会话/工作流，换新的 ID 则从空状态开始；没有 `thread_id` 时不能保存、恢复中断或做 time-travel。checkpointer 是线程内短期状态，跨线程/用户的长期资料应使用 `store`，不能把两者混为一个 Session JSON。

`SqliteSaver` 的官方构造方式是：

```python
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver

conn = sqlite3.connect(".data/checkpoints.sqlite", check_same_thread=False)
checkpointer = SqliteSaver(conn)
agent = create_agent(model=model, tools=tools, checkpointer=checkpointer)
config = {"configurable": {"thread_id": thread_id}}
```

源码明确把同步 `SqliteSaver` 定位为轻量、同步、小规模场景；它不支持 async saver 方法。异步入口应选 `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` 并安装 `aiosqlite`。数据库由 `setup()` 建立 `checkpoints` 与 `writes` 表，并在需要时使用 WAL；连接生命周期由应用层管理。将 `.data/checkpoints.sqlite*` 排除在 Git 之外。

动态 `interrupt(value, response_schema=...)` 必须配合 checkpointer 使用。第一次调用会抛出可恢复的 `GraphInterrupt`，把 JSON 可序列化的 `value` 暴露给调用方；恢复时传入 `Command(resume=...)`，节点会从节点开头重新执行，`interrupt()` 返回 resume 值。`response_schema` 可用 Pydantic model、`TypedDict` 或 dataclass，给 UI 生成表单并校验恢复值；直接传 JSON Schema 只暴露 schema，不做值校验。

中断前的代码会在恢复时再次执行，所以副作用必须放在 `interrupt()` 之后，或具备业务幂等键；不要捕获 `interrupt` 抛出的特殊异常，也不要在同一节点中条件性改变多个 `interrupt` 的顺序。`SqliteSaver` 只保存图状态，不能证明一个已经发出但无回执的外部副作用没有发生；写工具仍需要幂等键、结果查询或人工确认。

### HelloAgents 工程实现

研究基准为 [jjyaoao/HelloAgents 提交 `c597a7ca46ea9969e5b21d9e130549ab69626ff9`](https://github.com/jjyaoao/HelloAgents/tree/c597a7ca46ea9969e5b21d9e130549ab69626ff9)。实际读取：

- [core/runtime.py](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/hello_agents/core/runtime.py)：显式模型/工具循环、并行工具、超时和事件流。
- [core/agent.py](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/hello_agents/core/agent.py)：Agent 外观、历史、上下文压缩、SessionStore 注入和子代理作用域。
- [core/components.py](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/hello_agents/core/components.py)：Protocol + `AgentComponents` 依赖注入。
- [core/session_store.py](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/hello_agents/core/session_store.py)：原子 JSON 快照、配置/工具 schema 一致性检查。
- [context/builder.py](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/hello_agents/context/builder.py)：预算感知的候选信息收集、筛选和消息组装。
- [agents/react_agent.py](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/hello_agents/agents/react_agent.py)：自定义 ReAct 循环和并行工具执行。
- [工具响应协议](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/docs/tool-response-protocol.md)、[自定义工具指南](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/docs/custom_tools_guide.md)、[会话持久化指南](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/docs/session-persistence-guide.md)、[工具权限指南](https://github.com/jjyaoao/HelloAgents/blob/c597a7ca46ea9969e5b21d9e130549ab69626ff9/docs/tool-policy-guide.md)。

应借鉴但不直接搬入 LangGraph 的部分：

1. `AgentComponents` 的 Protocol/依赖注入思想：模型、历史、截断器、SessionStore 和技能加载器可替换、可独立测试。
2. `ToolResponse(status, text, data, stats, error_info)` 的结构化结果：适配成 LangChain `ToolMessage` 时保留结构化 payload 和错误语义，不把失败重包装成字符串成功。
3. `ToolRegistry` 的单一名称空间、重复名称检查、`fork` 和 policy 入口：迁移后由 LangChain tool 列表作为模型可见集合，由 mini-agent 的业务 registry 保留风险级别、工作区预检、命令策略和执行元数据。
4. ContextBuilder 的 token 预算、诊断和历史压缩边界：迁移后由消息状态、middleware 或独立 context builder 负责，不在每个工具中塞入上下文特判。
5. SessionStore 的配置/schema 一致性提示和原子写入：旧 JSON 可以保留，但不能假装它是 LangGraph checkpoint；新会话以 `thread_id` + checkpointer 为准，并为旧文件提供只读导入或明确迁移提示。

应删除/由框架接管的部分：`runtime.py` 中手写的模型循环、工具 schema → OpenAI 字典转换、工具调用 ID 配对循环、并行调度和通用事件循环。保留文件安全、受控命令、审批策略、完成验收和 Verifier，它们属于业务工具/政策/验收层，而不是第二个 Agent Runtime。

### Datawhale Hello Agents 教程

研究基准为 [datawhalechina/hello-agents 提交 `623d8148f83d53365e39c21cb592b62b0f8e7b1b`](https://github.com/datawhalechina/hello-agents/tree/623d8148f83d53365e39c21cb592b62b0f8e7b1b)。实际读取：

- [第七章《构建你的 Agent 框架》](https://github.com/datawhalechina/hello-agents/blob/623d8148f83d53365e39c21cb592b62b0f8e7b1b/docs/chapter7/%E7%AC%AC%E4%B8%83%E7%AB%A0%20%E6%9E%84%E5%BB%BA%E4%BD%A0%E7%9A%84Agent%E6%A1%86%E6%9E%B6.md)：按 core/agents/tools 分层、统一工具注册和上下文接口。
- [第六章 LangGraph 对话示例](https://github.com/datawhalechina/hello-agents/blob/623d8148f83d53365e39c21cb592b62b0f8e7b1b/code/chapter6/Langgraph/Dialogue_System.py)：`StateGraph` 的显式节点、`InMemorySaver` 和 `configurable.thread_id`。
- [第九章《上下文工程》](https://github.com/datawhalechina/hello-agents/blob/623d8148f83d53365e39c21cb592b62b0f8e7b1b/docs/chapter9/%E7%AC%AC%E4%B9%9D%E7%AB%A0%20%E4%B8%8A%E4%B8%8B%E6%96%87%E5%B7%A5%E7%A8%8B.md)：GSSC、JIT/渐进式披露、预算压缩、即时 TerminalTool + 会话 MemoryTool + 持久 NoteTool。
- [第七章自定义 SimpleAgent](https://github.com/datawhalechina/hello-agents/blob/623d8148f83d53365e39c21cb592b62b0f8e7b1b/code/chapter7/my_simple_agent.py)：旧式文本 `[TOOL_CALL:...]` 解析循环。
- [第九章代码库维护示例](https://github.com/datawhalechina/hello-agents/blob/623d8148f83d53365e39c21cb592b62b0f8e7b1b/code/chapter9/codebase_maintainer.py)：把即时代码探索、笔记、记忆和上下文构建组合成 Agentic 维护任务。

教程的分层、JIT 探索和三层上下文思想仍有价值；`Dialogue_System.py` 的 `InMemorySaver` 只适合教学示例，`my_simple_agent.py` 的正则文本工具调用和手写循环不应迁移。当前工程应把显式 `StateGraph` 留给需要规划、分支、Verifier/Recovery 或多阶段工作流的场景，基础工具循环优先 `create_agent`。

## 推荐落地形状

### 基础 Agent

```python
from langchain.agents import create_agent
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver(sqlite3.connect(".data/checkpoints.sqlite", check_same_thread=False))
agent = create_agent(
    model=model,
    tools=tool_registry.langchain_tools(),
    system_prompt=system_prompt,
    middleware=[policy_middleware, observation_middleware, hitl_middleware],
    checkpointer=checkpointer,
)

config = {"configurable": {"thread_id": session_id}}
result = agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config)
```

`tool_registry.langchain_tools()` 应是唯一模型可见工具来源；每个工具在适配层内完成参数模型、工作区预检和结构化结果转换。入口层只装配 model/tools/checkpointer/middleware、传入 `thread_id` 并渲染结果。

### HITL 审批与 UI 恢复

对 `write_file`、`apply_patch`、`run_command` 等有副作用工具使用内置 middleware：

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware

hitl = HumanInTheLoopMiddleware(
    interrupt_on={
        "write_file": {"allowed_decisions": ["approve", "edit", "reject"]},
        "apply_patch": {"allowed_decisions": ["approve", "edit", "reject"]},
        "run_command": {"allowed_decisions": ["approve", "reject"]},
    },
)
```

调用时使用 `version="v2"`，让 `invoke` 返回 `GraphOutput`：

```python
from langgraph.types import Command

run = agent.invoke(input_state, config, version="v2")
if run.interrupts:
    # 将每个 interrupt.value（HITLRequest）原样映射到 CLI/Web 审批卡片。
    pending = [item.value for item in run.interrupts]
    return {"status": "interrupted", "thread_id": session_id, "interrupts": pending}

# UI 按 action_requests 顺序返回一个 HITLResponse：
resume = Command(resume={"decisions": [
    {"type": "approve"},
    # 或 {"type": "edit", "edited_action": {"name": "...", "args": {...}}}
    # 或 {"type": "reject", "message": "..."}
]})
run = agent.invoke(resume, config, version="v2")
```

多工具同时暂停时，`decisions` 必须与 `action_requests` 顺序一一对应；拒绝副作用动作使用 `reject`，不要用 `respond`，因为 `respond` 会被当作成功的 `ToolMessage`。审批状态不应自己另存一份“待执行 tool call”并重新调用工具；恢复应始终复用同一 `thread_id`、同一 graph/checkpointer 和 `Command(resume=...)`，以免重复副作用。

### 何时下沉到 StateGraph

只有以下情况才增加自定义 LangGraph workflow：

- 规划 → 执行 → 测试 → 验收需要条件分支或循环；
- Verifier 失败后要回到修复节点，而非让模型继续普通工具循环；
- Recovery 需要根据 checkpoint 状态跳过已确认完成的副作用步骤；
- 多 Agent/subgraph、并行任务或外部事件等待。

自定义 workflow 仍应把 `create_agent(...)` 作为一个节点/子图，避免在 StateGraph 中重新实现模型/工具循环。节点状态仅存可序列化的任务事实（阶段、测试结果、变更摘要、幂等键和验证证据），完整消息由 Agent 状态保存，长期知识由独立 Store/RAG 保存。

## 对 mini-agent 的迁移边界

| 旧能力 | 迁移后的归属 |
| --- | --- |
| 模型调用、Tool Calling、循环、工具调度、streaming | LangChain `create_agent` / LangGraph compiled graph |
| 文件/路径安全、受控命令、工具风险级别、审批前预检 | `mini_agent.tools` + policy middleware，作为标准 LangChain tools 暴露 |
| 会话短期状态、恢复、HITL checkpoint | LangGraph checkpointer + `thread_id` |
| 完整消息审计、工具结构化结果、错误码 | LangChain messages + 业务层 `ToolResult`/`ToolMessage` 适配 |
| 上下文预算、截断、JIT 文件读取 | 独立 context policy/middleware 和工具输出预算；不按工具名散落特殊规则 |
| Finish Gate、测试/变更验收、Verifier、Recovery 规则 | 可独立测试的业务服务；复杂分支再接 StateGraph |
| 未来跨会话 Memory/RAG/MCP | LangGraph Store、LangChain retriever/tool/MCP adapter；不写入 checkpoint schema |

旧 Session JSON 不做破坏性转换。恢复时必须区分“已保存的消息/审计记录”和“可继续执行的 LangGraph checkpoint”；对没有 checkpoint 的旧会话只能导入消息为新线程，不能据此重放写文件或命令。

## 不能忽略的 API 约束

1. 使用稳定包名 `langchain.agents.create_agent`，不要从历史 `langchain.agents.create_react_agent` 或 `langchain_classic` 复制旧循环。
2. `SqliteSaver` 的导入来自 `langgraph.checkpoint.sqlite`，不是 `langgraph.checkpoint.memory`；后者的内存 saver 不能跨进程恢复。
3. 每次 `invoke/stream` 都要传 `configurable.thread_id`；HITL 恢复必须复用同一个 ID。
4. `invoke(..., version="v2")` 可直接拿到含 `.interrupts` 的 `GraphOutput`；本项目固定使用 `get_state(config).tasks[*].interrupts` 读取持久化中断，CLI 不同时解析多种返回版本。
5. `HumanInTheLoopMiddleware` 的决策是 `approve`、`edit`、`reject`、`respond`；`edit` 的 action 必须仍指向可用工具，`respond` 仅用于人代替工具回答。
6. `interrupt()` 的 payload 必须可序列化，且节点恢复会从头执行；任何 interrupt 前非幂等写操作都可能重复。
7. 同步和异步入口不能混用：同步 `SqliteSaver` 不支持 async saver 方法，异步应用使用 `AsyncSqliteSaver`。
8. `langchain-openai 1.7.0` 的依赖要求 OpenAI SDK `>=2.45,<4`；项目的 OpenAI-compatible `base_url`、API key 和 model 仍应由统一配置注入，不能写死。
